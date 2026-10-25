"""Small web dashboard (password protected) plus public clip hosting for Instagram.

GET  /                 dashboard (HTTP basic auth, any username + DASHBOARD_PASSWORD; 5 wrong passwords lock an address out for 15 minutes)
POST /pause, /resume   stop or restart posting and production
POST /run              start a production cycle on the next tick
POST /submitted        mark all pending campaign links as submitted
POST /review/approve, /review/reject, /review/approve-all   decide on clips waiting for review
POST /manual/save, /manual/skip   record (or skip) a clip you posted yourself from your phone
POST /settings/review, /settings/ramp, /settings/youtube-auto, /settings/youtube-test   switch review mode / gradual start on or off
POST /settings/youtube-disconnect   cancel the saved YouTube login at Google and forget it
POST /campaigns/save   add or update a campaign from the form
POST /campaigns/toggle pause or resume one campaign
POST /campaigns/delete remove a campaign added from the dashboard
GET  /media/<t>.mp4    rendered clip, served under an unguessable token
GET  /health           liveness check for Railway
GET  /privacy          public privacy policy (Google asks for one)
GET  /connect/youtube  start Google sign-in; /oauth/youtube/callback finishes it
"""
import base64
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import asdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from . import campaigns, db, pipeline, strategy, youtube
from .config import Settings

log = logging.getLogger(__name__)
MEDIA_RE = re.compile(r"^/media/([0-9a-f]{32})\.mp4$")
RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$")
DIMENSIONS = {"music": "Music", "campaign": "Campaign", "topic": "Topic", "format": "Format", "length": "Clip length",
              "title_style": "Title style", "post_hour": "Posting time"}
PLATFORM_NAMES = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}
HOURS_ET = {"13": "9am ET", "16": "noon ET", "19": "3pm ET", "22": "6pm ET", "1": "9pm ET"}


def _arm_label(dimension: str, arm: str) -> str:
    if dimension == "post_hour":
        return HOURS_ET.get(arm, f"{arm}:00 UTC")
    if dimension == "length":
        fmt, secs = arm.split(":", 1)
        return f"{fmt} {secs}s"
    return arm.replace("_", " ")

PRIVACY = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport"
content="width=device-width, initial-scale=1"><title>Clip Engine privacy policy</title>
<style>body{font:16px/1.5 system-ui,sans-serif;max-width:640px;margin:24px auto;padding:0 16px}</style></head><body>
<h1>Clip Engine privacy policy</h1>
<p>Clip Engine is a private tool that its owner uses to publish short video clips to the owner's own YouTube channel.
It is not offered to the public and has no other users.</p>
<h2>What it accesses</h2>
<p>With the owner's permission through Google sign-in, it can upload videos to the owner's YouTube channel and read
that channel's video view and subscriber statistics. It uses these only to publish clips and to measure how they
perform.</p>
<h2>What it stores</h2>
<p>It stores a Google login token on the owner's own server so it can keep working without signing in again, and
the performance numbers it reads. It does not collect information about viewers.</p>
<h2>Sharing</h2>
<p>It does not sell, share or disclose this information to anyone.</p>
<h2>Revoking access</h2>
<p>Access can be removed at any time at <a href="https://myaccount.google.com/permissions">
myaccount.google.com/permissions</a>.</p>
<p>Clip Engine's use of information received from Google APIs adheres to the
<a href="https://developers.google.com/terms/api-services-user-data-policy">Google API Services User Data Policy</a>,
including the Limited Use requirements.</p>
</body></html>"""

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Clip Engine</title>
<style>
:root {{ --bg:#f6f7f9; --card:#fff; --text:#16181d; --muted:#667085; --line:#e4e7ec; --accent:#2f6fed; --bad:#c4320a; --good:#067647; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f1115; --card:#181b21; --text:#e6e8ec; --muted:#98a2b3; --line:#2a2f37; --accent:#6d9bff; --bad:#ff8a65; --good:#5fd39a; }} }}
body {{ margin:0; font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); }}
main {{ max-width:880px; margin:0 auto; padding:16px; }}
h1 {{ font-size:20px; margin:4px 0 12px; }} h2 {{ font-size:15px; margin:0 0 8px; color:var(--muted); font-weight:600; }}
section {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:14px; margin:12px 0; overflow-x:auto; }}
.row {{ display:flex; gap:8px; flex-wrap:wrap; align-items:center; }}
.stat {{ flex:1 1 120px; }} .stat b {{ display:block; font-size:22px; }} .stat span {{ color:var(--muted); font-size:13px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }} td,th {{ text-align:left; padding:6px 8px; border-top:1px solid var(--line); white-space:nowrap; }}
th {{ color:var(--muted); font-weight:600; border-top:0; }} a {{ color:var(--accent); }}
button {{ font:inherit; padding:8px 14px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--text); }}
button.primary {{ background:var(--accent); color:#fff; border-color:var(--accent); }}
.bad {{ color:var(--bad); }} .good {{ color:var(--good); }} .muted {{ color:var(--muted); }}
label {{ display:block; margin:10px 0 4px; font-weight:600; font-size:14px; }} label small {{ font-weight:400; color:var(--muted); }}
input[type=text], input[type=url], input[type=date], textarea {{ width:100%; box-sizing:border-box; font:inherit; padding:9px 10px;
  border:1px solid var(--line); border-radius:8px; background:var(--bg); color:var(--text); }}
textarea {{ min-height:70px; }} .checks label {{ display:inline-flex; gap:6px; font-weight:400; margin-right:14px; }}
.notice {{ border-color:var(--good); }} .error {{ border-color:var(--bad); color:var(--bad); }}
summary {{ cursor:pointer; font-weight:600; color:var(--accent); }} form.inline {{ display:inline; }}
.card {{ padding:10px 0; border-top:1px solid var(--line); }} .card:first-of-type {{ border-top:0; }}
video {{ width:100%; max-width:280px; aspect-ratio:9/16; object-fit:contain; border-radius:10px; background:#000; display:block; margin:8px 0; }}
.desc {{ white-space:pre-wrap; font-size:13px; color:var(--muted); }}
a.button {{ display:inline-block; padding:8px 14px; border-radius:8px; border:1px solid var(--line); text-decoration:none; }}
</style></head><body><main>
<h1>Clip Engine <span class="{state_class}">&middot; {state}</span></h1>
{banner}
{start}
{review}
{ready}
<section><div class="row">
<div class="stat"><b>${earned}</b><span>estimated earnings</span></div>
<div class="stat"><b>{views}</b><span>total views</span></div>
<div class="stat"><b>{posted}</b><span>posts live</span></div>
<div class="stat"><b>{queued}</b><span>posts queued</span></div>
</div><div class="row" style="margin-top:12px">
<form method="post" action="/{toggle}"><button class="primary">{toggle_label}</button></form>
<form method="post" action="/run"><button>Make clips now</button></form>
</div></section>
<section><h2>Links to submit</h2>{pending}</section>
<section id="campaigns"><h2>Campaigns</h2>{campaigns}{campaign_form}</section>
<section><h2>Trending now</h2>{trending}</section>
<section><h2>Earnings by campaign</h2>{earnings}</section>
<section><h2>What's working (higher score = more views and earnings)</h2>{strategy}</section>
<section><h2>Recent clips</h2>{clips}</section>
<section><h2>Recent problems</h2>{errors}</section>
<section id="settings"><h2>Settings</h2>{settings_html}</section>
</main></body></html>"""


def _table(headers: list[str], rows: list[list[str]], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{empty}</p>'
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><tr>{head}</tr>{body}</table>"


def _link(url: str | None, text: str) -> str:
    return f'<a href="{html.escape(url)}">{html.escape(text)}</a>' if url else html.escape(text)


def _trending(conn) -> str:
    rows = conn.execute("SELECT key, value FROM kv WHERE key LIKE 'trends:%' ORDER BY key").fetchall()
    e = html.escape
    blocks = []
    for r in rows:
        b = json.loads(r["value"])["brief"]
        lines = [f"<p><b>{e(r['key'][7:])}</b></p>"]
        for label, key in (("Hashtags", "hashtags"), ("Phrases", "phrases"), ("Title formats", "title_patterns")):
            if b.get(key):
                lines.append(f"<p><span class=\"muted\">{label}:</span> {e(', '.join(b[key]))}</p>")
        if b.get("notes"):
            lines.append(f"<p><span class=\"muted\">Hot this week:</span> {e(b['notes'])}</p>")
        if b.get("sounds"):
            lines.append(f"<p><span class=\"muted\">Trending sounds</span> (copyrighted; add licensed "
                         f"versions to the music library to use them): {e(', '.join(b['sounds']))}</p>")
        blocks.append("".join(lines))
    return "".join(blocks) or '<p class="muted">Trend research runs with the first clip.</p>'


def _campaign_rows(settings: Settings) -> str:
    """One card per campaign (cards fit a phone screen better than a wide table)."""
    e = html.escape
    locked = campaigns.env_ids()
    cards = []
    today = date.today()
    for c in campaigns.load(settings.data_dir):
        if c.ends_on and date.fromisoformat(c.ends_on) < today:
            status = '<span class="muted">ended</span>'
        else:
            status = '<span class="good">live</span>' if c.active else '<span class="bad">paused</span>'
        actions = [f'<a class="button" href="/?edit={e(c.id)}#campaign-form">Edit</a>',
                   f'<form class="inline" method="post" action="/campaigns/toggle"><input type="hidden" name="id" '
                   f'value="{e(c.id)}"><button>{"Pause" if c.active else "Resume"}</button></form>']
        if c.id not in locked:
            actions.append(f'<form class="inline" method="post" action="/campaigns/delete" onsubmit="return '
                           f'confirm(\'Delete this campaign? Its past clips stay.\')"><input type="hidden" '
                           f'name="id" value="{e(c.id)}"><button>Delete</button></form>')
        cards.append(f'<div class="card"><div><b>{_link(c.program_url, c.name)}</b> &middot; {status}</div>'
                     f'<div class="muted">${c.rate_per_1k_views:,.2f} per 1,000 views &middot; '
                     f'{e(", ".join(PLATFORM_NAMES.get(p, p) for p in c.platforms))}</div>'
                     f'<div class="row" style="margin-top:8px">{" ".join(actions)}</div></div>')
    return "".join(cards) or '<p class="muted">No campaigns yet. Add the first one below.</p>'


def _campaign_form(values: dict | None, editing: bool, open_form: bool) -> str:
    """Add/edit form. values: the campaign being edited, or what the user just submitted."""
    e = html.escape
    v = values or {}

    def val(key, default=""):
        return e(str(v.get(key, default) or ""))
    platforms = v.get("platforms", list(campaigns.PLATFORMS))
    checks = "".join(f'<label><input type="checkbox" name="platforms" value="{p}"'
                     f'{" checked" if p in platforms else ""}> {PLATFORM_NAMES[p]}</label>' for p in campaigns.PLATFORMS)
    sources = v.get("sources", "")
    if isinstance(sources, list):
        sources = "\n".join(sources)
    music_checked = " checked" if v.get("allow_music", True) not in (False, "off") else ""
    active_checked = " checked" if v.get("active", True) not in (False, "off") else ""
    title = "Edit campaign" if editing else "Add a campaign"
    return f"""<details id="campaign-form"{" open" if open_form else ""} style="margin-top:12px"><summary>{title}</summary>
<form method="post" action="/campaigns/save">
<input type="hidden" name="id" value="{val("id")}">
<label>Name <small>e.g. "Deep Talk Podcast"</small></label><input type="text" name="name" required value="{val("name")}">
<label>Campaign page link <small>the Whop (or other) page with the terms</small></label>
<input type="url" name="program_url" required placeholder="https://whop.com/..." value="{val("program_url")}">
<label>Pay per 1,000 views, in dollars</label><input type="text" name="rate" inputmode="decimal" required placeholder="1.50" value="{val("rate_per_1k_views")}">
<label>Creator's videos <small>channel, playlist, Google Drive or Dropbox links; one per line</small></label>
<textarea name="sources" required placeholder="https://www.youtube.com/@creator/videos">{e(sources)}</textarea>
<label>Required in every caption <small>tags or @mentions the campaign asks for</small></label>
<input type="text" name="caption_required" placeholder="@creator #creatorclips" value="{val("caption_required")}">
<label>Creator's rules <small>anything clips must or mustn't do</small></label>
<textarea name="rules" placeholder="No sponsor segments. Keep it family friendly.">{val("rules")}</textarea>
<label>What the content is about <small>used to research what's trending</small></label>
<input type="text" name="niche" placeholder="business and money podcast" value="{val("niche")}">
<label>Platforms it pays for</label><div class="checks">{checks}</div>
<label>Options</label><div class="checks">
<label><input type="checkbox" name="allow_music"{music_checked}> Background music allowed</label>
<label><input type="checkbox" name="active"{active_checked}> Live</label></div>
<label>End date <small>optional</small></label><input type="date" name="ends_on" value="{val("ends_on")}">
<div class="row" style="margin-top:14px"><button class="primary">Save campaign</button>
{'<a href="/#campaigns">Cancel</a>' if editing else ""}</div>
</form></details>"""


def _review(conn, settings: Settings) -> str:
    """Clips waiting for approval, each with a playable preview."""
    waiting = pipeline.awaiting_review(conn)
    if not waiting:
        return ""
    e = html.escape
    names = {c.id: c.name for c in campaigns.load(settings.data_dir)}
    cards = []
    for c in waiting:
        cards.append(
            f'<div class="card"><b>{e(c["title"])}</b>'
            f'<div class="muted">{e(names.get(c["campaign_id"], c["campaign_id"] or "archive"))} &middot; {c["seconds"]:.0f}s &middot; AI score '
            f'{e(str(c["ai_score"] or "?"))}/10 &middot; posts to '
            f'{e(", ".join(PLATFORM_NAMES.get(p.strip(), p) for p in c["platforms"].split(",")))}</div>'
            + (f'<div>On-screen hook: <b>{e(c["hook_text"])}</b></div>' if c.get("hook_text") else "")
            + f'<video controls playsinline preload="metadata" src="/media/{e(c["media_token"])}.mp4"></video>'
            f'<div class="desc">{e(c["description"])}</div>'
            f'<div class="row" style="margin-top:8px">'
            f'<form class="inline" method="post" action="/review/approve"><input type="hidden" name="id" '
            f'value="{c["id"]}"><button class="primary">Approve</button></form>'
            f'<form class="inline" method="post" action="/review/reject"><input type="hidden" name="id" '
            f'value="{c["id"]}"><button>Reject</button></form></div></div>')
    approve_all = ('<form method="post" action="/review/approve-all" style="margin-top:10px"><button>Approve all '
                   f'{len(waiting)}</button></form>' if len(waiting) > 1 else "")
    return (f'<section id="review"><h2>Waiting for your approval ({len(waiting)})</h2>'
            f'<p class="muted">Nothing posts until you approve it. Approved clips go out at their scheduled time.</p>'
            f'{"".join(cards)}{approve_all}</section>')


COPY_JS = """<script>
function copyText(id, btn) {
  const el = document.getElementById(id);
  const done = () => { const t = btn.textContent; btn.textContent = 'Copied'; setTimeout(() => btn.textContent = t, 1500); };
  if (navigator.clipboard) { navigator.clipboard.writeText(el.value).then(done, () => { el.select(); document.execCommand('copy'); done(); }); }
  else { el.select(); document.execCommand('copy'); done(); }
}
</script>"""

APP_TIPS = {
    "youtube": "YouTube app: + → Short → upload the video, paste the title, then paste the caption as the description.",
    "tiktok": "TikTok app: + → Upload → pick the video. Add a trending sound if you like, keeping it quiet under the "
              "speech, then paste the caption. If it's a paid campaign, switch on Content disclosure → Paid partnership.",
    "instagram": "Instagram app: + → Reel → pick the video. Add a trending sound if you like, keeping it quiet, then "
                 "paste the caption. If it's a paid campaign, add the Paid partnership label.",
}


def post_time_label(scheduled_at: str | None, now: datetime | None = None) -> str:
    """'now', or e.g. 'Tue 3:00pm ET' for a UTC timestamp from the database."""
    if not scheduled_at:
        return ""
    when = datetime.fromisoformat(scheduled_at).replace(tzinfo=timezone.utc)
    if when <= (now or datetime.now(timezone.utc)):
        return "now"
    local = when.astimezone(ZoneInfo("America/New_York"))
    return local.strftime("%a %-I:%M%p").replace("AM", "am").replace("PM", "pm") + " ET"


def _ready(conn, settings: Settings) -> str:
    """Approved clips for you to post from your phone, with copy buttons and a link box per platform."""
    clips = pipeline.ready_to_post(conn)
    if not clips:
        return ""
    e = html.escape
    names = {c.id: c.name for c in campaigns.load(settings.data_dir)}
    cards = []
    for c in clips:
        cid = c["id"]
        when = post_time_label(c["scheduled_at"])
        platforms_html = []
        for p in c["posts"]:
            name = PLATFORM_NAMES.get(p["platform"], p["platform"])
            platforms_html.append(
                f'<div class="card"><b>{name}</b><div class="muted">{e(APP_TIPS.get(p["platform"], ""))}</div>'
                f'<form method="post" action="/manual/save" class="row" style="margin-top:6px">'
                f'<input type="hidden" name="id" value="{p["id"]}">'
                f'<input type="url" name="url" required placeholder="Paste your {name} post link" style="flex:1 1 200px">'
                f'<button class="primary">Save link</button></form>'
                f'<form method="post" action="/manual/skip" style="margin-top:6px"><input type="hidden" name="id" '
                f'value="{p["id"]}"><button>Not posting on {name}</button></form></div>')
        caption = c["description"]
        cards.append(
            f'<div class="card"><b>{e(c["title"])}</b>'
            f'<div class="muted">{e(names.get(c["campaign_id"], c["campaign_id"] or "archive"))}'
            f'{" &middot; best time to post: " + when if when else ""}</div>'
            f'<video controls playsinline preload="metadata" src="/media/{e(c["media_token"])}.mp4"></video>'
            f'<div class="row"><a class="button" href="/media/{e(c["media_token"])}.mp4?download=1" '
            f'download="clip-{cid}.mp4">Download video</a></div>'
            f'<p class="muted" style="font-size:13px">iPhone: if the download doesn\'t go to Photos, open the '
            f'video, tap Share, then Save Video.</p>'
            f'<label>Title <small>for YouTube</small></label>'
            f'<div class="row"><input type="text" id="title-{cid}" readonly value="{e(c["title"])}" style="flex:1 1 200px">'
            f'<button type="button" onclick="copyText(\'title-{cid}\', this)">Copy</button></div>'
            f'<label>Caption <small>includes the hashtags, required tags and #ad</small></label>'
            f'<textarea id="caption-{cid}" readonly>{e(caption)}</textarea>'
            f'<div class="row"><button type="button" onclick="copyText(\'caption-{cid}\', this)">Copy caption</button></div>'
            f'{"".join(platforms_html)}</div>')
    return (f'<section id="ready"><h2>Ready to post from your phone ({len(clips)})</h2>'
            f'<p class="muted">Download the video, post it in each app with the caption, then paste the post\'s link '
            f'here so the engine can track its views. Unposted clips are dropped after 7 days.</p>'
            f'{"".join(cards)}{COPY_JS}</section>')


def _youtube_card(conn, settings: Settings) -> str:
    e = html.escape
    connected = bool(youtube.stored_token(conn, settings))
    auto = youtube.auto_posting_on(conn)
    if not (settings.youtube_client_id and settings.youtube_client_secret):
        body = ('<div class="muted">To connect: in Railway Variables add <code>YOUTUBE_CLIENT_ID</code> and '
                '<code>YOUTUBE_CLIENT_SECRET</code> from Google Cloud, with this redirect address: '
                f'<code>{e(youtube.redirect_uri(settings))}</code></div>')
        status = '<span class="muted">not set up</span>'
    elif not connected:
        status = '<span class="bad">not connected</span>'
        body = '<div class="row" style="margin-top:8px"><a class="button" href="/connect/youtube">Connect YouTube</a></div>'
    else:
        status = '<span class="good">connected</span>'
        lock = db.get(conn, "youtube_private_lock")
        lock_html = (f'<div class="error" style="padding:8px;border:1px solid;border-radius:8px;margin:8px 0">Google '
                     f'kept an automatic upload private (video {e(lock)}), so automatic YouTube posting was switched '
                     f'off. Post YouTube from your phone and request Google\'s audit.</div>' if lock else "")
        result = db.get(conn, "youtube_test_result")
        result_html = f'<div class="muted">Last test: {e(result)}</div>' if result else ""
        body = (f'{lock_html}<div class="muted">Automatic posting: {"on" if auto else "off"}. Google may keep videos '
                f'uploaded by a new project private. Run the test below first; if it says uploads stay public, turn '
                f'automatic posting on. Otherwise post YouTube from your phone.</div>{result_html}'
                f'<div class="row" style="margin-top:8px"><form class="inline" method="post" '
                f'action="/settings/youtube-auto"><button>{"Turn automatic posting off" if auto else "Turn automatic posting on"}</button></form>'
                f'<form class="inline" method="post" action="/settings/youtube-test"><button>Test a YouTube upload</button></form>'
                f'<a class="button" href="/connect/youtube">Reconnect</a>'
                f'<form class="inline" method="post" action="/settings/youtube-disconnect" '
                f'onsubmit="return confirm(\'Cancel the YouTube login? Posting stops until you reconnect.\')">'
                f'<button>Disconnect and cancel login</button></form></div>'
                f'<div class="muted" style="margin-top:6px">The test uploads a 3-second unlisted video. Delete it in '
                f'YouTube Studio afterwards. Disconnect cancels the saved login at Google; use it if you think '
                f'someone else got hold of it.</div>')
    return f'<div class="card"><b>YouTube connection:</b> {status}{body}</div>'


def setup_steps(conn, settings: Settings) -> list[tuple[str, bool, str]]:
    """(step, done, how to do it) for everything needed before clips go out."""
    connected = bool(youtube.stored_token(conn, settings))
    tested = (db.get(conn, "youtube_test_result") or "").startswith("uploads are not locked")
    auto = youtube.auto_posting_on(conn)
    decided = conn.execute("SELECT 1 FROM posts WHERE status != 'review' LIMIT 1").fetchone() is not None
    return [
        ("Add your Claude API key", bool(os.environ.get("ANTHROPIC_API_KEY")),
         "Create a key at console.anthropic.com (API Keys) and set a monthly spending limit. In Railway, open "
         "clip-engine, Variables, and add it as ANTHROPIC_API_KEY."),
        ("Connect YouTube", connected, '<a class="button" href="/connect/youtube">Connect YouTube</a>'),
        ("Test a YouTube upload", tested or auto,
         '<form class="inline" method="post" action="/settings/youtube-test"><button>Test a YouTube upload</button></form>'),
        ("Turn on automatic YouTube posting", auto,
         '<form class="inline" method="post" action="/settings/youtube-auto"><button>Turn it on</button></form>'),
        ("Add a campaign", bool(campaigns.load(settings.data_dir)),
         'Join a clipping campaign (for example on Whop), then fill in <a href="#campaigns">Add a campaign</a>.'),
        ("Approve your first clip", decided,
         '<form class="inline" method="post" action="/run"><button>Make clips now</button></form> '
         "then approve the best ones under Waiting for your approval. The first clips take about 15 minutes."),
    ]


def _getting_started(conn, settings: Settings) -> str:
    steps = setup_steps(conn, settings)
    done = sum(1 for _, ok, _ in steps if ok)
    if done == len(steps):
        return ""
    items, shown_next = [], False
    for name, ok, how in steps:
        if ok:
            items.append(f'<li class="good">&#10003; {html.escape(name)}</li>')
        elif not shown_next:
            shown_next = True
            # how is trusted markup written above, never user input
            items.append(f'<li><b>Next: {html.escape(name)}</b><div style="margin:6px 0">{how}</div></li>')
        else:
            items.append(f'<li class="muted">{html.escape(name)}</li>')
    return (f'<section id="start"><h2>Getting started: {done} of {len(steps)} done</h2>'
            f'<ol style="margin:0;padding-left:20px">{"".join(items)}</ol></section>')


def _settings(conn, settings: Settings) -> str:
    auto = pipeline.auto_platforms(conn)
    modes = ", ".join(f"{name}: {'automatic' if key in auto else 'from your phone'}"
                      for key, name in PLATFORM_NAMES.items())
    review_on = pipeline.review_mode(conn)
    ramp_on = db.get(conn, "ramp", "on") == "on"
    limit = pipeline.daily_clip_limit(conn, settings)
    return (
        _youtube_card(conn, settings) +
        f'<div class="card"><b>Posting:</b> {html.escape(modes)}<div class="muted">A platform switches to '
        f'automatic when its posting connection is added in Railway.</div></div>'
        f'<div class="card"><b>Review clips before posting:</b> '
        f'{"<span class=good>on</span>" if review_on else "<span class=bad>off</span>"}'
        f'<div class="muted">On: every clip waits for you to approve it. Off: clips post automatically.</div>'
        f'<form method="post" action="/settings/review" style="margin-top:8px"><button>'
        f'{"Turn off (post automatically)" if review_on else "Turn on"}</button></form></div>'
        f'<div class="card"><b>Gradual start:</b> {"on" if ramp_on else "off"} &middot; up to {limit} clips today'
        f'<div class="muted">Protects new accounts from looking like spam: 4 clips a day in week 1, '
        f'then {settings.max_clips_per_day}.</div>'
        f'<form method="post" action="/settings/ramp" style="margin-top:8px"><button>'
        f'{"Turn off" if ramp_on else "Turn on"}</button></form></div>')


def render(conn, settings: Settings, notice: str = "", error: str = "", form_values: dict | None = None,
           edit_id: str = "") -> str:
    e = html.escape
    paused = db.get(conn, "paused") == "1"
    earn = pipeline.earnings(conn, settings)
    totals = conn.execute(
        "SELECT COALESCE(SUM(views), 0) AS views, SUM(status = 'posted') AS posted, SUM(status = 'queued') AS queued"
        " FROM posts").fetchone()
    pending = pipeline.pending_submissions(conn)
    pending_html = _table(["Campaign", "Platform", "Link"],
                          [[e(p["campaign_id"]), e(p["platform"]), _link(p["url"], p["url"])] for p in pending],
                          "Nothing waiting.")
    if pending:
        pending_html += '<form method="post" action="/submitted" style="margin-top:10px"><button>Mark all submitted</button></form>'
    clips = conn.execute(
        "SELECT c.id, c.title, c.created_at, c.ai_score, c.campaign_id,"
        " GROUP_CONCAT(p.platform || ':' || p.status || ':' || COALESCE(p.views, 0) || ':' || COALESCE(p.url, ''), '|') AS posts"
        " FROM clips c LEFT JOIN posts p ON p.clip_id = c.id GROUP BY c.id ORDER BY c.id DESC LIMIT 25").fetchall()
    clip_rows = []
    for c in clips:
        parts = []
        for p in (c["posts"] or "").split("|"):
            if not p:
                continue
            platform, status, views, url = p.split(":", 3)
            label = f"{platform} {views}" if status == "posted" else f"{platform} ({status})"
            parts.append(_link(url, label))
        clip_rows.append([e(c["created_at"][5:16]), e(c["title"]), e(str(c["ai_score"] or "")),
                          e(c["campaign_id"] or "archive"), " · ".join(parts)])
    errors = conn.execute("SELECT platform, error, scheduled_at FROM posts WHERE error IS NOT NULL"
                          " ORDER BY id DESC LIMIT 8").fetchall()
    if edit_id and form_values is None:
        match = next((c for c in campaigns.load(settings.data_dir) if c.id == edit_id), None)
        form_values = asdict(match) if match else None
    banner = ""
    if notice:
        banner = f'<section class="notice">{e(notice)}</section>'
    if error:
        banner = f'<section class="error">{e(error)}</section>'
    return PAGE.format(
        banner=banner,
        start=_getting_started(conn, settings),
        review=_review(conn, settings),
        ready=_ready(conn, settings),
        settings_html=_settings(conn, settings),
        campaigns=_campaign_rows(settings),
        campaign_form=_campaign_form(form_values, editing=bool(form_values and form_values.get("id")),
                                     open_form=bool(form_values) or not campaigns.load(settings.data_dir)),
        state="paused" if paused else "running", state_class="bad" if paused else "good",
        earned=f"{sum(r['estimated_usd'] for r in earn):,.2f}", views=f"{totals['views']:,}",
        posted=totals["posted"] or 0, queued=totals["queued"] or 0,
        toggle="resume" if paused else "pause", toggle_label="Resume" if paused else "Pause",
        pending=pending_html,
        trending=_trending(conn),
        earnings=_table(["Campaign", "Clips", "Posts", "Views", "Est. $"],
                        [[e(r["campaign"]), str(r["clips"]), str(r["posts"]), f"{r['views']:,}",
                          f"{r['estimated_usd']:,.2f}"] for r in earn], "No campaign posts yet."),
        strategy=_table(["Choice", "Option", "Tried", "Score"],
                        [[e(DIMENSIONS.get(r["dimension"], r["dimension"])), e(_arm_label(r["dimension"], r["arm"])),
                          str(r["n"]), f"{r['mean']:.2f}"]
                         for r in strategy.leaderboard(conn)], "Learning starts once the first clips are 48 hours old."),
        clips=_table(["Made", "Title", "AI score", "Campaign", "Posts (views)"], clip_rows, "No clips yet."),
        errors=_table(["Platform", "Error", "When"],
                      [[e(r["platform"]), e((r["error"] or "")[:160]), e(r["scheduled_at"])] for r in errors],
                      "None."),
    )


MAX_BAD_PASSWORDS = 5
LOCKOUT_SECONDS = 15 * 60


class LoginThrottle:
    """Locks an address out for 15 minutes after 5 wrong passwords, so the password can't be guessed."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failures: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def _recent(self, client: str) -> list[float]:
        cutoff = self._clock() - LOCKOUT_SECONDS
        return [t for t in self._failures.get(client, []) if t > cutoff]

    def blocked(self, client: str) -> bool:
        with self._lock:
            return len(self._recent(client)) >= MAX_BAD_PASSWORDS

    def fail(self, client: str) -> None:
        with self._lock:
            self._failures[client] = self._recent(client) + [self._clock()]

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


def make_handler(settings: Settings, throttle: LoginThrottle | None = None):
    media_dir = settings.data_dir / "clips"
    throttle = throttle or LoginThrottle()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _client(self) -> str:
            # Railway's proxy appends the real address last; earlier entries can be forged.
            forwarded = self.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
            return forwarded or self.client_address[0]

        def _authorized(self) -> bool:
            self._locked = False
            if not settings.dashboard_password:
                return False
            client = self._client()
            if throttle.blocked(client):
                self._locked = True
                return False
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, password = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                return False
            if hmac.compare_digest(password, settings.dashboard_password):
                throttle.reset(client)
                return True
            throttle.fail(client)
            return False

        def _deny(self) -> None:
            if self._locked:
                self.send_response(429)
                self.send_header("Retry-After", str(LOCKOUT_SECONDS))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="clip-engine"')
            self.end_headers()

        def _send(self, code: int, body: bytes, ctype: str = "text/html; charset=utf-8") -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                return self._send(200, b"ok", "text/plain")
            url = urlsplit(self.path)
            if url.path == "/privacy":
                return self._send(200, PRIVACY.encode())
            if url.path in ("/connect/youtube", "/oauth/youtube/callback"):
                return self._youtube_oauth(url)
            match = MEDIA_RE.match(url.path)
            if match:
                path = media_dir / f"{match.group(1)}.mp4"
                if not path.exists():
                    return self._send(404, b"not found", "text/plain")
                return self._send_video(path, download="download=1" in url.query)
            if url.path != "/":
                return self._send(404, b"not found", "text/plain")
            if not self._authorized():
                return self._deny()
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            self._page(notice=query.get("notice", ""), error=query.get("error", ""), edit_id=query.get("edit", ""))

        def _youtube_disconnect(self, conn) -> None:
            """Cancel the saved login at Google, forget it and stop automatic YouTube posting."""
            token = youtube.stored_token(conn, settings)
            try:
                cancelled = youtube.revoke(token)
            except Exception:
                log.exception("YouTube token revoke failed")
                cancelled = False
            db.put(conn, "youtube_refresh_token", "")
            db.put(conn, "youtube_auto", "off")
            msg = ("YouTube disconnected and the login cancelled at Google." if cancelled else
                   "YouTube disconnected here, but Google didn't confirm the cancel. Also remove Clip Engine at "
                   "myaccount.google.com/permissions.")
            if settings.youtube_refresh_token:
                msg += " Delete the YOUTUBE_REFRESH_TOKEN variable in Railway too."
            return self._redirect("/?" + urlencode({"notice": msg}))

        def _youtube_test(self, conn) -> None:
            """Upload a tiny unlisted video and report whether Google kept it private."""
            token = youtube.stored_token(conn, settings)
            if not token:
                return self._redirect("/?" + urlencode({"error": "Connect YouTube first."}))
            try:
                result = youtube.YouTube(youtube.effective_settings(conn, settings)).test_upload(
                    settings.work_dir / "yt-test")
            except Exception as exc:
                db.put(conn, "youtube_test_result", f"failed: {exc}")
                return self._redirect("/?" + urlencode({"error": f"The test upload failed: {exc}"}))
            if result["locked"]:
                msg = ("Google kept the test video private, so API uploads stay private until Google approves the "
                       "project. Post YouTube from your phone and request the audit.")
                db.put(conn, "youtube_test_result", "uploads are kept private (audit needed)")
                return self._redirect("/?" + urlencode({"error": msg}))
            db.put(conn, "youtube_test_result", f"uploads are not locked (status: {result['applied']})")
            return self._redirect("/?" + urlencode({"notice": f"Test passed: the video was uploaded as "
                                                              f"{result['applied']}, not forced private. You can turn "
                                                              f"automatic YouTube posting on. Delete the test video in "
                                                              f"YouTube Studio."}))

        def _youtube_oauth(self, url) -> None:
            """Sign in with Google from the dashboard, so no computer or script is needed."""
            if not self._authorized():
                return self._deny()
            if not (settings.youtube_client_id and settings.youtube_client_secret and settings.public_base_url):
                return self._redirect("/?" + urlencode({"error": "Add YOUTUBE_CLIENT_ID, YOUTUBE_CLIENT_SECRET and "
                                                                   "PUBLIC_BASE_URL in Railway first."}))
            conn = db.connect(settings.db_path)
            try:
                if url.path == "/connect/youtube":
                    state = secrets.token_urlsafe(24)
                    db.put(conn, "youtube_oauth_state", state)
                    return self._redirect(youtube.authorize_url(settings, state), code=302)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                expected = db.get(conn, "youtube_oauth_state", "")
                db.put(conn, "youtube_oauth_state", "")  # one use only
                if not expected or not hmac.compare_digest(query.get("state", ""), expected):
                    return self._redirect("/?" + urlencode({"error": "That sign-in link expired. Tap Connect YouTube again."}))
                if "code" not in query:
                    return self._redirect("/?" + urlencode({"error": f"Google didn't connect the account "
                                                                     f"({query.get('error', 'cancelled')})."}))
                try:
                    token = youtube.exchange_code(settings, query["code"])
                except Exception as exc:
                    return self._redirect("/?" + urlencode({"error": f"Google sign-in failed: {exc}"}))
                db.put(conn, "youtube_refresh_token", token)
                return self._redirect("/?" + urlencode({"notice": "YouTube connected. Clips still post from your phone "
                                                                  "until you turn automatic posting on."}))
            finally:
                conn.close()

        def _send_video(self, path, download: bool = False) -> None:
            """Serve a clip, honouring byte ranges (iPhones won't play video without them)."""
            size = path.stat().st_size
            start, end = 0, size - 1
            match = RANGE_RE.match(self.headers.get("Range", ""))
            if match and size:
                if match.group(1):
                    start = int(match.group(1))
                    end = min(int(match.group(2)), size - 1) if match.group(2) else size - 1
                elif match.group(2):  # "bytes=-N": the last N bytes
                    start = max(0, size - int(match.group(2)))
                if start > end or start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.end_headers()
                    return
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            self.send_header("Content-Type", "video/mp4")
            if download:
                self.send_header("Content-Disposition", 'attachment; filename="clip.mp4"')
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remaining = end - start + 1
                while remaining > 0 and (chunk := f.read(min(1024 * 1024, remaining))):
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

        def _page(self, **kwargs) -> None:
            conn = db.connect(settings.db_path)
            try:
                self._send(200, render(conn, settings, **kwargs).encode())
            finally:
                conn.close()

        def _same_origin(self) -> bool:
            """Reject form posts from other websites (the browser sends the saved password with them)."""
            source = self.headers.get("Origin") or self.headers.get("Referer") or ""
            return bool(source) and urlsplit(source).netloc == self.headers.get("Host", "")

        def _form(self) -> dict[str, list[str]]:
            length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
            return parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)

        def _redirect(self, location: str, code: int = 303) -> None:
            self.send_response(code)
            self.send_header("Location", location)
            self.end_headers()

        def do_POST(self):
            if not self._authorized():
                return self._deny()
            if not self._same_origin():
                return self._send(403, b"forbidden", "text/plain")
            if self.path.startswith("/campaigns/"):
                return self._campaign_action()
            conn = db.connect(settings.db_path)
            try:
                if self.path == "/pause":
                    db.put(conn, "paused", "1")
                elif self.path == "/resume":
                    db.put(conn, "paused", "0")
                elif self.path == "/run":
                    db.put(conn, "run_now", "1")
                elif self.path == "/submitted":
                    pipeline.mark_submitted(conn, [p["id"] for p in pipeline.pending_submissions(conn)])
                elif self.path in ("/review/approve", "/review/reject"):
                    clip_id = int(self._form().get("id", ["0"])[0] or 0)
                    (pipeline.approve if self.path.endswith("approve") else pipeline.reject)(conn, clip_id)
                elif self.path in ("/manual/save", "/manual/skip"):
                    form = self._form()
                    post_id = int(form.get("id", ["0"])[0] or 0)
                    if self.path == "/manual/skip":
                        pipeline.skip_manual_post(conn, post_id)
                    else:
                        try:
                            pipeline.save_manual_post(conn, post_id, form.get("url", [""])[0])
                        except ValueError as exc:
                            return self._page(error=str(exc))
                elif self.path == "/review/approve-all":
                    for c in pipeline.awaiting_review(conn):
                        pipeline.approve(conn, c["id"])
                elif self.path == "/settings/review":
                    db.put(conn, "review_mode", "off" if pipeline.review_mode(conn) else "on")
                elif self.path == "/settings/youtube-auto":
                    turning_on = not youtube.auto_posting_on(conn)
                    db.put(conn, "youtube_auto", "on" if turning_on else "off")
                    if turning_on:
                        db.put(conn, "youtube_private_lock", "")
                elif self.path == "/settings/youtube-test":
                    return self._youtube_test(conn)
                elif self.path == "/settings/youtube-disconnect":
                    return self._youtube_disconnect(conn)
                elif self.path == "/settings/ramp":
                    db.put(conn, "ramp", "off" if db.get(conn, "ramp", "on") == "on" else "on")
                else:
                    return self._send(404, b"not found", "text/plain")
            finally:
                conn.close()
            self._redirect("/")

        def _campaign_action(self) -> None:
            raw = self._form()
            form = {k: v[0] for k, v in raw.items()}
            by_id = {c.id: c for c in campaigns.load(settings.data_dir)}
            if self.path == "/campaigns/save":
                try:
                    campaign = campaigns.from_form(form, raw.get("platforms", []))
                except campaigns.CampaignError as exc:
                    values = {**form, "rate_per_1k_views": form.get("rate", ""), "platforms": raw.get("platforms", []),
                              "allow_music": form.get("allow_music", "off"), "active": form.get("active", "off")}
                    return self._page(error=str(exc), form_values=values)
                existed = campaign.id in by_id
                campaigns.save(settings.data_dir, campaign)
                done = "updated." if existed else "added. Clips for it start on the next cycle."
                return self._redirect("/?" + urlencode({"notice": f"Campaign {campaign.name} {done}"}))
            campaign = by_id.get(form.get("id", ""))
            if campaign is None:
                return self._send(404, b"no such campaign", "text/plain")
            if self.path == "/campaigns/toggle":
                campaign.active = not campaign.active
                campaigns.save(settings.data_dir, campaign)
            elif self.path == "/campaigns/delete" and campaign.id not in campaigns.env_ids():
                campaigns.remove(settings.data_dir, campaign.id)
            else:
                return self._send(404, b"not found", "text/plain")
            self._redirect("/#campaigns")

    return Handler


def start(settings: Settings) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), make_handler(settings))
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    log.info("dashboard listening on port %s", settings.port)
    return server
