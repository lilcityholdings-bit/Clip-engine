"""Small web dashboard (password protected) plus public clip hosting for Instagram.

GET  /                 dashboard (HTTP basic auth, any username + DASHBOARD_PASSWORD)
POST /pause, /resume   stop or restart posting and production
POST /run              start a production cycle on the next tick
POST /submitted        mark all pending campaign links as submitted
GET  /media/<t>.mp4    rendered clip, served under an unguessable token
GET  /health           liveness check for Railway
"""
import base64
import hmac
import html
import json
import logging
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import db, pipeline, strategy
from .config import Settings

log = logging.getLogger(__name__)
MEDIA_RE = re.compile(r"^/media/([0-9a-f]{32})\.mp4$")
DIMENSIONS = {"music": "Music", "campaign": "Campaign", "topic": "Topic", "format": "Format", "length": "Clip length",
              "title_style": "Title style", "post_hour": "Posting time"}
HOURS_ET = {"13": "9am ET", "16": "noon ET", "19": "3pm ET", "22": "6pm ET", "1": "9pm ET"}


def _arm_label(dimension: str, arm: str) -> str:
    if dimension == "post_hour":
        return HOURS_ET.get(arm, f"{arm}:00 UTC")
    if dimension == "length":
        fmt, secs = arm.split(":", 1)
        return f"{fmt} {secs}s"
    return arm.replace("_", " ")

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
</style></head><body><main>
<h1>Clip Engine <span class="{state_class}">&middot; {state}</span></h1>
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
<section><h2>Trending now</h2>{trending}</section>
<section><h2>Earnings by campaign</h2>{earnings}</section>
<section><h2>What's working (higher score = more views and earnings)</h2>{strategy}</section>
<section><h2>Recent clips</h2>{clips}</section>
<section><h2>Recent problems</h2>{errors}</section>
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


def render(conn, settings: Settings) -> str:
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
    return PAGE.format(
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


def make_handler(settings: Settings):
    media_dir = settings.data_dir / "clips"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def _authorized(self) -> bool:
            if not settings.dashboard_password:
                return False
            header = self.headers.get("Authorization", "")
            if not header.startswith("Basic "):
                return False
            try:
                _, _, password = base64.b64decode(header[6:]).decode().partition(":")
            except Exception:
                return False
            return hmac.compare_digest(password, settings.dashboard_password)

        def _deny(self) -> None:
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
            match = MEDIA_RE.match(self.path)
            if match:
                path = media_dir / f"{match.group(1)}.mp4"
                if not path.exists():
                    return self._send(404, b"not found", "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(path.stat().st_size))
                self.end_headers()
                with open(path, "rb") as f:
                    while chunk := f.read(1024 * 1024):
                        self.wfile.write(chunk)
                return
            if self.path != "/":
                return self._send(404, b"not found", "text/plain")
            if not self._authorized():
                return self._deny()
            conn = db.connect(settings.db_path)
            try:
                self._send(200, render(conn, settings).encode())
            finally:
                conn.close()

        def do_POST(self):
            if not self._authorized():
                return self._deny()
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
                else:
                    return self._send(404, b"not found", "text/plain")
            finally:
                conn.close()
            self.send_response(303)
            self.send_header("Location", "/")
            self.end_headers()

    return Handler


def start(settings: Settings) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("0.0.0.0", settings.port), make_handler(settings))
    threading.Thread(target=server.serve_forever, daemon=True, name="dashboard").start()
    log.info("dashboard listening on port %s", settings.port)
    return server
