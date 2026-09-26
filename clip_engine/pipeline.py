"""One production cycle (find → clip → upload) and one scoring cycle (views → strategy).

Two source modes:
- campaign mode (any live clipping campaigns configured): clip the newest videos
  of creators who pay per view, and learn which campaigns earn the most.
- archive mode (no campaigns): clip public-domain videos from the Internet Archive.
"""
import hashlib
import json
import logging
import shutil
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import anthropic

from . import ai, campaigns, media, sources, strategy
from .config import Settings
from .db import uploads_today
from .youtube import YouTube

log = logging.getLogger(__name__)
MIN_CAMPAIGN_VIDEO_SEC = 5 * 60


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

def candidate_topics(conn: sqlite3.Connection, claude: anthropic.Anthropic, yt: YouTube) -> list[str]:
    best = [r["arm"] for r in strategy.leaderboard(conn) if r["dimension"] == "topic"][:5]
    try:
        trending = yt.trending_titles()
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


def archive_job(conn: sqlite3.Connection, claude: anthropic.Anthropic, yt: YouTube) -> Job | None:
    arms = strategy.plan(conn, candidate_topics(conn, claude, yt))
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


def campaign_job(conn: sqlite3.Connection, live: list[campaigns.Campaign]) -> Job | None:
    remaining = list(live)
    while remaining:
        ids = [c.id for c in remaining]
        campaign = campaigns.by_id(remaining)[strategy.choose(conn, "campaign", ids)]
        formats = [f for f in campaign.formats if f in strategy.FORMATS] or ["short"]
        arms = strategy.plan(conn, [campaign.id], subject_dim="campaign", formats=formats)
        src = find_campaign_source(conn, campaign)
        if src:
            return Job(arms, src, campaigns.caption_lines(campaign, src, arms["format"]), campaign)
        log.info("campaign %s has no new videos", campaign.id)
        remaining.remove(campaign)
    return None


# --- shared ----------------------------------------------------------------

def produce(conn: sqlite3.Connection, settings: Settings, claude: anthropic.Anthropic, yt: YouTube,
            max_clips: int) -> int:
    """Run one production cycle. Returns the number of videos uploaded."""
    remaining = min(max_clips, settings.max_uploads_per_day - uploads_today(conn))
    if remaining <= 0:
        log.info("daily upload limit reached")
        return 0

    live = campaigns.live(campaigns.load(settings.data_dir))
    job = campaign_job(conn, live) if live else archive_job(conn, claude, yt)
    if not job:
        log.warning("no usable source found")
        return 0
    src, arms, campaign = job.src, job.arms, job.campaign
    log.info("plan %s using %s", arms, src.identifier)

    conn.execute(
        "INSERT INTO sources (identifier, title, creator, license_url, source_url, topic) VALUES (?, ?, ?, ?, ?, ?)",
        (src.identifier, src.title, src.creator, src.license_url, src.page_url,
         arms.get("topic") or arms.get("campaign")),
    )
    conn.commit()

    work = settings.work_dir / hashlib.sha1(src.identifier.encode()).hexdigest()[:16]
    uploaded = 0
    try:
        if campaign:
            video = campaigns.download(src.video_url, work / "source.mp4")
        else:
            video = media.download(src.video_url, work / "source.mp4")
        segments, words = media.transcribe(video, settings.whisper_model)
        if not segments:
            log.warning("no speech found in %s", src.identifier)
            return 0
        lo, hi = strategy.length_range(arms)
        clips = ai.pick_clips(
            claude, segments, source_title=src.title, fmt=arms["format"],
            min_sec=lo, max_sec=hi, title_style=arms["title_style"], count=remaining,
            rules=campaign.rules if campaign else "",
        )
        for i, clip in enumerate(clips):
            out = media.render(
                video, work / f"clip{i}.mp4", clip["start"], clip["end"], arms["format"],
                media.captions_ass(words, clip["start"], clip["end"], arms["format"]),
            )
            description = f"{clip['description']}\n\n{job.footer}"
            video_id = yt.upload(out, clip["title"], description, clip["tags"])
            conn.execute(
                "INSERT INTO uploads (video_id, source_identifier, start_sec, end_sec, title, arms, campaign_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (video_id, src.identifier, clip["start"], clip["end"], clip["title"], json.dumps(arms),
                 campaign.id if campaign else None),
            )
            conn.commit()
            uploaded += 1
            log.info("uploaded %s: %s", video_id, clip["title"])
    finally:
        conn.execute("UPDATE sources SET processed_at = datetime('now') WHERE identifier = ?", (src.identifier,))
        conn.commit()
        shutil.rmtree(work, ignore_errors=True)
    return uploaded


def score(conn: sqlite3.Connection, settings: Settings, yt: YouTube) -> int:
    """Turn performance of videos old enough to judge into strategy updates."""
    cutoff = (datetime.utcnow() - timedelta(hours=settings.score_after_hours)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT video_id, arms, campaign_id, uploaded_at FROM uploads WHERE scored_at IS NULL AND uploaded_at <= ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0
    rates = {c.id: c.rate_per_1k_views for c in campaigns.load(settings.data_dir)}
    since = min(date.fromisoformat(r["uploaded_at"][:10]) for r in rows)
    perf = yt.performance([r["video_id"] for r in rows], since)
    for r in rows:
        views, subs = perf.get(r["video_id"], (0, 0))
        rate = rates.get(r["campaign_id"]) if r["campaign_id"] else None
        strategy.record(conn, r["arms"], strategy.reward(views, subs, rate))
        conn.execute(
            "UPDATE uploads SET views = ?, subscribers_gained = ?, scored_at = datetime('now') WHERE video_id = ?",
            (views, subs, r["video_id"]),
        )
    conn.commit()
    return len(rows)


def earnings(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    """Estimated payout per campaign, from the latest scored view counts."""
    rates = {c.id: c.rate_per_1k_views for c in campaigns.load(settings.data_dir)}
    rows = conn.execute(
        "SELECT campaign_id, COUNT(*) AS posts, COALESCE(SUM(views), 0) AS views FROM uploads"
        " WHERE campaign_id IS NOT NULL GROUP BY campaign_id ORDER BY views DESC"
    ).fetchall()
    return [
        {"campaign": r["campaign_id"], "posts": r["posts"], "views": r["views"],
         "estimated_usd": round(r["views"] * rates.get(r["campaign_id"], 0) / 1000, 2)}
        for r in rows
    ]


def pending_submissions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT campaign_id, video_id, title, uploaded_at FROM uploads"
        " WHERE campaign_id IS NOT NULL AND submitted_at IS NULL ORDER BY campaign_id, uploaded_at"
    ).fetchall()
    return [{**dict(r), "url": f"https://youtu.be/{r['video_id']}"} for r in rows]


def send_digest(conn: sqlite3.Connection, webhook_url: str, http=None) -> int:
    """Post links awaiting submission to a Discord/Slack-style webhook, then mark them sent.

    Whop has no API for submitting clips, so the day's links go to your phone
    ready to paste into each campaign.
    """
    pending = pending_submissions(conn)
    if not pending or not webhook_url:
        return 0
    lines = ["New clips to submit:"] + [f"[{p['campaign_id']}] {p['url']}" for p in pending]
    body = "\n".join(lines)
    import requests  # local import keeps the module light for tests

    resp = (http or requests).post(webhook_url, json={"content": body[:1900], "text": body}, timeout=30)
    resp.raise_for_status()
    mark_submitted(conn, [p["video_id"] for p in pending])
    return len(pending)


def mark_submitted(conn: sqlite3.Connection, video_ids: list[str]) -> None:
    conn.executemany("UPDATE uploads SET submitted_at = datetime('now') WHERE video_id = ?",
                     [(v,) for v in video_ids])
    conn.commit()
