"""One production cycle (find → clip → upload) and one scoring cycle (views → strategy)."""
import json
import logging
import shutil
import sqlite3
from datetime import date, datetime, timedelta

import anthropic

from . import ai, media, sources, strategy
from .config import Settings
from .db import uploads_today
from .youtube import YouTube

log = logging.getLogger(__name__)


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


def candidate_topics(conn: sqlite3.Connection, claude: anthropic.Anthropic, yt: YouTube) -> list[str]:
    best = [r["arm"] for r in strategy.leaderboard(conn) if r["dimension"] == "topic"][:5]
    try:
        trending = yt.trending_titles()
    except Exception:
        log.exception("could not load trending videos; using evergreen topics")
        trending = []
    topics = ai.trend_topics(claude, trending, best)
    return list(dict.fromkeys(topics + best))


def find_source(conn: sqlite3.Connection, topic: str) -> sources.Source | None:
    for identifier in sources.search(topic):
        if conn.execute("SELECT 1 FROM sources WHERE identifier = ?", (identifier,)).fetchone():
            continue
        try:
            src = sources.fetch(identifier)
        except Exception:
            log.exception("metadata failed for %s", identifier)
            continue
        if src:
            return src
    return None


def produce(conn: sqlite3.Connection, settings: Settings, claude: anthropic.Anthropic, yt: YouTube,
            max_clips: int) -> int:
    """Run one production cycle. Returns the number of videos uploaded."""
    remaining = min(max_clips, settings.max_uploads_per_day - uploads_today(conn))
    if remaining <= 0:
        log.info("daily upload limit reached")
        return 0

    topics = candidate_topics(conn, claude, yt)
    arms = strategy.plan(conn, topics)
    src = find_source(conn, arms["topic"])
    for fallback in ai.EVERGREEN_TOPICS:
        if src:
            break
        log.info("no new licensed video for %r, trying %r", arms["topic"], fallback)
        arms["topic"] = fallback
        src = find_source(conn, fallback)
    if not src:
        log.warning("no usable source found")
        return 0
    log.info("plan %s using %s (%s)", arms, src.identifier, src.license_url)

    conn.execute(
        "INSERT INTO sources (identifier, title, creator, license_url, source_url, topic) VALUES (?, ?, ?, ?, ?, ?)",
        (src.identifier, src.title, src.creator, src.license_url, src.page_url, arms["topic"]),
    )
    conn.commit()

    work = settings.work_dir / src.identifier
    uploaded = 0
    try:
        video = media.download(src.video_url, work / "source.mp4")
        segments, words = media.transcribe(video, settings.whisper_model)
        if not segments:
            log.warning("no speech found in %s", src.identifier)
            return 0
        lo, hi = strategy.length_range(arms)
        clips = ai.pick_clips(
            claude, segments, source_title=src.title, fmt=arms["format"],
            min_sec=lo, max_sec=hi, title_style=arms["title_style"], count=remaining,
        )
        for i, clip in enumerate(clips):
            out = media.render(
                video, work / f"clip{i}.mp4", clip["start"], clip["end"], arms["format"],
                media.captions_ass(words, clip["start"], clip["end"], arms["format"]),
            )
            description = f"{clip['description']}\n\n{attribution(src, arms['format'])}"
            video_id = yt.upload(out, clip["title"], description, clip["tags"])
            conn.execute(
                "INSERT INTO uploads (video_id, source_identifier, start_sec, end_sec, title, arms)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (video_id, src.identifier, clip["start"], clip["end"], clip["title"], json.dumps(arms)),
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
        "SELECT video_id, arms, uploaded_at FROM uploads WHERE scored_at IS NULL AND uploaded_at <= ?",
        (cutoff,),
    ).fetchall()
    if not rows:
        return 0
    since = min(date.fromisoformat(r["uploaded_at"][:10]) for r in rows)
    perf = yt.performance([r["video_id"] for r in rows], since)
    for r in rows:
        views, subs = perf.get(r["video_id"], (0, 0))
        strategy.record(conn, r["arms"], strategy.reward(views, subs))
        conn.execute(
            "UPDATE uploads SET views = ?, subscribers_gained = ?, scored_at = datetime('now') WHERE video_id = ?",
            (views, subs, r["video_id"]),
        )
    conn.commit()
    return len(rows)
