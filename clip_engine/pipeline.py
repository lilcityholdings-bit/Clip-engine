"""One production cycle (find → clip → upload) and one scoring cycle (views → strategy).

Two source modes:
- campaign mode (any live clipping campaigns configured): clip the newest videos
  of creators who pay per view, and learn which campaigns earn the most.
- archive mode (no campaigns): clip public-domain videos from the Internet Archive.
"""
import hashlib
import json
import logging
import secrets
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import anthropic

from . import ai, campaigns, db, media, moments, sources, strategy, trends
from .config import Settings
from .db import clips_today, posts_today
from .platforms import PublishError, is_manual, next_slot
from .youtube import YouTube

log = logging.getLogger(__name__)
MIN_CAMPAIGN_VIDEO_SEC = 5 * 60
MAX_ATTEMPTS = 3


@dataclass
class Job:
    arms: dict[str, str]
    src: sources.Source
    footer: str                 # appended to every clip's description
    campaign: campaigns.Campaign | None = None


def attribution(src: sources.Source, fmt: str) -> str:
    lines = [
        f'Clip from "{src.title}" by {src.creator}.',
        f"Source: {src.page_url}",
        f"License: {sources.license_name(src.license_url)} ({src.license_url})",
        "Changes: trimmed, reformatted and captioned.",
    ]
    if "by-sa" in src.license_url.lower():
        lines.append("This clip is shared under the same license.")
    if fmt == "short":
        lines.append("#Shorts")
    return "\n".join(lines)


def _seen(conn: sqlite3.Connection, identifier: str) -> bool:
    return conn.execute("SELECT 1 FROM sources WHERE identifier = ?", (identifier,)).fetchone() is not None


# --- archive mode ----------------------------------------------------------

def candidate_topics(conn: sqlite3.Connection, claude: anthropic.Anthropic, yt: YouTube | None) -> list[str]:
    best = [r["arm"] for r in strategy.leaderboard(conn) if r["dimension"] == "topic"][:5]
    try:
        trending = yt.trending_titles() if yt else []
    except Exception:
        log.exception("could not load trending videos; using evergreen topics")
        trending = []
    topics = ai.trend_topics(claude, trending, best)
    return list(dict.fromkeys(topics + best))


def find_archive_source(conn: sqlite3.Connection, topic: str) -> sources.Source | None:
    for identifier in sources.search(topic):
        if _seen(conn, identifier):
            continue
        try:
            src = sources.fetch(identifier)
        except Exception:
            log.exception("metadata failed for %s", identifier)
            continue
        if src:
            return src
    return None


def archive_job(conn: sqlite3.Connection, claude: anthropic.Anthropic, yt: YouTube | None,
                music: list[str] | None = None) -> Job | None:
    arms = strategy.plan(conn, candidate_topics(conn, claude, yt), music=music)
    src = find_archive_source(conn, arms["topic"])
    for fallback in ai.EVERGREEN_TOPICS:
        if src:
            break
        log.info("no new licensed video for %r, trying %r", arms["topic"], fallback)
        arms["topic"] = fallback
        src = find_archive_source(conn, fallback)
    if not src:
        return None
    return Job(arms, src, attribution(src, arms["format"]))


# --- campaign mode ---------------------------------------------------------

def find_campaign_source(conn: sqlite3.Connection, campaign: campaigns.Campaign) -> sources.Source | None:
    """Newest video from the campaign's sources that hasn't been clipped yet."""
    for source_url in campaign.sources:
        try:
            videos = campaigns.latest_videos(source_url)
        except Exception:
            log.exception("could not list %s", source_url)
            continue
        for url in videos:
            if not url or _seen(conn, url):
                continue
            try:
                src = campaigns.describe(campaign, url)
            except Exception:
                log.exception("could not read %s", url)
                continue
            if src.duration >= MIN_CAMPAIGN_VIDEO_SEC:
                return src
    return None


def campaign_job(conn: sqlite3.Connection, live: list[campaigns.Campaign],
                 music: list[str] | None = None) -> Job | None:
    remaining = list(live)
    while remaining:
        ids = [c.id for c in remaining]
        campaign = campaigns.by_id(remaining)[strategy.choose(conn, "campaign", ids)]
        formats = [f for f in campaign.formats if f in strategy.FORMATS] or ["short"]
        arms = strategy.plan(conn, [campaign.id], subject_dim="campaign", formats=formats,
                             music=music if campaign.allow_music else None)
        src = find_campaign_source(conn, campaign)
        if src:
            return Job(arms, src, campaigns.caption_lines(campaign, src, arms["format"]), campaign)
        log.info("campaign %s has no new videos", campaign.id)
        remaining.remove(campaign)
    return None


# --- shared ----------------------------------------------------------------

def track_record(conn: sqlite3.Connection, n: int = 5, min_scored: int = 8) -> str:
    """Best and worst scored clips, for Claude to learn from. Empty until there's enough data."""
    count = conn.execute("SELECT COUNT(*) FROM clips WHERE scored_at IS NOT NULL").fetchone()[0]
    if count < min_scored:
        return ""

    def fmt(rows) -> str:
        return "\n".join(f'- {r["views"]:,} views: title "{r["title"]}", hook "{r["hook_text"] or ""}", '
                         f'{r["end_sec"] - r["start_sec"]:.0f}s' for r in rows)

    query = ("SELECT title, hook_text, views, start_sec, end_sec FROM clips WHERE scored_at IS NOT NULL"
             " ORDER BY views {} LIMIT ?")
    best = conn.execute(query.format("DESC"), (n,)).fetchall()
    worst = conn.execute(query.format("ASC"), (min(n, count - n) if count > n else 0,)).fetchall()
    return f"Top performers:\n{fmt(best)}\nWorst performers:\n{fmt(worst)}"


def niche_of(campaign: campaigns.Campaign | None, arms: dict) -> str:
    if campaign:
        return campaign.niche or campaign.name
    return arms.get("topic", "education")


def compose_description(clip: dict, footer: str) -> str:
    """Clip description, then its hashtags, then credit and disclosure."""
    tags = " ".join(t if t.startswith("#") else f"#{t}" for t in clip.get("hashtags", [])[:5]
                    if t.strip("# ") and " " not in t.strip())
    return "\n\n".join(part for part in (clip["description"], tags, footer) if part)


# New accounts that suddenly post a lot look like spam, so output ramps up:
# (days since the first clip, clips per day). After the last step, MAX_CLIPS_PER_DAY applies.
RAMP = [(7, 2), (14, 4)]


def daily_clip_limit(conn: sqlite3.Connection, settings: Settings) -> int:
    if db.get(conn, "ramp", "on") != "on":
        return settings.max_clips_per_day
    first = conn.execute("SELECT MIN(created_at) FROM clips").fetchone()[0]
    days = (datetime.utcnow() - datetime.fromisoformat(first)).days if first else 0
    for until_day, limit in RAMP:
        if days < until_day:
            return min(limit, settings.max_clips_per_day)
    return settings.max_clips_per_day


def review_mode(conn: sqlite3.Connection) -> bool:
    """When on (the default), new clips wait on the dashboard until approved."""
    return db.get(conn, "review_mode", "on") == "on"


def awaiting_review(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT c.id, c.title, c.description, c.campaign_id, c.media_token, c.ai_score, c.hook_text,"
        " c.end_sec - c.start_sec AS seconds, GROUP_CONCAT(p.platform, ', ') AS platforms"
        " FROM clips c JOIN posts p ON p.clip_id = c.id WHERE p.status = 'review'"
        " GROUP BY c.id ORDER BY c.id").fetchall()
    return [dict(r) for r in rows]


def auto_platforms(conn: sqlite3.Connection) -> set[str]:
    """Platforms connected for automatic posting (recorded at startup); the rest are posted by hand."""
    return set(filter(None, (db.get(conn, "auto_platforms") or "").split(",")))


def approve(conn: sqlite3.Connection, clip_id: int) -> None:
    """Approve a reviewed clip. Automatic platforms are queued (keeping the scheduled slot if it's
    still ahead); phone-posted platforms move to "Ready to post"."""
    auto = auto_platforms(conn)
    for post_id, platform in conn.execute("SELECT id, platform FROM posts WHERE clip_id = ? AND status = 'review'",
                                          (clip_id,)).fetchall():
        if platform in auto:
            conn.execute("UPDATE posts SET status = 'queued', scheduled_at = MAX(scheduled_at, datetime('now'))"
                         " WHERE id = ?", (post_id,))
        else:
            conn.execute("UPDATE posts SET status = 'ready' WHERE id = ?", (post_id,))
    conn.commit()


def ready_to_post(conn: sqlite3.Connection) -> list[dict]:
    """Approved clips waiting for you to post them from your phone, with their open posts."""
    rows = conn.execute(
        "SELECT c.id, c.title, c.description, c.campaign_id, c.media_token, c.hook_text, p.id AS post_id,"
        " p.platform, p.scheduled_at FROM clips c JOIN posts p ON p.clip_id = c.id WHERE p.status = 'ready'"
        " ORDER BY p.scheduled_at, c.id, p.platform").fetchall()
    clips: dict[int, dict] = {}
    for r in rows:
        clip = clips.setdefault(r["id"], {k: r[k] for k in ("id", "title", "description", "campaign_id",
                                                              "media_token", "hook_text", "scheduled_at")} | {"posts": []})
        clip["posts"].append({"id": r["post_id"], "platform": r["platform"]})
    return list(clips.values())


def save_manual_post(conn: sqlite3.Connection, post_id: int, url: str) -> None:
    """Record the link of a clip you posted yourself; views are tracked from it."""
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("Paste the full link to your post (starting with https://).")
    conn.execute("UPDATE posts SET status = 'posted', url = ?, external_id = ?, posted_at = datetime('now'),"
                 " error = NULL WHERE id = ? AND status = 'ready'", (url, url, post_id))
    conn.commit()
    cleanup_files(conn)


def skip_manual_post(conn: sqlite3.Connection, post_id: int) -> None:
    conn.execute("UPDATE posts SET status = 'skipped' WHERE id = ? AND status = 'ready'", (post_id,))
    conn.commit()
    cleanup_files(conn)


def expire_stale(conn: sqlite3.Connection, days: int = 7) -> None:
    """Clips nobody posted within a week are dropped, so old files don't fill the disk."""
    conn.execute("UPDATE posts SET status = 'expired' WHERE status IN ('ready', 'review')"
                 " AND scheduled_at < datetime('now', ?)", (f"-{days} days",))
    conn.commit()
    cleanup_files(conn)


def reject(conn: sqlite3.Connection, clip_id: int) -> None:
    conn.execute("UPDATE posts SET status = 'rejected' WHERE clip_id = ? AND status = 'review'", (clip_id,))
    conn.commit()
    cleanup_files(conn)


def snap_to_words(clip: dict, words: list[dict]) -> dict:
    """Start exactly on the first spoken word and end just after the last, so there's no dead air."""
    inside = [w for w in words if w["start"] >= clip["start"] - 0.3 and w["end"] <= clip["end"] + 0.3]
    if inside:
        clip["start"] = max(0.0, inside[0]["start"] - 0.05)
        clip["end"] = inside[-1]["end"] + 0.25
    return clip


def produce(conn: sqlite3.Connection, settings: Settings, claude: anthropic.Anthropic, yt: YouTube | None,
            publishers: dict, max_clips: int) -> int:
    """Find a source, cut the best clips and queue them on every enabled platform. Returns clips queued."""
    if not publishers:
        log.warning("no publishing platform is configured")
        return 0
    remaining = min(max_clips, daily_clip_limit(conn, settings) - clips_today(conn))
    if remaining <= 0:
        log.info("daily clip limit reached")
        return 0

    live = campaigns.live(campaigns.load(settings.data_dir))
    library = media.music_library(settings.data_dir)
    tracks = sorted(library)
    job = campaign_job(conn, live, tracks) if live else archive_job(conn, claude, yt, tracks)
    if not job:
        log.warning("no usable source found")
        return 0
    src, arms, campaign = job.src, job.arms, job.campaign
    log.info("plan %s using %s", arms, src.identifier)

    key = hashlib.sha1(src.identifier.encode()).hexdigest()[:16]
    work = settings.work_dir / key
    out_dir = settings.data_dir / "clips"
    platforms = [p for p in (campaign.platforms if campaign else publishers) if p in publishers]
    if not platforms:
        log.warning("campaign %s pays on %s, none of which are connected", campaign.id, campaign.platforms)
        return 0
    conn.execute(
        "INSERT INTO sources (identifier, title, creator, license_url, source_url, topic) VALUES (?, ?, ?, ?, ?, ?)",
        (src.identifier, src.title, src.creator, src.license_url, src.page_url,
         arms.get("topic") or arms.get("campaign")),
    )
    conn.commit()

    slot = next_slot(int(arms["post_hour"])).strftime("%Y-%m-%d %H:%M:%S")
    queued = 0
    try:
        if campaign:
            video = campaigns.download(src.video_url, work / "source.mp4")
        else:
            video = media.download(src.video_url, work / "source.mp4")
        # Go straight to the most replayed parts when YouTube has that data.
        windows = moments.windows(src.heatmap, src.duration)
        segments, words = media.transcribe(video, settings.whisper_model, windows or None)
        moments.mark_replayed(segments, src.heatmap)
        if not segments:
            log.warning("no speech found in %s", src.identifier)
            return 0
        lo, hi = strategy.length_range(arms)
        clips = ai.pick_clips(
            claude, segments, source_title=src.title, fmt=arms["format"],
            min_sec=lo, max_sec=hi, title_style=arms["title_style"], count=remaining,
            rules=campaign.rules if campaign else "",
            track_record=track_record(conn),
            trends=trends.as_prompt(trends.brief(conn, claude, niche_of(campaign, arms), settings.trend_region)),
        )
        for i, clip in enumerate(clips):
            if not moments.same_window(clip, segments):
                log.info("skipping clip that crosses a transcript gap: %s", clip["title"])
                continue
            clip = snap_to_words(clip, words)
            if arms["format"] == "short":
                clip["end"] = min(clip["end"], clip["start"] + 59)
            token = secrets.token_hex(16)
            frame = media.plan_layout(video, clip["start"], clip["end"]) if arms["format"] == "short" else None
            out = media.render(
                video, out_dir / f"{token}.mp4", clip["start"], clip["end"], arms["format"],
                media.captions_ass(words, clip["start"], clip["end"], arms["format"], clip.get("hook_text", ""),
                                   center=bool(frame and frame.kind == "stack")),
                frame=frame,
                music=library.get(arms.get("music", "none")),
            )
            cur = conn.execute(
                "INSERT INTO clips (source_identifier, campaign_id, start_sec, end_sec, title, description, tags,"
                " arms, file_path, media_token, ai_score, hook_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (src.identifier, campaign.id if campaign else None, clip["start"], clip["end"], clip["title"],
                 compose_description(clip, job.footer), json.dumps(clip["tags"]), json.dumps(arms),
                 str(out), token, clip.get("score"), clip.get("hook_text", "")),
            )
            reviewing = review_mode(conn)
            for platform in platforms:
                manual = is_manual(publishers.get(platform))
                status = "review" if reviewing else ("ready" if manual else "queued")
                conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, ?, ?, ?)",
                             (cur.lastrowid, platform, status, slot))
            conn.commit()
            queued += 1
            log.info("queued clip %s for %s at %s: %s", cur.lastrowid, platforms, slot, clip["title"])
    finally:
        conn.execute("UPDATE sources SET processed_at = datetime('now') WHERE identifier = ?", (src.identifier,))
        conn.commit()
        shutil.rmtree(work, ignore_errors=True)
    return queued


def publish_due(conn: sqlite3.Connection, settings: Settings, publishers: dict) -> int:
    """Post every queued clip whose time has come. Returns the number posted."""
    due = conn.execute(
        "SELECT p.id AS post_id, p.platform, p.attempts, c.id AS clip_id, c.title, c.description, c.tags,"
        " c.campaign_id, c.file_path, c.media_token, c.arms FROM posts p JOIN clips c ON c.id = p.clip_id"
        " WHERE p.status = 'queued' AND p.scheduled_at <= datetime('now') ORDER BY p.scheduled_at"
    ).fetchall()
    posted = 0
    for row in due:
        pub = publishers.get(row["platform"])
        if pub is None:
            continue
        if posts_today(conn, row["platform"]) >= pub.daily_limit:
            continue
        path = Path(row["file_path"] or "")
        public_url = f"{settings.public_base_url}/media/{row['media_token']}.mp4" if settings.public_base_url else ""
        try:
            if not path.exists():
                raise PublishError("rendered file is missing")
            external_id, url = pub.publish(dict(row), path, public_url)
        except Exception as exc:
            attempts = row["attempts"] + 1
            status = "failed" if attempts >= MAX_ATTEMPTS else "queued"
            # Retry later: push the slot back an hour.
            conn.execute("UPDATE posts SET attempts = ?, status = ?, error = ?,"
                         " scheduled_at = datetime('now', '+1 hour') WHERE id = ?",
                         (attempts, status, repr(exc)[:500], row["post_id"]))
            conn.commit()
            log.exception("%s post of clip %s failed (attempt %s)", row["platform"], row["clip_id"], attempts)
            if status == "failed":
                notify(settings.digest_webhook_url,
                       f"clip-engine: {row['platform']} gave up on clip {row['clip_id']} after {attempts} tries: {exc!r}")
            continue
        conn.execute("UPDATE posts SET status = 'posted', external_id = ?, url = ?, posted_at = datetime('now'),"
                     " error = NULL WHERE id = ?", (external_id, url, row["post_id"]))
        conn.commit()
        posted += 1
        log.info("posted clip %s to %s: %s", row["clip_id"], row["platform"], url)
    expire_stale(conn)
    return posted


def cleanup_files(conn: sqlite3.Connection) -> None:
    """Delete rendered files once no post of that clip is still queued."""
    rows = conn.execute(
        "SELECT id, file_path FROM clips WHERE file_path IS NOT NULL AND NOT EXISTS"
        " (SELECT 1 FROM posts WHERE posts.clip_id = clips.id AND posts.status IN ('queued', 'review', 'ready'))"
    ).fetchall()
    for r in rows:
        Path(r["file_path"]).unlink(missing_ok=True)
        conn.execute("UPDATE clips SET file_path = NULL WHERE id = ?", (r["id"],))
    conn.commit()


def score(conn: sqlite3.Connection, settings: Settings, publishers: dict) -> int:
    """Once a clip's posts are old enough, sum views across platforms and update the strategy."""
    cutoff = (datetime.utcnow() - timedelta(hours=settings.score_after_hours)).strftime("%Y-%m-%d %H:%M:%S")
    clips = conn.execute(
        "SELECT id, arms, campaign_id FROM clips WHERE scored_at IS NULL"
        " AND NOT EXISTS (SELECT 1 FROM posts WHERE posts.clip_id = clips.id AND posts.status IN ('queued', 'review', 'ready'))"
        " AND EXISTS (SELECT 1 FROM posts WHERE posts.clip_id = clips.id AND posts.status = 'posted')"
        " AND (SELECT MIN(posted_at) FROM posts WHERE posts.clip_id = clips.id AND status = 'posted') <= ?",
        (cutoff,),
    ).fetchall()
    if not clips:
        return 0
    ids = [c["id"] for c in clips]
    marks = ",".join("?" * len(ids))
    posts = conn.execute(f"SELECT id, clip_id, platform, external_id FROM posts WHERE status = 'posted'"
                         f" AND clip_id IN ({marks})", ids).fetchall()
    stats: dict[int, tuple[int, int]] = {}
    for platform, pub in publishers.items():
        mine = [p for p in posts if p["platform"] == platform]
        if not mine:
            continue
        try:
            got = pub.stats([p["external_id"] for p in mine])
        except Exception:
            log.exception("could not read %s stats", platform)
            continue
        for p in mine:
            views, subs = got.get(p["external_id"], (0, 0))
            conn.execute("UPDATE posts SET views = ? WHERE id = ?", (views, p["id"]))
            total = stats.get(p["clip_id"], (0, 0))
            stats[p["clip_id"]] = (total[0] + views, total[1] + subs)
    rates = {c.id: c.rate_per_1k_views for c in campaigns.load(settings.data_dir)}
    for c in clips:
        views, subs = stats.get(c["id"], (0, 0))
        rate = rates.get(c["campaign_id"]) if c["campaign_id"] else None
        strategy.record(conn, c["arms"], strategy.reward(views, subs, rate))
        conn.execute("UPDATE clips SET views = ?, subscribers_gained = ?, scored_at = datetime('now') WHERE id = ?",
                     (views, subs, c["id"]))
    conn.commit()
    return len(clips)


def refresh_views(conn: sqlite3.Connection, publishers: dict, days: int = 30) -> None:
    """Keep view counts on recent posts current for the dashboard and earnings."""
    posts = conn.execute("SELECT id, platform, external_id, posted_at >= datetime('now', '-3 days') AS fresh"
                         " FROM posts WHERE status = 'posted' AND posted_at >= datetime('now', ?)",
                         (f"-{days} days",)).fetchall()
    for platform, pub in publishers.items():
        # Phone posts are read from public pages one by one, so only the newest are refreshed hourly.
        mine = [p for p in posts if p["platform"] == platform and (p["fresh"] or not is_manual(pub))]
        if not mine:
            continue
        try:
            got = pub.stats([p["external_id"] for p in mine])
        except Exception:
            log.exception("could not refresh %s views", platform)
            continue
        for p in mine:
            conn.execute("UPDATE posts SET views = ? WHERE id = ?", (got.get(p["external_id"], (0, 0))[0], p["id"]))
    conn.commit()


def earnings(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """Estimated payout per campaign, from the latest view counts on every platform."""
    rates = {c.id: c.rate_per_1k_views for c in campaigns.load(settings.data_dir)}
    rows = conn.execute(
        "SELECT c.campaign_id, COUNT(DISTINCT c.id) AS clips, COUNT(p.id) AS posts,"
        " COALESCE(SUM(p.views), 0) AS views FROM clips c JOIN posts p ON p.clip_id = c.id"
        " WHERE c.campaign_id IS NOT NULL AND p.status = 'posted' GROUP BY c.campaign_id ORDER BY views DESC"
    ).fetchall()
    return [
        {"campaign": r["campaign_id"], "clips": r["clips"], "posts": r["posts"], "views": r["views"],
         "estimated_usd": round(r["views"] * rates.get(r["campaign_id"], 0) / 1000, 2)}
        for r in rows
    ]


def pending_submissions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT p.id, c.campaign_id, p.platform, p.url, c.title FROM posts p JOIN clips c ON c.id = p.clip_id"
        " WHERE c.campaign_id IS NOT NULL AND p.status = 'posted' AND p.submitted_at IS NULL AND p.url != ''"
        " ORDER BY c.campaign_id, p.posted_at"
    ).fetchall()
    return [dict(r) for r in rows]


def mark_submitted(conn: sqlite3.Connection, post_ids: list[int]) -> None:
    conn.executemany("UPDATE posts SET submitted_at = datetime('now') WHERE id = ?", [(i,) for i in post_ids])
    conn.commit()


def notify(webhook_url: str, text: str, http=None) -> None:
    """Best-effort message to the Discord/Slack webhook; never raises."""
    if not webhook_url:
        return
    try:
        import requests

        (http or requests).post(webhook_url, json={"content": text[:1900], "text": text}, timeout=30)
    except Exception:
        log.exception("could not send notification")


def send_digest(conn: sqlite3.Connection, webhook_url: str, http=None) -> int:
    """Post links awaiting submission to a Discord/Slack-style webhook, then mark them sent.

    Whop has no API for submitting clips, so new links go to your phone
    ready to paste into each campaign.
    """
    pending = pending_submissions(conn)
    if not pending or not webhook_url:
        return 0
    lines = ["New clips to submit:"] + [f"[{p['campaign_id']}] {p['platform']}: {p['url']}" for p in pending]
    body = "\n".join(lines)
    import requests  # local import keeps the module light for tests

    resp = (http or requests).post(webhook_url, json={"content": body[:1900], "text": body}, timeout=30)
    resp.raise_for_status()
    mark_submitted(conn, [p["id"] for p in pending])
    return len(pending)
