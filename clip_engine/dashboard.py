"""Small web dashboard (password protected) plus public clip hosting for Instagram.

GET  /                 dashboard (HTTP basic auth, any username + DASHBOARD_PASSWORD; 5 wrong passwords lock an address out for 15 minutes)
POST /pause, /resume   stop or restart posting and production
POST /run              start a production cycle on the next tick
POST /submitted        mark all pending campaign links as submitted
POST /submitted/one    mark one link as submitted
POST /review/approve, /review/reject, /review/approve-all   decide on clips waiting for review
POST /manual/save, /manual/skip   record (or skip) a clip you posted yourself from your phone
POST /settings/review, /settings/ramp, /settings/youtube-auto, /settings/youtube-test   switch review mode / gradual start on or off
POST /settings/post-to  accounts new clips are made for
POST /labels/done   clear the list of YouTube Shorts waiting for the paid-promotion tick
POST /repost   post a recent campaign clip again on one account
POST /post/schedule   set the time an automatic (YouTube) post goes out
POST /clip/hide   take a posted clip off the Your clips list (its posts stay up)
POST /clip/delete-video   delete a posted clip's saved video so it can't be posted again
POST /clip/delete   delete a clip that has not been posted yet (waiting for approval, ready to post or scheduled)
POST /post/now   send one queued automatic post at the next check instead of its scheduled time
POST /settings/youtube-disconnect   cancel the saved YouTube login at Google and forget it
POST /campaigns/save   add or update a campaign from the form
POST /campaigns/toggle pause or resume one campaign
POST /campaigns/delete remove a campaign added from the dashboard
GET  /media/<t>.mp4    rendered clip, served under an unguessable token
GET  /clip/<id>   one clip: watch it, approve, reject, delete, download
GET  /health           liveness check for Railway
GET  /privacy          public privacy policy (Google asks for one)
GET  /connect/youtube  start Google sign-in; /oauth/youtube/callback finishes it
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from dataclasses import asdict
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlsplit

from . import campaigns, db, manual, pipeline, policy, strategy, youtube
from .config import Settings

log = logging.getLogger(__name__)
MEDIA_RE = re.compile(r"^/media/([0-9a-f]{32})\.mp4$")
CLIP_RE = re.compile(r"^/clip/(\d+)$")
# where a form may send you afterwards: this site's own pages only ("/\\evil.com" would leave it)
SAFE_BACK_RE = re.compile(r"^/(?:clip/\d+)?(?:#[a-z-]+)?$")
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

STATE_LABELS = {"waiting": "Waiting for your approval", "approved": "Approved", "posted": "Posted"}
POST_STATUS_LABELS = {"review": "waiting for your approval", "ready": "ready to post from your phone",
                      "queued": "scheduled", "posted": "posted", "rejected": "removed", "expired": "removed",
                      "skipped": "skipped", "failed": "failed"}

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
{next}
{todo}
{start}
<section><div class="row">
<form method="post" action="/{toggle}"><button class="primary">{toggle_label}</button></form>
<form method="post" action="/run"><button>Make clips now</button></form>
<a class="button" href="/status">Clip status</a>
</div>{status}</section>
{own_video}
{library}
<section id="links"><h2>Links to submit</h2>{pending}</section>
<details id="more" style="margin-top:12px"><summary>More: campaigns, lists by step, earnings, trends and settings</summary>
<section id="campaigns"><h2>Campaigns</h2>{campaigns}{campaign_form}</section>
{review}
{ready}
{scheduled}
{repost}
<section><div class="row">
<div class="stat"><b>${earned}</b><span>estimated earnings</span></div>
<div class="stat"><b>{views}</b><span>total views</span></div>
<div class="stat"><b>{posted}</b><span>posts live</span></div>
<div class="stat"><b>{queued}</b><span>posts queued</span></div>
</div></section>
<section><h2>Earnings by campaign</h2>{earnings}</section>
<section><h2>Trending now</h2>{trending}</section>
<section><h2>What's working (higher score = more views and earnings)</h2>{strategy}</section>
<section><h2>Recent clips</h2>{clips}</section>
<section><h2>Recent problems</h2>{errors}</section>
<section id="settings"><h2>Settings</h2>{settings_html}</section>
</details>
<script>
// A link to a section inside the closed More box opens the box first, so the page can jump there.
function openFor(hash) {{ var t = hash && document.querySelector(hash); var d = t && t.closest('details'); if (d) d.open = true; }}
document.querySelectorAll('a[href^="#"]').forEach(function (a) {{ a.addEventListener('click', function () {{ openFor(a.getAttribute('href')); }}); }});
openFor(location.hash);
</script>
</main></body></html>"""

_CSS = PAGE.split("<style>", 1)[1].split("</style>", 1)[0].replace("{{", "{").replace("}}", "}")


CLIP_PAGE = ("""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title}</title><style>"""
             + PAGE.split("<style>", 1)[1].split("</style>", 1)[0].replace("{{", "{").replace("}}", "}")
             + "</style></head><body><main>{body}</main></body></html>")


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


UPLOAD_JS = """<script>
// Remember the campaign picked last time (only on this phone).
document.querySelectorAll('#own-video select[name=campaign]').forEach(function (sel) {
  try { var last = localStorage.getItem('campaign'); if (last && sel.querySelector('option[value="' + last + '"]')) sel.value = last; } catch (err) {}
  sel.addEventListener('change', function () { try { localStorage.setItem('campaign', sel.value); } catch (err) {} });
});
function sendVideo(form, btn) {
  const files = Array.from(form.file.files);
  if (!files.length) { alert('Choose at least one file first.'); return false; }
  const combine = form.mode.value === 'combine';
  const batch = Math.random().toString(36).slice(2, 10) + Date.now().toString(36);
  const status = form.querySelector('.status');
  let i = 0;
  const fail = text => { status.textContent = text; btn.disabled = false; window.uploading = false; };
  const next = () => {
    const file = files[i];
    const q = new URLSearchParams({campaign: form.campaign.value, creator: form.creator.value,
                                   title: form.title.value, length: form.length.value, count: form.count.value, review: form.review.checked ? '1' : '', name: file.name,
                                   batch: combine ? batch : '', last: i === files.length - 1 ? '1' : ''});
    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/manual-video/upload?' + q.toString());
    xhr.upload.onprogress = e => {
      if (e.lengthComputable) status.textContent = 'Uploading file ' + (i + 1) + ' of ' + files.length + ': ' + Math.round(100 * e.loaded / e.total) + '%';
    };
    xhr.onload = () => {
      let reply = {};
      try { reply = JSON.parse(xhr.responseText); } catch (err) {}
      if (xhr.status !== 200) { location.href = '/?error=' + encodeURIComponent(reply.message || 'Upload failed (' + xhr.status + ').'); return; }
      i += 1;
      if (i < files.length) { next(); } else { location.href = '/?notice=' + encodeURIComponent(reply.message || 'Uploaded.'); }
    };
    xhr.onerror = () => fail('Upload failed. Check your connection and try again.');
    xhr.send(file);
  };
  btn.disabled = true; window.uploading = true; status.textContent = 'Uploading file 1 of ' + files.length;
  next();
  return false;
}
</script>"""


# While clips are being made, reload the page so new clips appear by themselves, but never while
# the owner is typing, uploading or watching a preview.
REFRESH_JS = """<script>
setInterval(function () {
  var el = document.activeElement, busy = el && ['INPUT', 'TEXTAREA', 'SELECT'].indexOf(el.tagName) >= 0;
  var playing = Array.prototype.some.call(document.querySelectorAll('video'), function (v) { return !v.paused; });
  if (!busy && !playing && !window.uploading) location.replace(location.pathname);
}, 20000);
</script>"""


# The "Do this next" card: one thing at a time, with big buttons. On a phone, Share hands the video straight to
# TikTok or Instagram (no downloading), with the caption already copied.
NEXT_JS = """<script>
var clipFile = null;
function prepareClip(url) {
  fetch(url).then(function (r) { return r.blob(); }).then(function (b) {
    clipFile = new File([b], 'clip.mp4', {type: 'video/mp4'});
    var btn = document.getElementById('share-btn');
    if (btn) { btn.disabled = false; btn.textContent = btn.getAttribute('data-label'); }
  }).catch(function () {
    var btn = document.getElementById('share-btn');
    if (btn) { btn.disabled = false; btn.textContent = btn.getAttribute('data-label'); }
  });
}
function shareClip(captionId, downloadUrl) {
  var text = document.getElementById(captionId).value;
  if (navigator.clipboard) { navigator.clipboard.writeText(text).catch(function () {}); }
  if (clipFile && navigator.canShare && navigator.canShare({files: [clipFile]})) {
    navigator.share({files: [clipFile]}).catch(function () {});
  } else {
    location.href = downloadUrl;  // this phone can't share files: download it instead
  }
  return false;
}
function pasteLink(formId) {
  var f = document.getElementById(formId);
  if (!(navigator.clipboard && navigator.clipboard.readText)) { f.url.focus(); return false; }
  navigator.clipboard.readText().then(function (t) {
    var m = (t || '').match(/https?:\\/\\/\\S+/);
    if (m) { f.url.value = m[0]; f.submit(); }
    else { alert('First copy the post link: in the app tap Share, then Copy link. Then tap this button again.'); }
  }).catch(function () { f.url.focus(); });
  return false;
}
function copyAndOpen(link, page) {
  if (navigator.clipboard) { navigator.clipboard.writeText(link).catch(function () {}); }
  window.open(page, '_blank');
  return false;
}
</script>"""

BIG = 'style="font-size:18px;padding:14px 18px;width:100%;margin-top:8px"'


def _short_rules(campaign) -> str:
    """The two or three rules that matter while posting, in a few words."""
    if campaign is None:
        return ""
    rules = []
    if not campaign.allow_music:
        rules.append("Don't add music")
    rules.append("Don't turn on Paid partnership" if not campaign.paid_label else "Turn on Paid partnership")
    rules.append("Keep it public")
    return " &middot; ".join(rules) + "."


def _next_task(conn, settings: Settings) -> str:
    """The single next thing to do, with big buttons."""
    e = html.escape
    camps = campaigns.by_id(campaigns.load(settings.data_dir))

    def card(title: str, body: str) -> str:
        return (f'<section id="next" style="border:2px solid var(--accent)"><h2 style="font-size:17px;color:var(--text)">'
                f'{title}</h2>{body}</section>')

    # 1. A link that must be submitted soon
    for p in pipeline.pending_submissions(conn):
        camp = camps.get(p["campaign_id"])
        label, open_ = deadline_label(p, camp)
        if label and open_ and camp:
            name = PLATFORM_NAMES.get(p["platform"], p["platform"])
            return card(f"Submit your {e(name)} link now", (
                f'<p class="good"><b>{e(label)}</b></p>'
                f'<button class="primary" {BIG} onclick="return copyAndOpen({e(json.dumps(p["url"]))}, '
                f'{e(json.dumps(camp.program_url))})">1. Copy the link and open the campaign</button>'
                f'<p class="muted">On the campaign page tap <b>Submit clip</b>, paste the link, tick the box, submit.</p>'
                f'<form method="post" action="/submitted/one"><input type="hidden" name="id" value="{p["id"]}">'
                f'<input type="hidden" name="back" value="/"><button {BIG}>2. I submitted it &#10003;</button></form>'
                + NEXT_JS))

    # 2. A clip to check
    waiting = pipeline.awaiting_review(conn)
    if waiting:
        c = waiting[0]
        more = f' <span class="muted">({len(waiting) - 1} more after this)</span>' if len(waiting) > 1 else ""
        return card(f"Do you want to post this clip?{more}", (
            f'<video controls playsinline preload="metadata" style="width:100%;max-height:60vh;background:#000" '
            f'src="/media/{e(c["media_token"])}.mp4"></video>'
            f'<p><b>{e(c["title"])}</b></p>'
            f'<form method="post" action="/review/approve"><input type="hidden" name="id" value="{c["id"]}">'
            f'<input type="hidden" name="back" value="/"><button class="primary" {BIG}>&#128077; Yes, post it</button></form>'
            f'<form method="post" action="/review/reject"><input type="hidden" name="id" value="{c["id"]}">'
            f'<input type="hidden" name="back" value="/"><button {BIG}>&#128078; No, delete it</button></form>'))

    # 3. A clip to post from the phone
    ready = pipeline.ready_to_post(conn)
    if ready:
        c = ready[0]
        p = c["posts"][0]
        name = PLATFORM_NAMES.get(p["platform"], p["platform"])
        camp = camps.get(c["campaign_id"])
        video = f'/media/{e(c["media_token"])}.mp4'
        rules = _short_rules(camp)
        return card(f"Post this clip on {e(name)}", (
            f'<video controls playsinline preload="metadata" style="width:100%;max-height:45vh;background:#000" src="{video}"></video>'
            f'<textarea id="next-caption" readonly style="display:none">{e(c["description"])}</textarea>'
            f'<button id="share-btn" class="primary" {BIG} disabled data-label="1. Share to {e(name)}" '
            f'onclick="return shareClip(\'next-caption\', \'{video}?download=1\')">Getting the video ready&hellip;</button>'
            f'<p class="muted">Pick <b>{e(name)}</b> in the list that opens. The caption is already copied: in {e(name)} tap '
            f'<b>Next</b>, hold the caption box and tap <b>Paste</b>, then tap <b>Post</b>. {rules}</p>'
            f'<form id="link-form" method="post" action="/manual/save"><input type="hidden" name="id" value="{p["id"]}">'
            f'<input type="hidden" name="back" value="/">'
            f'<button type="button" {BIG} onclick="return pasteLink(\'link-form\')">2. Paste my {e(name)} link</button>'
            f'<p class="muted">After posting, in {e(name)} tap <b>Share</b>, then <b>Copy link</b>, come back and tap the button '
            f'above. Or paste it here:</p><div class="row"><input type="url" name="url" placeholder="https://" '
            f'style="flex:1 1 200px"><button>Save</button></div></form>'
            f'<form method="post" action="/manual/skip" style="margin-top:8px"><input type="hidden" name="id" value="{p["id"]}">'
            f'<input type="hidden" name="back" value="/"><button>Skip: not posting this on {e(name)}</button></form>'
            + NEXT_JS + f'<script>prepareClip("{video}");</script>'))

    # 4. Clips being made, or nothing to do
    if (pipeline.status(conn) or {}).get("state") == "running":
        return card("Making your clips&hellip;", '<p>This takes 5 to 20 minutes. You can close this page and come back.</p>')
    scheduled = len(pipeline.scheduled_posts(conn))
    later = (f'<p class="muted">YouTube will post {scheduled} clip{"s" if scheduled > 1 else ""} by itself.</p>'
             if scheduled else "")
    return card("All done &#10003;", f'<p>Nothing to do right now.</p>{later}'
                f'<a class="button primary" {BIG} href="#own-video">Add a video</a>')


def _todo(conn, settings: Settings) -> str:
    """The top of the page: what needs you right now, each item a link to its section, then shortcuts."""
    review = len(pipeline.awaiting_review(conn))
    to_post = sum(len(c["posts"]) for c in pipeline.ready_to_post(conn))
    links = len(pipeline.pending_submissions(conn))
    items = []
    if review:
        items.append(f'<a class="button" href="#clips"><b>{review}</b> clip{"s" if review > 1 else ""} to approve</a>')
    if to_post:
        items.append(f'<a class="button" href="#clips"><b>{to_post}</b> to post from your phone</a>')
    labels = len(youtube.label_todo(conn))
    if labels:
        items.append(f'<a class="button" href="#labels"><b>{labels}</b> YouTube label{"s" if labels > 1 else ""} to tick</a>')
    if links:
        camps = campaigns.by_id(campaigns.load(settings.data_dir))
        soonest = next((label for label, ok in (deadline_label(p, camps.get(p["campaign_id"]))
                                                for p in pipeline.pending_submissions(conn)) if label and ok), "")
        hurry = f' &middot; {html.escape(soonest.split(",")[0])}' if soonest else ""
        items.append(f'<a class="button" href="#links"><b>{links}</b> link{"s" if links > 1 else ""} to submit{hurry}</a>')
    has_campaign = bool(campaigns.load(settings.data_dir))
    if not items and not has_campaign:
        return ""  # still setting up: the Getting started list leads
    head = "".join(items) if items else '<span class="good">&#10003; Nothing needs you right now.</span>'
    shortcuts = ['<a href="#clips">Your clips</a>', '<a href="#campaigns">Campaigns</a>']
    if campaigns.live(campaigns.load(settings.data_dir)):
        shortcuts.append('<a href="#own-video">Add a video</a>')
    shortcuts.append('<a href="#settings">Settings</a>')
    return (f'<section id="todo"><div class="row">{head}</div>'
            f'<div class="muted" style="margin-top:8px;font-size:13px">Go to: {" &middot; ".join(shortcuts)}</div></section>')


def start_now(conn, settings: Settings, first: str) -> str:
    """After you hand over a video: start making clips straight away (resuming if paused) and say so,
    or say why it can't start yet. `first` is what was saved, e.g. "Video uploaded."."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return f"{first} Clips start once your Claude API key is added (step 1 of Getting started)."
    reason = pipeline.nothing_to_do(conn, settings)
    if reason:
        return f"{first} {reason}"
    db.put(conn, "paused", "0")  # handing over a video means you want clips
    db.put(conn, "run_now", "1")
    return (f"{first} Making clips now, nothing else to tap. They appear at the top of this page in 5 to 20 "
            "minutes; you can leave and come back.")


def _label_todo(conn) -> str:
    """YouTube Shorts where YouTube didn't confirm the paid-promotion label, each a link to its Studio page."""
    todo = youtube.label_todo(conn)
    if not todo:
        return ""
    links = "".join(f'<li>{_link(youtube.studio_link(v), "Open Short " + html.escape(v) + " in YouTube Studio")}</li>'
                    for v in todo)
    return (f'<div class="notice" id="labels"><b>Tick "Includes paid promotion" on {len(todo)} YouTube '
            f'Short{"s" if len(todo) > 1 else ""}</b><p class="muted">YouTube didn\'t take the label automatically '
            f'for these. Open each one, tap Show more (or Details), tick <b>Includes paid promotion</b>, and Save.</p>'
            f'<ul>{links}</ul><form method="post" action="/labels/done"><button>Done, I ticked them</button></form></div>')


def _status(conn) -> str:
    """One line under the buttons saying what clip-making is doing or why it made nothing."""
    s = pipeline.status(conn)
    if db.get(conn, "run_now") == "1" and db.get(conn, "paused") != "1":
        s = {"state": "running", "text": "Starting to make clips. This usually takes 5 to 20 minutes."}
    if not s:
        return ""
    css = {"running": "", "done": "good", "failed": "bad"}.get(s["state"], "muted")
    return (f'<div id="status" class="{css}" style="margin-top:10px">{html.escape(s["text"])}</div>'
            + (REFRESH_JS if s["state"] == "running" else ""))


def _own_video(conn, settings: Settings) -> str:
    """Add a video: pick the campaign, the file(s) and how many clips; everything else is under More options."""
    live = campaigns.live(campaigns.load(settings.data_dir))
    if not live:
        return ""
    e = html.escape
    options = "".join(f'<option value="{e(c.id)}">{e(c.name)}</option>' for c in live)
    waiting = manual.queue(conn)
    queue_html = ""
    if waiting:
        queue_html = ('<div class="muted" style="margin:8px 0">Waiting to be clipped: '
                      + "; ".join(e(w.get("title") or (f"{len(w['files'])} files" if w.get("kind") == "project" else w["ref"][-40:]))
                                  for w in waiting) + '</div>')
    campaign = f'<label>Campaign</label><select name="campaign" style="width:100%;font:inherit;padding:9px">{options}</select>'
    count = ('<label>How many clips do you want? <small>(up to 4)</small></label>'
             '<select name="count" style="width:100%;font:inherit;padding:9px">'
             + "".join(f'<option value="{n}"{" selected" if n == 2 else ""}>{n}</option>' for n in range(1, manual.MAX_VIDEOS + 1))
             + '</select>')
    review = ('<label style="margin-top:8px"><input type="checkbox" name="review" checked> Let me watch each clip before it '
              'can post</label>')
    names = ('<label>Creator\'s name <small>(credited in the caption; optional)</small></label><input type="text" name="creator">'
             '<label>Title <small>(optional; the post title when the campaign has no approved captions)</small></label>'
             '<input type="text" name="title">')
    return (
        '<section id="own-video"><h2>Add a video</h2>' + queue_html +
        '<form onsubmit="return sendVideo(this, this.querySelector(\'button\'))">' + campaign +
        f'<label>Video or photos <small>(one video is cut into clips; several files are combined)</small></label>'
        f'<input type="file" name="file" accept="video/*,image/*" multiple>' + count +
        '<details style="margin-top:10px"><summary>More options</summary>' + names +
        '<div class="checks" style="flex-direction:column;align-items:flex-start;margin-top:8px"><b>With several files:</b>'
        '<label><input type="radio" name="mode" value="combine" checked> <b>Combine</b> them into new videos, in a smart '
        'order (photos zoom slowly)</label>'
        '<label><input type="radio" name="mode" value="each"> Clip <b>each video</b> separately (videos only)</label></div>'
        '<label style="margin-top:8px">How long should each combined video be?</label>'
        '<select name="length" style="width:100%;font:inherit;padding:9px"><option value="30">15 to 30 seconds (best)</option>'
        '<option value="60">Up to 60 seconds</option></select>' + review + '</details>'
        '<div class="muted status" style="margin:6px 0"></div>'
        '<button class="primary" type="submit" style="margin-top:8px">Upload</button></form>'
        '<details style="margin-top:12px"><summary>Paste a link instead</summary>'
        '<form method="post" action="/manual-video/link">' + campaign +
        '<label>Video link</label><input type="url" name="url" placeholder="https://">' + count + review + names +
        '<button class="primary" style="margin-top:10px">Add video</button></form></details>'
        + UPLOAD_JS + '</section>')


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
    captions = v.get("approved_captions", "")
    if isinstance(captions, list):
        captions = "\n".join(captions)
    music_checked = " checked" if v.get("allow_music", True) not in (False, "off") else ""
    paid_checked = " checked" if v.get("paid_label", True) not in (False, "off") else ""
    active_checked = " checked" if v.get("active", True) not in (False, "off") else ""
    title = "Edit campaign" if editing else "Add a campaign"
    return f"""<details id="campaign-form"{" open" if open_form else ""} style="margin-top:12px"><summary>{title}</summary>
<form method="post" action="/campaigns/save">
<input type="hidden" name="id" value="{val("id")}">
<label>Name <small>e.g. "Deep Talk Podcast"</small></label><input type="text" name="name" required value="{val("name")}">
<label>Campaign page link <small>the Whop (or other) page with the terms</small></label>
<input type="url" name="program_url" required placeholder="https://whop.com/..." value="{val("program_url")}">
<label>Pay per 1,000 views, in dollars</label><input type="text" name="rate" inputmode="decimal" required placeholder="1.50" value="{val("rate_per_1k_views")}">
<label>Creator's videos <small>channel, playlist, Google Drive or Dropbox links; one per line. Leave empty if the campaign gives you files to download: you upload those under Add your own video</small></label>
<textarea name="sources" placeholder="https://www.youtube.com/@creator/videos">{e(sources)}</textarea>
<label>Approved captions <small>only if the campaign supplies them: one per line. The engine uses one exactly as written and adds nothing else (no hashtags, links, credit lines or on-screen text)</small></label>
<textarea name="approved_captions" placeholder="first look at the new drone @brand #ad">{e(captions)}</textarea>
<label>Disclosure on its own line <small>only if the campaign says the #Ad must sit alone on the line right after the caption, as the first hashtag; type it here, e.g. #Ad. The engine then writes: caption, that line, then at most 2 related hashtags, with no credit or other lines</small></label>
<input type="text" name="disclosure_line" placeholder="#Ad" value="{val("disclosure_line")}">
<label>Required in every caption <small>tags or @mentions the campaign asks for</small></label>
<input type="text" name="caption_required" placeholder="@creator #creatorclips" value="{val("caption_required")}">
<label>Creator's rules <small>anything clips must or mustn't do</small></label>
<textarea name="rules" placeholder="No sponsor segments. Keep it family friendly.">{val("rules")}</textarea>
<label>What the content is about <small>used to research what's trending</small></label>
<input type="text" name="niche" placeholder="business and money podcast" value="{val("niche")}">
<label>Platforms it pays for</label><div class="checks">{checks}</div>
<label>Options</label><div class="checks">
<label><input type="checkbox" name="allow_music"{music_checked}> Background music allowed</label>
<label>Skip at the start <small>seconds cut from the start of every video: rating cards, studio intros. 0 for none</small></label>
<input type="text" name="trim_start" inputmode="decimal" placeholder="0" value="{val("trim_start") if v.get("trim_start") else ""}">
<label>Skip at the end <small>seconds cut from the end of every video: logos, end cards. 0 for none</small></label>
<input type="text" name="trim_end" inputmode="decimal" placeholder="0" value="{val("trim_end") if v.get("trim_end") else ""}">
<label>Submit within <small>minutes after posting the campaign accepts a link (e.g. 30); 0 for no limit. The dashboard then shows a deadline for each link</small></label>
<input type="text" name="submit_within_minutes" inputmode="numeric" placeholder="0" value="{val("submit_within_minutes") if v.get("submit_within_minutes") else ""}">
<label>Keep posts up for <small>days the campaign requires a post to stay live (e.g. 30); 0 if not stated</small></label>
<input type="text" name="keep_live_days" inputmode="numeric" placeholder="0" value="{val("keep_live_days") if v.get("keep_live_days") else ""}">
<label><input type="checkbox" name="paid_label"{paid_checked}> Use the apps' own paid-promotion switch <small>(untick if the campaign bans "paid promotion or ads of any kind"; the caption's #Ad is then the disclosure)</small></label>
<label><input type="checkbox" name="active"{active_checked}> Live</label></div>
<label>End date <small>optional</small></label><input type="date" name="ends_on" value="{val("ends_on")}">
<div class="row" style="margin-top:14px"><button class="primary">Save campaign</button>
{'<a href="/#campaigns">Cancel</a>' if editing else ""}</div>
</form></details>"""


def _platform_checks(platforms: list[str], checked: list[str] | None = None) -> str:
    """A tick box per account, all ticked unless `checked` says otherwise."""
    checked = platforms if checked is None else checked
    return "".join(f'<label><input type="checkbox" name="platforms" value="{p}"{" checked" if p in checked else ""}> '
                   f'{html.escape(PLATFORM_NAMES.get(p, p))}</label>' for p in platforms)


def deadline_label(post: dict, campaign, now: datetime | None = None) -> tuple[str, bool]:
    """("Submit by 2:45pm ET, 12 min left", still open) for a campaign with a submit window, else ("", True)."""
    limit = getattr(campaign, "submit_within_minutes", 0)
    got = pipeline.submit_deadline(post.get("posted_at"), limit, now)
    if not got:
        return "", True
    deadline, left = got
    at = deadline.astimezone(ZoneInfo(pipeline.LOCAL_ZONE)).strftime("%-I:%M%p").replace("AM", "am").replace("PM", "pm")
    if left > 0:
        return f"Submit by {at} ET, {left} min left", True
    return f"Too late to submit: the {limit}-minute window closed at {at} ET.", False


def _submissions(pending: list[dict], settings: Settings, conn=None) -> str:
    """The posted links still to be submitted to their campaign, each with its deadline and buttons: Submitted
    while the window is open; Post again and Remove once it has closed."""
    e = html.escape
    camps = campaigns.by_id(campaigns.load(settings.data_dir))
    again = {c["id"]: c["platforms"] for c in pipeline.repostable(conn)} if conn is not None else {}
    cards = []
    for p in pending:
        camp = camps.get(p["campaign_id"])
        label, open_ = deadline_label(p, camp)
        when = f'<div class="{"good" if open_ else "bad"}"><b>{e(label)}</b></div>' if label else ""
        page = (f'<a class="button" href="{e(camp.program_url)}" target="_blank" rel="noopener">Open campaign page</a>'
                if camp and camp.program_url else "")
        cards.append(
            f'<div class="card"><b>{e(p["title"])}</b>'
            f'<div class="muted">{e(camp.name if camp else p["campaign_id"])} &middot; '
            f'{e(PLATFORM_NAMES.get(p["platform"], p["platform"]))}</div>{when}'
            f'<div class="row" style="margin-top:6px"><input type="text" id="link-{p["id"]}" readonly value="{e(p["url"])}" '
            f'style="flex:1 1 200px"><button type="button" onclick="copyText(\'link-{p["id"]}\', this)">Copy link</button></div>'
            + (f'<div class="row" style="margin-top:6px">{page}'
               f'<form class="inline" method="post" action="/submitted/one"><input type="hidden" name="id" value="{p["id"]}">'
               f'<input type="hidden" name="back" value="/#links"><button class="primary">Submitted &#10003;</button></form></div>'
               if open_ else
               '<div class="row" style="margin-top:6px">'
               + (f'<form class="inline" method="post" action="/repost" onsubmit="return confirm({_again_question(p["platform"])})">'
                  f'<input type="hidden" name="id" value="{p["clip_id"]}"><input type="hidden" name="platform" '
                  f'value="{e(p["platform"])}"><button class="primary">Post again on '
                  f'{e(PLATFORM_NAMES.get(p["platform"], p["platform"]))}</button></form>'
                  if p["platform"] in again.get(p["clip_id"], []) else "")
               + f'<form class="inline" method="post" action="/submitted/one"><input type="hidden" name="id" value="{p["id"]}">'
                 f'<input type="hidden" name="back" value="/#links"><button>Remove</button></form></div>'
                 '<div class="muted" style="font-size:13px">Remove only takes it off this list; the post stays up.</div>')
            + '</div>')
    return "".join(cards) + COPY_JS


def campaign_checklist(campaign) -> list[str]:
    """The campaign's posting rules as a short checklist for posting by hand."""
    if campaign is None:
        return []
    items = []
    if campaign.disclosure_line:
        items.append(f"{campaign.disclosure_line} sits alone on the line right after the caption text (the copied "
                     "caption already has it).")
    else:
        items.append("Keep the #ad disclosure in the caption (the copied caption already has it).")
    if not campaign.allow_music:
        items.append("Don't add any sound or music.")
    items.append("Don't switch on Paid partnership, Branded content or any ad option, and don't boost the post."
                 if not campaign.paid_label else "Switch on the app's paid-partnership label.")
    items.append("Everyone can view the post, and likes are visible.")
    if campaign.submit_within_minutes:
        items.append(f"Submit the link on the campaign page within {campaign.submit_within_minutes} minutes of posting.")
    if campaign.keep_live_days:
        items.append(f"Don't delete the post for {campaign.keep_live_days} days.")
    return items


def _checklist_html(campaign) -> str:
    items = campaign_checklist(campaign)
    if not items:
        return ""
    return ('<div class="checks" style="flex-direction:column;align-items:flex-start;margin:8px 0">'
            '<b>Before you post</b>'
            + "".join(f'<label><input type="checkbox"> {html.escape(i)}</label>' for i in items) + '</div>')


def _clip_needs(c: dict) -> str:
    """One line saying what a clip is waiting for."""
    if c["state"] == "waiting":
        return '<b class="bad">Needs your approval</b>'
    if c["state"] == "approved":
        parts = []
        for p in c["posts"]:
            name = PLATFORM_NAMES.get(p["platform"], p["platform"])
            if p["status"] == "ready":
                parts.append(f"<b>Post it on {name}</b>")
            elif p["status"] == "queued":
                when = post_time_label(p["scheduled_at"])
                parts.append(f"{name} posts {'within 5 minutes' if when == 'now' else 'at ' + html.escape(when)}")
        return " &middot; ".join(parts) or "Approved"
    done = sorted({PLATFORM_NAMES.get(p["platform"], p["platform"]) for p in c["posts"] if p["status"] == "posted"})
    return '<span class="good">Posted</span> on ' + ", ".join(done)


def _hide_form(clip_id: int) -> str:
    return (f'<form class="inline" method="post" action="/clip/hide" onsubmit="return confirm(\'Take this clip off the list? '
            f'Its posts stay up.\')"><input type="hidden" name="id" value="{clip_id}"><input type="hidden" name="back" '
            f'value="/#clips"><button>Remove from list</button></form>')


def _clip_meta(c: dict) -> str:
    return f'{c["seconds"]:.0f}s &middot; {STATE_LABELS[c["state"]]}'


def _library(conn, settings: Settings) -> str:
    """Your clips: every recent clip, grouped by campaign. Tap one to open its own page."""
    clips = pipeline.clip_library(conn)
    running = (pipeline.status(conn) or {}).get("state") == "running"
    if not clips and not running:
        return ""
    e = html.escape
    names = {c.id: c.name for c in campaigns.load(settings.data_dir)}
    groups: dict[str, list[dict]] = {}
    for c in clips:
        groups.setdefault(c["campaign_id"] or "", []).append(c)
    blocks = []
    for campaign_id, items in groups.items():
        rows = "".join(
            f'<div class="card"><b>{e(c["title"])}</b><div class="muted">{c["seconds"]:.0f}s &middot; {_clip_needs(c)}</div>'
            f'<div class="row" style="margin-top:6px"><a class="button primary" href="/clip/{c["id"]}">Open</a>'
            + (_delete_form(c["id"], back="/#clips") if c["state"] in ("waiting", "approved") else _hide_form(c["id"]))
            + '</div></div>'
            for c in items)
        blocks.append(f'<details open><summary>{e(names.get(campaign_id, campaign_id or "Other clips"))} '
                      f'({len(items)})</summary>{rows}</details>')
    working = ('<p class="muted"><b>Making clips now.</b> New ones appear here in a few minutes; this page refreshes by '
               'itself.</p>' if running else "")
    return ('<section id="clips"><h2>Your clips</h2><p class="muted">Tap Open to watch a clip and do everything for it: '
            'approve, download, copy the caption, save the post link, set the YouTube time.</p>'
            f'{working}{"".join(blocks)}</section>')


def _posting_tools(c: dict, camp) -> str:
    """On a clip's page: the caption and a link box for each app you post to by hand, and the time box for
    each automatic (YouTube) post."""
    e = html.escape
    ready = [p for p in c["posts"] if p["status"] == "ready"]
    queued = [p for p in c["posts"] if p["status"] == "queued"]
    if not ready and not queued:
        return ""
    out = []
    back = f'<input type="hidden" name="back" value="/clip/{c["id"]}">'
    if ready and c["has_video"]:
        out.append(f'<div class="row"><a class="button primary" href="/media/{e(c["media_token"] or "")}.mp4?download=1" '
                   f'download="clip-{c["id"]}.mp4">1. Download the video</a></div>')
    if ready:
        out.append(f'<label>Caption</label><textarea id="caption-{c["id"]}" readonly>{e(c["description"])}</textarea>'
                   f'<div class="row"><button type="button" onclick="copyText(\'caption-{c["id"]}\', this)">Copy caption</button></div>')
    for p in ready:
        name = PLATFORM_NAMES.get(p["platform"], p["platform"])
        out.append(
            f'<div class="card"><b>Post it on {e(name)}</b><div class="muted">{e(app_tip(p["platform"], camp))}</div>'
            f'<form method="post" action="/manual/save" class="row" style="margin-top:6px"><input type="hidden" name="id" '
            f'value="{p["id"]}">{back}<input type="url" name="url" required placeholder="Paste your {e(name)} post link" '
            f'style="flex:1 1 200px"><button class="primary">Save link</button></form>'
            f'<form method="post" action="/manual/skip" style="margin-top:6px"><input type="hidden" name="id" value="{p["id"]}">'
            f'{back}<button>Not posting on {e(name)}</button></form></div>')
    for p in queued:
        name = PLATFORM_NAMES.get(p["platform"], p["platform"])
        out.append(
            f'<div class="card"><b>{e(name)} posts it by itself</b>'
            f'<form method="post" action="/post/schedule" style="margin-top:6px"><input type="hidden" name="id" value="{p["id"]}">'
            f'{back}<label>Posts at <small>(Eastern time)</small></label><div class="row">{_time_box("at", p["scheduled_at"])}'
            f'<button class="primary">Set time</button></div></form>'
            f'<form method="post" action="/post/now" style="margin-top:6px"><input type="hidden" name="id" value="{p["id"]}">'
            f'{back}<button>Post now</button></form></div>')
    return '<section><h2>Post it</h2>' + "".join(out) + '</section>'


def et_text(utc_text: str | None) -> str:
    """A UTC timestamp from the database as Eastern time, e.g. "Nov 18, 9:15am ET"."""
    if not utc_text:
        return ""
    when = datetime.fromisoformat(utc_text).replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/New_York"))
    return when.strftime("%b %-d, %-I:%M%p").replace("AM", "am").replace("PM", "pm") + " ET"


def status_page(conn, settings: Settings) -> str:
    """Every clip in order, newest first: when it was made, and for each app its status, when it was posted and its
    views. Reads the same data the rest of the dashboard uses; nothing here changes anything."""
    e = html.escape
    names = {c.id: c.name for c in campaigns.load(settings.data_dir)}
    rows = conn.execute(
        "SELECT c.id, c.title, c.campaign_id, c.created_at FROM clips c ORDER BY c.id DESC LIMIT 200").fetchall()
    try:
        watched = json.loads(db.get(conn, "retention", "{}"))
    except ValueError:
        watched = {}
    out = []
    for c in rows:
        posts = conn.execute(
            "SELECT platform, status, scheduled_at, posted_at, views, url, external_id FROM posts WHERE clip_id = ? ORDER BY platform",
            (c["id"],)).fetchall()
        lines = []
        for p in posts:
            name = PLATFORM_NAMES.get(p["platform"], p["platform"])
            label = {"review": "waiting for your approval", "ready": "ready to post by hand",
                     "queued": "scheduled", "posted": "posted", "rejected": "removed", "expired": "expired",
                     "skipped": "skipped", "failed": "failed"}.get(p["status"], p["status"])
            when = ""
            if p["status"] == "posted" and p["posted_at"]:
                when = f' on {e(et_text(p["posted_at"]))}'
            elif p["status"] == "queued" and p["scheduled_at"]:
                when = f' for {e(et_text(p["scheduled_at"]))}'  # the same date format as the rest of the page
            views = f' &middot; {p["views"]:,} views' if p["status"] == "posted" and p["views"] is not None else ""
            link = f' &middot; {_link(p["url"], "open")}' if p["url"] else ""
            pct = watched.get(p["external_id"]) if p["status"] == "posted" and p["external_id"] else None
            kept = f' &middot; people watched {pct:g}% of it' if pct else ""
            lines.append(f'<li><b>{e(name)}</b>: {e(label)}{when}{views}{kept}{link}</li>')
        out.append(
            f'<div class="card"><b>{e(c["title"])}</b>'
            f'<div class="muted">{e(names.get(c["campaign_id"], c["campaign_id"] or ""))} &middot; made '
            f'{e(et_text(c["created_at"]))}</div>'
            f'<ul style="margin:6px 0 0 18px;padding:0">{"".join(lines) or "<li>no posts yet</li>"}</ul>'
            f'<div class="row" style="margin-top:6px"><a class="button" href="/clip/{c["id"]}">Open</a></div></div>')
    body = "".join(out) or '<p class="muted">No clips yet.</p>'
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>Clip status</title><style>{_CSS}</style></head><body><main>'
            f'<p><a href="/">&larr; Back to the dashboard</a></p><h1>Clip status</h1>'
            f'<p class="muted">Every clip, newest first. Times are Eastern. Open a clip to act on it.</p>{body}'
            f'</main></body></html>')


def _clip_page(conn, settings: Settings, clip_id: int) -> str | None:
    """The page of one clip: the video, its caption, where it goes, and what you can do with it."""
    c = pipeline.clip_detail(conn, clip_id)
    if c is None:
        return None
    e = html.escape
    camp = next((x for x in campaigns.load(settings.data_dir) if x.id == c["campaign_id"]), None)
    name = camp.name if camp else (c["campaign_id"] or "")
    auto = pipeline.auto_platforms(conn)
    token = e(c["media_token"] or "")
    video = (f'<video controls playsinline preload="metadata" style="width:100%;max-height:70vh;background:#000" '
             f'src="/media/{token}.mp4"></video>' if c["has_video"] else
             '<p class="muted">This clip\'s video has been removed.</p>')
    where = []
    for p in c["posts"]:
        label = POST_STATUS_LABELS.get(p["status"], p["status"])
        when = f' &middot; {e(post_time_label(p["scheduled_at"]))}' if p["status"] == "queued" else ""
        link = f' &middot; {_link(p["url"], "open the post")}' if p["status"] == "posted" and p["url"] else ""
        where.append(f'<li><b>{e(PLATFORM_NAMES.get(p["platform"], p["platform"]))}</b>: {e(label)}{when}{link}</li>')
    actions = []
    if c["has_video"]:
        actions.append(f'<a class="button" href="/media/{token}.mp4?download=1" download="clip-{c["id"]}.mp4">Download</a>')
    review = [p for p in c["posts"] if p["status"] == "review"]
    if review:
        platforms = [p["platform"] for p in review]
        first = min(p["scheduled_at"] for p in review)
        times = "".join(f'<label>{e(PLATFORM_NAMES.get(p, p))} posts at <small>(Eastern time)</small></label>'
                        + _time_box("at", first) for p in platforms if p in auto)
        actions.insert(0, (
            f'<form method="post" action="/review/approve"><input type="hidden" name="id" value="{c["id"]}">'
            f'<input type="hidden" name="choose" value="1"><input type="hidden" name="back" value="/clip/{c["id"]}">'
            f'<div class="checks"><b>Post to:</b> {_platform_checks(platforms)}</div>{times}'
            f'<div class="row" style="margin-top:8px"><button class="primary">Approve</button></div></form>'))
        actions.append(f'<form class="inline" method="post" action="/review/reject" onsubmit="return confirm(\'Reject '
                       f'this clip? It will be deleted.\')"><input type="hidden" name="id" value="{c["id"]}">'
                       f'<input type="hidden" name="back" value="/#clips"><button>Reject</button></form>')
    if c["state"] in ("waiting", "approved"):
        actions.append(_delete_form(c["id"], back="/#clips"))
    posting = _posting_tools(c, camp)
    if c["state"] == "posted":
        actions.append(_hide_form(c["id"]))
    if c["state"] == "posted" and c["has_video"]:
        again = next((x for x in pipeline.repostable(conn) if x["id"] == c["id"]), None)
        for p in (again or {}).get("platforms", []):
            actions.append(f'<form class="inline" method="post" action="/repost" onsubmit="return confirm({_again_question(p)})">'
                           f'<input type="hidden" name="id" '
                           f'value="{c["id"]}"><input type="hidden" name="platform" value="{p}"><input type="hidden" '
                           f'name="back" value="/clip/{c["id"]}"><button>Post again on '
                           f'{e(PLATFORM_NAMES.get(p, p))}</button></form>')
        actions.append(f'<form class="inline" method="post" action="/clip/delete-video" onsubmit="return confirm(\'Delete '
                       f'this video for good? You will not be able to post it again.\')"><input type="hidden" name="id" '
                       f'value="{c["id"]}"><input type="hidden" name="back" value="/#clips"><button>Delete video</button></form>')
    return (CLIP_PAGE.replace("{title}", e(c["title"])).replace("{body}", (
        f'<p><a href="/#clips">&larr; Back to Your clips</a></p><h1>{e(c["title"])}</h1>'
        f'<div class="muted">{e(name)} &middot; {_clip_meta(c)}</div>'
        f'<section>{video}</section>'
        f'<section><h2>Caption</h2><div style="white-space:pre-wrap">{e(c["description"])}</div></section>'
        f'<section><h2>Where it goes</h2><ul>{"".join(where)}</ul></section>'
        + (f'<section>{_checklist_html(camp)}</section>' if camp and c["state"] in ("waiting", "approved") else "")
        + posting +
        f'<section><h2>What do you want to do?</h2><div class="row">{"".join(actions)}</div></section>' + COPY_JS)))


def _review(conn, settings: Settings) -> str:
    """Clips waiting for approval, each with a playable preview."""
    waiting = pipeline.awaiting_review(conn)
    if not waiting:
        return ""
    auto = pipeline.auto_platforms(conn)
    e = html.escape
    names = {c.id: c.name for c in campaigns.load(settings.data_dir)}
    cards = []
    for c in waiting:
        cards.append(
            f'<div class="card"><b>{e(c["title"])}</b>'
            f'<div class="muted">{e(names.get(c["campaign_id"], c["campaign_id"] or "archive"))} &middot; {c["seconds"]:.0f}s &middot; AI score '
            f'{e(str(c["ai_score"] or "?"))}/10</div>'
            + (f'<div>On-screen hook: <b>{e(c["hook_text"])}</b></div>' if c.get("hook_text") else "")
            + f'<video controls playsinline preload="metadata" src="/media/{e(c["media_token"])}.mp4"></video>'
            f'<div class="desc">{e(c["description"])}</div>'
            f'<div class="row"><a class="button" href="/media/{e(c["media_token"])}.mp4?download=1" '
            f'download="clip-{c["id"]}.mp4">Download</a></div>'
            f'<form method="post" action="/review/approve" style="margin-top:8px"><input type="hidden" name="id" '
            f'value="{c["id"]}"><input type="hidden" name="choose" value="1"><div class="checks"><b>Post to:</b> {_platform_checks(c["platforms"].split(", "))}</div>'
            + "".join(f'<label>{e(PLATFORM_NAMES.get(p, p))} posts at <small>(Eastern time)</small></label>'
                      + _time_box("at", c["scheduled_at"]) for p in c["platforms"].split(", ") if p in auto)
            +
            f'<div class="row" style="margin-top:8px"><button class="primary">Approve</button></div></form>'
            f'<div class="row" style="margin-top:8px">'
            f'<form class="inline" method="post" action="/review/reject" onsubmit="return confirm(\'Reject this clip? It will '
            f'be deleted.\')"><input type="hidden" name="id" value="{c["id"]}"><button>Reject</button></form>'
            f'{_delete_form(c["id"])}</div></div>')
    approve_all = ('<form method="post" action="/review/approve-all" style="margin-top:10px"><button>Approve all '
                   f'{len(waiting)}</button></form>' if len(waiting) > 1 else "")
    return (f'<section id="review"><h2>Waiting for your approval ({len(waiting)})</h2>'
            f'<p class="muted">Watch each clip, then Approve it, Reject it, Delete it or Download it (Reject and Delete both remove it). Nothing posts until '
            f'you approve it. Approved clips go out at their scheduled time.</p>'
            f'{"".join(cards)}{approve_all}</section>')


def local_input(scheduled_at: str | None, now: datetime | None = None) -> str:
    """A UTC timestamp from the database as the Eastern-time value a date-time box wants ("2026-11-17T15:30")."""
    now = now or datetime.now(timezone.utc)
    when = datetime.fromisoformat(scheduled_at).replace(tzinfo=timezone.utc) if scheduled_at else now
    return max(when, now).astimezone(ZoneInfo(pipeline.LOCAL_ZONE)).strftime("%Y-%m-%dT%H:%M")


def _time_box(name: str, scheduled_at: str | None) -> str:
    return (f'<input type="datetime-local" name="{name}" value="{local_input(scheduled_at)}" required '
            f'style="font:inherit;padding:9px">')


def _delete_form(clip_id: int, back: str = "") -> str:
    keep = f'<input type="hidden" name="back" value="{html.escape(back)}">' if back else ""
    return (f'<form class="inline" method="post" action="/clip/delete" onsubmit="return confirm(\'Delete this clip? '
            f'It will not be posted.\')"><input type="hidden" name="id" value="{clip_id}">{keep}<button>Delete</button></form>')


def _scheduled(conn, settings: Settings) -> str:
    """Clips the engine posts by itself (YouTube), when, and a button to send one now."""
    posts = pipeline.scheduled_posts(conn)
    if not posts:
        return ""
    e = html.escape
    names = {c.id: c.name for c in campaigns.load(settings.data_dir)}
    rows = []
    for p in posts:
        when = post_time_label(p["scheduled_at"])
        name = PLATFORM_NAMES.get(p["platform"], p["platform"])
        rows.append(
            f'<div class="card"><b>{e(p["title"])}</b>'
            f'<div class="muted">{e(name)} &middot; {e(names.get(p["campaign_id"], p["campaign_id"] or "archive"))}'
            f' &middot; {"posting within 5 minutes" if when == "now" else "posts automatically " + e(when)}</div>'
            + f'<form method="post" action="/post/schedule" style="margin-top:6px"><input type="hidden" name="id" '
              f'value="{p["id"]}"><label>{e(name)} posts at <small>(Eastern time)</small></label>'
              f'<div class="row">{_time_box("at", p["scheduled_at"])}<button class="primary">Set time</button></div></form>'
            + ('' if when == "now" else
               f'<form method="post" action="/post/now" style="margin-top:6px"><input type="hidden" name="id" '
               f'value="{p["id"]}"><button>Post now</button></form>')
            + '<div class="row" style="margin-top:6px">'
            + (f'<a class="button" href="/media/{e(p["media_token"])}.mp4?download=1" download="clip-{p["clip_id"]}.mp4">'
               f'Download</a>' if p["media_token"] else '')
            + f'{_delete_form(p["clip_id"])}</div>'
            + '</div>')
    return (f'<section id="scheduled"><h2>Posting automatically ({len(posts)})</h2>'
            f'<p class="muted">Each one goes out by itself at the time shown. Change the time with Set time, or tap Post '
            f'now to send it sooner.</p>{"".join(rows)}</section>')


def _again_question(platform: str) -> str:
    """The are-you-sure for Post again, as a quoted JavaScript string for an onsubmit attribute."""
    name = PLATFORM_NAMES.get(platform, platform)
    text = (f"Post this clip again on {name}? It goes up publicly within 5 minutes." if platform == "youtube" else
            f"Post this clip again on {name}? It goes back under Ready to post.")
    return html.escape(json.dumps(text))


def _repost(conn, settings: Settings) -> str:
    """Post a recent campaign clip again, for example when a campaign's submit window was missed."""
    clips = pipeline.repostable(conn)
    if not clips:
        return ""
    e = html.escape
    names = {c.id: c.name for c in campaigns.load(settings.data_dir)}
    rows = []
    for c in clips:
        buttons = "".join(
            f'<form method="post" action="/repost" class="inline" onsubmit="return confirm({_again_question(p)})">'
            f'<input type="hidden" name="id" value="{c["id"]}">'
            f'<input type="hidden" name="platform" value="{p}"><button>Post again on {e(PLATFORM_NAMES.get(p, p))}</button></form> '
            for p in c["platforms"])
        rows.append(f'<div class="card"><b>{e(c["title"])}</b>'
                    f'<div class="muted">{e(names.get(c["campaign_id"], c["campaign_id"]))} &middot; '
                    f'{c["left"]} more post{"s" if c["left"] != 1 else ""} allowed</div>'
                    f'<div class="row" style="margin-top:6px">{buttons}'
                    f'<form class="inline" method="post" action="/clip/delete-video" onsubmit="return confirm(\'Delete this '
                    f'video for good? You will not be able to post it again.\')"><input type="hidden" name="id" '
                    f'value="{c["id"]}"><button>Delete video</button></form></div></div>')
    return ('<section id="repost"><h2>Post a clip again</h2>'
            '<p class="muted">For when you missed a campaign\'s submit window (some only take a post submitted within '
            '30 minutes), or lost the post. Tap one and it appears under Ready to post. Leave the earlier post up: '
            f'campaigns can reject removed posts. Clips are kept {pipeline.REPOST_DAYS} days, up to '
            f'{pipeline.MAX_POSTS_PER_CLIP} posts each.</p>' + "".join(rows) + '</section>')


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
    "tiktok": "TikTok app: + → Upload → pick the video. {sound}, then paste the caption. {label}",
    "instagram": "Instagram app: + → Reel → pick the video. {sound}, then paste the caption. {label}",
}
SOUND_OK = "Add a trending sound if you like, keeping it quiet under the speech"
SOUND_NO = "Don't add any sound or music: the campaign wants the original audio only"
LABEL_ON = "If it's a paid campaign, switch on Content disclosure → Paid partnership (TikTok) or add the Paid partnership label (Instagram)."
LABEL_OFF = ("Don't switch on any paid-partnership, branded-content or ad option for this campaign, and don't boost "
             "the post: it bans paid promotion, and the #Ad in the caption is the disclosure. Keep likes visible.")


def app_tip(platform: str, campaign: "campaigns.Campaign | None") -> str:
    """How to post by hand on one app, following the campaign's rules about sound and paid-promotion switches."""
    tip = APP_TIPS.get(platform, "")
    return tip.format(sound=SOUND_OK if campaign is None or campaign.allow_music else SOUND_NO,
                      label=LABEL_ON if campaign is None or campaign.paid_label else LABEL_OFF)


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
    camps = campaigns.by_id(campaigns.load(settings.data_dir))
    names = {i: camp.name for i, camp in camps.items()}
    cards = []
    for c in clips:
        cid = c["id"]
        when = post_time_label(c["scheduled_at"])
        platforms_html = []
        for p in c["posts"]:
            name = PLATFORM_NAMES.get(p["platform"], p["platform"])
            platforms_html.append(
                f'<div class="card"><b>{name}</b><div class="muted">{e(app_tip(p["platform"], camps.get(c["campaign_id"])))}</div>'
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
            f'download="clip-{cid}.mp4">Download video</a>{_delete_form(cid)}</div>'
            f'<p class="muted" style="font-size:13px">iPhone: if the download doesn\'t go to Photos, open the '
            f'video, tap Share, then Save Video.</p>'
            f'<label>Title <small>for YouTube</small></label>'
            f'<div class="row"><input type="text" id="title-{cid}" readonly value="{e(c["title"])}" style="flex:1 1 200px">'
            f'<button type="button" onclick="copyText(\'title-{cid}\', this)">Copy</button></div>'
            + _checklist_html(camps.get(c["campaign_id"])) +
            f'<label>Caption <small>includes the hashtags, required tags and #ad</small></label>'
            f'<textarea id="caption-{cid}" readonly>{e(caption)}</textarea>'
            f'<div class="row"><button type="button" onclick="copyText(\'caption-{cid}\', this)">Copy caption</button></div>'
            f'{"".join(platforms_html)}</div>')
    return (f'<section id="ready"><h2>Ready to post from your phone ({len(clips)})</h2>'
            f'<p class="muted">Download the video, post it in each app with the caption, then paste the post\'s link '
            f'here so the engine can track its views. Unposted clips are dropped after 7 days. Some campaigns only accept a '
            f'post submitted within 30 minutes: post, copy the link, submit it on the campaign page first, then save it here.</p>'
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
        body = (f'{lock_html}<div class="muted">YouTube clips are posted by hand from your phone; nothing is '
                f'uploaded automatically. The connection is only used to read views.</div>{result_html}'
                f'<div class="row" style="margin-top:8px">'
                f'<a class="button" href="/connect/youtube">Reconnect</a>'
                f'<form class="inline" method="post" action="/settings/youtube-disconnect" '
                f'onsubmit="return confirm(\'Cancel the YouTube login? Reading views stops until you reconnect.\')">'
                f'<button>Disconnect and cancel login</button></form></div>'
                f'<div class="muted" style="margin-top:6px">Disconnect cancels the saved login at Google; use it if '
                f'you think someone else got hold of it.</div>')
    return f'<div class="card"><b>YouTube connection:</b> {status}{body}</div>'


def setup_steps(conn, settings: Settings) -> list[tuple[str, bool, str]]:
    """(step, done, how to do it) for everything needed before clips go out."""
    connected = bool(youtube.stored_token(conn, settings))
    decided = conn.execute("SELECT 1 FROM posts WHERE status != 'review' LIMIT 1").fetchone() is not None
    return [
        ("Add your Claude API key", bool(os.environ.get("ANTHROPIC_API_KEY")),
         "Create a key at console.anthropic.com (API Keys) and set a monthly spending limit. In Railway, open "
         "clip-engine, Variables, and add it as ANTHROPIC_API_KEY."),
        ("Connect YouTube", connected, '<a class="button" href="/connect/youtube">Connect YouTube</a>'),
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
        f'<div class="card"><b>Post new clips to:</b><form method="post" action="/settings/post-to">'
        f'<div class="checks" style="margin-top:6px">{_platform_checks(list(PLATFORM_NAMES), pipeline.post_to(conn))}</div>'
        f'<button style="margin-top:8px">Save</button></form>'
        f'<div class="muted">Every new clip is made for each ticked account (if its campaign pays there). You can '
        f'still untick an account on a single clip before approving it.</div></div>'
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
    pending_html = _label_todo(conn) + (_submissions(pending, settings, conn) if pending else '<p class="muted">Nothing waiting.</p>')
    if len(pending) > 1:
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
        library=_library(conn, settings),
        review=_review(conn, settings),
        ready=_ready(conn, settings),
        scheduled=_scheduled(conn, settings),
        repost=_repost(conn, settings),
        settings_html=_settings(conn, settings),
        campaigns=_campaign_rows(settings),
        own_video=_own_video(conn, settings),
        campaign_form=_campaign_form(form_values, editing=bool(form_values and form_values.get("id")),
                                     open_form=bool(form_values) or not campaigns.load(settings.data_dir)),
        status=_status(conn),
        todo=_todo(conn, settings),
        next=_next_task(conn, settings),
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
    """Locks an address out for 15 minutes after 5 different wrong passwords, so the password can't
    be guessed. The same wrong password counts once: after a password change, browsers keep
    resending the old one with every request, and that shouldn't lock the owner out."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failures: dict[str, dict[str, float]] = {}  # address -> hash of wrong password -> when
        self._lock = threading.Lock()

    def _recent(self, client: str) -> dict[str, float]:
        cutoff = self._clock() - LOCKOUT_SECONDS
        return {h: t for h, t in self._failures.get(client, {}).items() if t > cutoff}

    def blocked(self, client: str) -> bool:
        with self._lock:
            return len(self._recent(client)) >= MAX_BAD_PASSWORDS

    def fail(self, client: str, password: str) -> None:
        digest = hashlib.sha256(password.encode()).hexdigest()
        with self._lock:
            self._failures[client] = {**self._recent(client), digest: self._clock()}

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
            throttle.fail(client, password)
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
            if url.path == "/status":
                if not self._authorized():
                    return self._deny()
                conn = db.connect(settings.db_path)
                try:
                    return self._send(200, status_page(conn, settings).encode())
                finally:
                    conn.close()
            clip = CLIP_RE.match(url.path)
            if clip:
                if not self._authorized():
                    return self._deny()
                conn = db.connect(settings.db_path)
                try:
                    page = _clip_page(conn, settings, int(clip.group(1)))
                finally:
                    conn.close()
                if page is None:
                    return self._send(404, CLIP_PAGE.replace("{title}", "Clip").replace("{body}", (
                        '<p><a href="/#clips">&larr; Back to Your clips</a></p><h1>That clip is not here any more</h1>'
                        '<p class="muted">It was rejected, deleted or has expired.</p>')).encode())
                return self._send(200, page.encode())
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
            db.put(conn, "youtube_test_result", "")  # a new login gets tested again
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
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    remaining = end - start + 1
                    while remaining > 0 and (chunk := f.read(min(1024 * 1024, remaining))):
                        self.wfile.write(chunk)
                        remaining -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                self.close_connection = True  # the viewer stopped or skipped the video: nothing went wrong

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
            if getattr(self, "_form_cache", None) is None:  # the body can only be read once per request
                length = min(int(self.headers.get("Content-Length") or 0), 64 * 1024)
                self._form_cache = parse_qs(self.rfile.read(length).decode("utf-8", "replace"), keep_blank_values=True)
            return self._form_cache

        def _redirect(self, location: str, code: int = 303) -> None:
            self.send_response(code)
            self.send_header("Location", location)
            self.end_headers()

        def do_POST(self):
            self._form_cache = None
            if not self._authorized():
                return self._deny()
            if not self._same_origin():
                return self._send(403, b"forbidden", "text/plain")
            if self.path.startswith("/campaigns/"):
                return self._campaign_action()
            if self.path.startswith("/manual-video/upload"):
                return self._upload_video()
            conn = db.connect(settings.db_path)
            try:
                if self.path == "/pause":
                    db.put(conn, "paused", "1")
                elif self.path == "/resume":
                    db.put(conn, "paused", "0")
                elif self.path == "/run":
                    if not os.environ.get("ANTHROPIC_API_KEY"):
                        return self._page(error="Add your Claude API key first (step 1 of Getting started).")
                    reason = pipeline.nothing_to_do(conn, settings)
                    if reason:
                        return self._page(error=reason)
                    was_paused = db.get(conn, "paused") == "1"
                    db.put(conn, "paused", "0")  # asking for clips means run
                    db.put(conn, "run_now", "1")
                    return self._redirect("/?" + urlencode({"notice": (
                        "Resumed. " if was_paused else "") + "Making clips now. They appear at the top of this "
                        "page in 5 to 20 minutes; you can leave and come back."}))
                elif self.path == "/submitted/one":
                    pipeline.mark_submitted(conn, [int(self._form().get("id", ["0"])[0] or 0)])
                elif self.path == "/submitted":
                    pipeline.mark_submitted(conn, [p["id"] for p in pipeline.pending_submissions(conn)])
                elif self.path == "/review/approve":
                    form = self._form()
                    clip_id = int(form.get("id", ["0"])[0] or 0)
                    # Unticked boxes aren't sent, so "choose" marks a form that had them.
                    chosen = form.get("platforms", []) if "choose" in form else None
                    if chosen == []:
                        return self._page(error="Tick at least one account to post to, or tap Reject.")
                    at = None
                    if form.get("at", [""])[0]:
                        try:
                            at = pipeline.parse_local_time(form["at"][0])
                        except ValueError as exc:
                            return self._page(error=str(exc))
                    pipeline.approve(conn, clip_id, chosen, at)
                elif self.path == "/post/schedule":
                    form = self._form()
                    try:
                        when = pipeline.parse_local_time(form.get("at", [""])[0])
                    except ValueError as exc:
                        return self._page(error=str(exc))
                    pipeline.reschedule(conn, int(form.get("id", ["0"])[0] or 0), when)
                    label = post_time_label(when)
                    back = form.get("back", [""])[0]
                    if back.startswith("/clip/") and SAFE_BACK_RE.match(back):
                        return self._redirect(back)  # set from a clip's page: stay on it
                    return self._redirect("/?" + urlencode({"notice": (
                        "It will post within 5 minutes." if label == "now" else f"It will post at {label}.")}) + "#clips")
                elif self.path == "/clip/hide":
                    pipeline.hide_clip(conn, int(self._form().get("id", ["0"])[0] or 0))
                elif self.path == "/clip/delete-video":
                    pipeline.delete_clip_video(conn, int(self._form().get("id", ["0"])[0] or 0))
                elif self.path == "/clip/delete":
                    pipeline.delete_clip(conn, int(self._form().get("id", ["0"])[0] or 0))
                elif self.path == "/review/reject":
                    pipeline.reject(conn, int(self._form().get("id", ["0"])[0] or 0))
                elif self.path == "/manual-video/link":
                    return self._add_video_link(conn)
                elif self.path == "/settings/post-to":
                    chosen = self._form().get("platforms", [])
                    if not chosen:
                        return self._page(error="Tick at least one account to post to.")
                    pipeline.set_post_to(conn, chosen)
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
                        saved = next((p for p in pipeline.pending_submissions(conn) if p["id"] == post_id), None)
                        camp = campaigns.by_id(campaigns.load(settings.data_dir)).get(saved["campaign_id"]) if saved else None
                        label, _ = deadline_label(saved, camp) if saved else ("", True)
                        if label:
                            return self._redirect("/?" + urlencode({"notice": (
                                f"Link saved. Now submit it on the campaign page: {label}.")}) + "#next")
                elif self.path == "/labels/done":
                    youtube.clear_label_todo(conn)
                elif self.path == "/repost":
                    form = self._form()
                    try:
                        status = pipeline.repost(conn, int(form.get("id", ["0"])[0] or 0), form.get("platform", [""])[0])
                    except ValueError as exc:
                        return self._page(error=str(exc))
                    return self._redirect(f"/clip/{int(form.get('id', ['0'])[0] or 0)}")  # its page has what you need next
                elif self.path == "/post/now":
                    pipeline.post_now(conn, int(self._form().get("id", ["0"])[0] or 0))
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
            back = self._form().get("back", [""])[0]  # a page's own buttons send you back to it
            self._redirect(back if SAFE_BACK_RE.match(back) else "/")

        def _live_campaign(self, campaign_id: str):
            return campaigns.by_id(campaigns.live(campaigns.load(settings.data_dir))).get(campaign_id)

        def _add_video_link(self, conn) -> None:
            form = {k: v[0] for k, v in self._form().items()}
            try:
                if self._live_campaign(form.get("campaign", "")) is None:
                    raise manual.UploadError("Pick one of your live campaigns.")
                url = manual.clean_link(form.get("url", ""))
                if policy.watermarked(url):
                    raise manual.UploadError("TikTok, Instagram and Facebook videos carry the app's watermark. "
                                             "Use the original file or a YouTube link.")
            except manual.UploadError as exc:
                return self._page(error=str(exc))
            manual.add(conn, {"kind": "url", "ref": url, "campaign_id": form["campaign"],
                              "creator": manual.clean_text(form.get("creator", "")),
                              "title": manual.clean_text(form.get("title", "")),
                              "count": manual.clean_count(form.get("count", "")),
                              "review": form.get("review") == "on"})
            return self._redirect("/?" + urlencode({"notice": start_now(conn, settings, "Video added.")}))

        def _upload_video(self) -> None:
            """The video arrives as the raw request body, so a big file streams to disk, not memory."""
            query = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}

            def reply(code: int, message: str) -> None:
                self._send(code, json.dumps({"message": message}).encode(), "application/json")
                # an error reply sent before the body was read: don't let the leftover bytes be misread as a new request
                self.close_connection = code != 200

            try:
                if self._live_campaign(query.get("campaign", "")) is None:
                    raise manual.UploadError("Pick one of your live campaigns.")
                batch = manual.clean_batch_id(query.get("batch", ""))
                if not batch and manual.is_image(query.get("name", "")):
                    raise manual.UploadError("Photos have to be made into a video. Choose Make one video from all of "
                                             "them.")
                length = int(self.headers.get("Content-Length") or 0)
                path = manual.save_upload(self.rfile, length, query.get("name", ""), settings.data_dir)
            except manual.UploadError as exc:
                return reply(400, str(exc))
            except Exception:
                log.exception("video upload failed")
                return reply(500, "The upload failed. Try again.")
            conn = db.connect(settings.db_path)
            try:
                if batch:
                    try:
                        manual.add_to_batch(conn, batch, path)
                        if query.get("last") == "1":
                            count = manual.finish_batch(conn, batch, {
                                "campaign_id": query["campaign"], "creator": manual.clean_text(query.get("creator", "")),
                                "title": manual.clean_text(query.get("title", "")),
                                "length": manual.clean_length(query.get("length", "")),
                                "count": manual.clean_count(query.get("count", "")),
                                "review": query.get("review") == "1"})
                            queued = manual.queue(conn)[-1]
                            if queued["kind"] == "file":  # a single video is cut into clips, not combined
                                clips = queued["count"]
                                made = f"cut into {clips} clip{'s' if clips > 1 else ''}"
                            else:
                                videos = queued["count"]
                                made = "made into " + ("one video" if videos == 1 else f"{videos} different videos")
                            return reply(200, start_now(conn, settings, f"{count} file{'s' if count > 1 else ''} "
                                                                        f"uploaded and will be {made}."))
                    except manual.UploadError as exc:
                        return reply(400, str(exc))
                    return reply(200, "File added.")
                manual.add(conn, {"kind": "file", "ref": str(path), "campaign_id": query["campaign"],
                                  "creator": manual.clean_text(query.get("creator", "")),
                                  "title": manual.clean_text(query.get("title", "")) or Path(query["name"]).stem[:60],
                                  "count": manual.clean_count(query.get("count", "")),
                                  "review": query.get("review") == "1"})
                message = start_now(conn, settings, "Video uploaded.")
            finally:
                conn.close()
            reply(200, message)

        def _campaign_action(self) -> None:
            raw = self._form()
            form = {k: v[0] for k, v in raw.items()}
            by_id = {c.id: c for c in campaigns.load(settings.data_dir)}
            if self.path == "/campaigns/save":
                try:
                    campaign = campaigns.from_form(form, raw.get("platforms", []))
                except campaigns.CampaignError as exc:
                    values = {**form, "rate_per_1k_views": form.get("rate", ""), "platforms": raw.get("platforms", []),
                              "allow_music": form.get("allow_music", "off"), "paid_label": form.get("paid_label", "off"), "active": form.get("active", "off")}
                    return self._page(error=str(exc), form_values=values)
                existed = campaign.id in by_id
                campaigns.save(settings.data_dir, campaign)
                done = "updated." if existed else "added."
                if not campaign.sources:
                    done += " Next: upload its video under Add your own video, just below the campaigns."
                elif not existed:
                    done += " Tap Make clips now to start."
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
