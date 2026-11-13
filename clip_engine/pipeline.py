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

from . import ai, campaigns, db, manual, media, moments, policy, sources, strategy, trends
from .config import Settings
from .db import clips_today, posts_today
from .layout import Layout
from .platforms import ALL_PLATFORMS, PublishError, is_manual, next_slot
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
    item: dict | None = None    # the queued video you handed over, if this job came from one
    whole: bool = False         # a video built from several of your files: it is posted as one clip, uncut


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
            if policy.watermarked(url):
                log.info("skipping %s: videos from that app carry a watermark", url)
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


def manual_job(conn: sqlite3.Connection, live: list[campaigns.Campaign],
               music: list[str] | None = None) -> Job | None:
    """The oldest video you handed over (uploaded or by link) whose campaign is still live."""
    by_id = campaigns.by_id(live)
    while (item := manual.pop(conn)) is not None:
        campaign = by_id.get(item.get("campaign_id"))
        if campaign is None:
            log.info("dropping %s: its campaign isn't live", item)
            manual.discard(item)
            continue
        whole = False
        try:
            if item["kind"] == "project":
                src = _build_project(item, campaign)
                whole = True
            elif item["kind"] == "file":
                path = Path(item["ref"])
                src = sources.Source(
                    identifier=f"upload:{path.stem}", title=item.get("title") or path.stem,
                    creator=item.get("creator") or campaign.name, license_url=campaign.program_url,
                    video_url=str(path), duration=media.duration(path), page=campaign.program_url)
            else:
                if _seen(conn, item["ref"]) or policy.watermarked(item["ref"]):
                    log.info("dropping %s: already clipped or watermarked", item["ref"])
                    continue
                src = campaigns.describe(campaign, item["ref"])
                if item.get("creator"):
                    src.creator = item["creator"]
        except Exception:
            log.exception("could not read the video you added: %s", item)
            manual.discard(item)
            continue
        formats = [f for f in campaign.formats if f in strategy.FORMATS] or ["short"]
        arms = strategy.plan(conn, [campaign.id], subject_dim="campaign", formats=formats,
                             music=music if campaign.allow_music else None)
        if whole:
            arms["format"] = "short"  # the video is built vertical
        return Job(arms, src, campaigns.caption_lines(campaign, src, arms["format"]), campaign, item, whole)
    return None


def _build_project(item: dict, campaign: campaigns.Campaign) -> sources.Source:
    """Join the files you uploaded together (videos and photos) into one vertical video."""
    files = [Path(f) for f in item["files"] if Path(f).exists()]
    if not files:
        raise FileNotFoundError("none of the uploaded files are left")
    stem = Path(files[0]).stem
    work = files[0].parent / f"build-{stem}"
    try:
        built = media.assemble(files, files[0].parent / f"{stem}-combined.mp4", work)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return sources.Source(
        identifier=f"upload:{stem}", title=item.get("title") or campaign.name,
        creator=item.get("creator") or campaign.name, license_url=campaign.program_url,
        video_url=str(built), duration=media.duration(built), page=campaign.program_url)


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


def approved_caption(conn: sqlite3.Connection, campaign: campaigns.Campaign) -> str:
    """The next of the campaign's approved captions, in turn, so consecutive clips don't repeat one."""
    made = conn.execute("SELECT COUNT(*) FROM clips WHERE campaign_id = ?", (campaign.id,)).fetchone()[0]
    return campaign.approved_captions[made % len(campaign.approved_captions)]


def compose_description(clip: dict, footer: str) -> str:
    """Clip description, then its hashtags, then credit and disclosure."""
    tags = " ".join(policy.clean_hashtags(clip.get("hashtags", [])))
    return "\n\n".join(part for part in (clip["description"], tags, footer) if part)


# New accounts that suddenly post a lot look like spam, so output ramps up:
# (days since the first clip, clips per day). After the last step, MAX_CLIPS_PER_DAY applies.
RAMP = [(7, 4)]


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


def post_to(conn: sqlite3.Connection) -> list[str]:
    """Accounts new clips go to, as ticked under Settings (all of them until you change it)."""
    saved = db.get(conn, "post_to")
    chosen = ALL_PLATFORMS if saved is None else saved.split(",")
    return [p for p in ALL_PLATFORMS if p in chosen]


def set_post_to(conn: sqlite3.Connection, chosen: list[str]) -> None:
    db.put(conn, "post_to", ",".join(p for p in ALL_PLATFORMS if p in chosen))


def approve(conn: sqlite3.Connection, clip_id: int, only: list[str] | None = None) -> None:
    """Approve a reviewed clip, on every platform it was made for or only those in `only` (the
    rest are skipped). Automatic platforms are queued (keeping the scheduled slot if it's still
    ahead); phone-posted platforms move to "Ready to post"."""
    auto = auto_platforms(conn)
    for post_id, platform in conn.execute("SELECT id, platform FROM posts WHERE clip_id = ? AND status = 'review'",
                                          (clip_id,)).fetchall():
        if only is not None and platform not in only:
            conn.execute("UPDATE posts SET status = 'skipped' WHERE id = ?", (post_id,))
        elif platform in auto:
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


def scheduled_posts(conn: sqlite3.Connection) -> list[dict]:
    """Approved clips the engine will post by itself, with the platform and when."""
    rows = conn.execute(
        "SELECT p.id, p.platform, p.scheduled_at, c.title, c.campaign_id FROM posts p JOIN clips c ON c.id = p.clip_id"
        " WHERE p.status = 'queued' ORDER BY p.scheduled_at, p.id").fetchall()
    return [dict(r) for r in rows]


def post_now(conn: sqlite3.Connection, post_id: int) -> None:
    """Skip the wait: the next 5-minute check posts it."""
    conn.execute("UPDATE posts SET scheduled_at = datetime('now') WHERE id = ? AND status = 'queued'", (post_id,))
    conn.commit()


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


MAX_UPLOAD_ATTEMPTS = 3
STALE_STATUS_MINUTES = 45


def set_status(conn: sqlite3.Connection, state: str, text: str) -> None:
    """What clip-making is doing, for the dashboard: state is running, done, failed or idle."""
    db.put(conn, "produce_status", json.dumps({"state": state, "text": text, "at": datetime.utcnow().isoformat()}))


def status(conn: sqlite3.Connection) -> dict:
    """The latest status. A "running" that is too old means the process was cut off (e.g. by a deploy)."""
    saved = json.loads(db.get(conn, "produce_status") or "{}")
    if saved.get("state") == "running" and \
            datetime.utcnow() - datetime.fromisoformat(saved["at"]) > timedelta(minutes=STALE_STATUS_MINUTES):
        return {**saved, "state": "failed", "text": "Clip-making was interrupted. Tap Make clips now to try again."}
    return saved


def chain_next(conn: sqlite3.Connection, made: int) -> bool:
    """After a run that made clips, go straight on to the next video you queued instead of waiting hours."""
    if made and manual.queue(conn):
        db.put(conn, "run_now", "1")
        return True
    return False


def _note(conn: sqlite3.Connection, text: str) -> None:
    """Why the last run made no clips, in words for the owner."""
    log.info(text)
    db.put(conn, "produce_note", text)


def nothing_to_do(conn: sqlite3.Connection, settings: Settings) -> str:
    """Why there is no point starting a clip run right now ("" if there is something to clip).
    Checked before the heavy process is started, so idle hours cost nothing."""
    limit = daily_clip_limit(conn, settings)
    if clips_today(conn) >= limit:
        return f"Today's limit of {limit} clips is reached. More can be made tomorrow."
    if manual.queue(conn):
        return ""
    live = campaigns.live(campaigns.load(settings.data_dir))
    if not live:
        return "" if settings.archive_mode else "Add a campaign first, then tap Make clips now."
    if not any(c.sources for c in live):
        return ("Nothing to clip yet. Upload a video under Add your own video, or add the creator's channel "
                "link to the campaign.")
    return ""


def produce(conn: sqlite3.Connection, settings: Settings, claude: anthropic.Anthropic, yt: YouTube | None,
            publishers: dict, max_clips: int) -> int:
    """Find a source, cut the best clips and queue them on every enabled platform. Returns clips queued."""
    db.put(conn, "produce_note", "")
    if not publishers:
        _note(conn, "No posting account is set up.")
        return 0
    left_today = daily_clip_limit(conn, settings) - clips_today(conn)
    remaining = min(max_clips, left_today)
    if remaining <= 0:
        _note(conn, "Today's clip limit is reached. More can be made tomorrow.")
        return 0

    live = campaigns.live(campaigns.load(settings.data_dir))
    library = media.music_library(settings.data_dir)
    tracks = sorted(library)
    if live:
        job = manual_job(conn, live, tracks) or campaign_job(conn, live, tracks)
    else:
        job = archive_job(conn, claude, yt, tracks) if settings.archive_mode else None
    if not job:
        _note(conn, "No new video to clip. Upload one under Add your own video, or wait for the creator's next "
                    "upload." if live else "Add a campaign first.")
        return 0
    if job.item:
        remaining = left_today  # a video you handed over is used once, so take all of today's clips from it
    src, arms, campaign = job.src, job.arms, job.campaign
    log.info("plan %s using %s", arms, src.identifier)

    key = hashlib.sha1(src.identifier.encode()).hexdigest()[:16]
    work = settings.work_dir / key
    out_dir = settings.data_dir / "clips"
    chosen = post_to(conn)
    platforms = [p for p in (campaign.platforms if campaign else publishers) if p in publishers and p in chosen]
    if not platforms:
        _note(conn, "None of this campaign's platforms are ticked under Settings, Post new clips to.")
        if job.item:
            manual.add(conn, job.item, front=True)  # keep your video for when an account is ticked
        return 0
    conn.execute(
        "INSERT OR IGNORE INTO sources (identifier, title, creator, license_url, source_url, topic)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (src.identifier, src.title, src.creator, src.license_url, src.page_url,
         arms.get("topic") or arms.get("campaign")),
    )
    conn.commit()

    slot = next_slot(int(arms["post_hour"])).strftime("%Y-%m-%d %H:%M:%S")
    queued = 0
    finished = False
    try:
        if src.identifier.startswith("upload:"):
            video = Path(src.video_url)  # a file you uploaded: already on the disk
        elif campaign:
            video = campaigns.download(src.video_url, work / "source.mp4")
        else:
            video = media.download(src.video_url, work / "source.mp4")
        # Go straight to the most replayed parts when YouTube has that data.
        windows = moments.windows(src.heatmap, src.duration)
        segments, words = [], []
        if not job.whole and media.has_audio(video):  # a silent file can't be transcribed
            segments, words = media.transcribe(video, settings.whisper_model, windows or None)
        lo, hi = strategy.length_range(arms)
        visual = False
        if job.whole:
            # Built from your own files: the whole video is the clip, never cut up or re-framed.
            visual, recent = True, []
            clips = [{"start": 0.0, "end": min(src.duration, media.COMPILATION_MAX_SEC), "title": src.title,
                      "description": "", "tags": [], "hook_text": "", "hashtags": [], "score": None}]
        elif moments.has_speech(words):
            moments.mark_replayed(segments, src.heatmap)
            recent = policy.recent_titles(conn)
            clips = ai.pick_clips(
                claude, segments, source_title=src.title, fmt=arms["format"],
                min_sec=lo, max_sec=hi, title_style=arms["title_style"], count=remaining,
                rules=campaign.rules if campaign else "",
                track_record=track_record(conn),
                recent_titles=recent,
                trends=trends.as_prompt(trends.brief(conn, claude, niche_of(campaign, arms), settings.trend_region)),
            )
        elif campaign and campaign.approved_captions:
            # Nothing to read, but the caption is dictated anyway: cut the picture along its shot changes.
            visual, recent = True, []
            spans = moments.visual_windows(media.scene_cuts(video), src.duration, min(hi, 58), remaining)
            clips = [{"start": a, "end": b, "title": "", "description": "", "tags": [], "hook_text": "",
                      "hashtags": [], "score": None} for a, b in spans]
            log.info("no speech in %s; cutting %d clips by shot changes", src.identifier, len(clips))
        else:
            _note(conn, f'No speech was found in "{src.title}", so there was nothing to pick clips from. For '
                        "footage without talking, add the campaign's approved captions and upload it again.")
            finished = True
            return 0
        for i, clip in enumerate(clips):
            if not visual and not moments.same_window(clip, segments):
                log.info("skipping clip that crosses a transcript gap: %s", clip["title"])
                continue
            fixed = approved_caption(conn, campaign) if campaign and campaign.approved_captions else ""
            if fixed:
                # The campaign dictates the caption: use it exactly, with no hashtags, tags, links, credit lines
                # or on-screen hook text of our own, and don't treat its repeats as spam.
                clip = {**clip, "title": fixed, "description": fixed, "tags": [], "hashtags": [], "hook_text": ""}
            else:
                twin = policy.near_duplicate(clip["title"], recent)
                if twin:
                    log.info("skipping clip that looks like a repeat of %r: %s", twin, clip["title"])
                    continue
                recent.append(clip["title"])
            clip = snap_to_words(clip, words)
            if arms["format"] == "short":
                clip["end"] = min(clip["end"], clip["start"] + 59)
            token = secrets.token_hex(16)
            if job.whole:
                frame = Layout("blur")
            else:
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
                 fixed or compose_description(clip, job.footer), json.dumps(clip["tags"]), json.dumps(arms),
                 str(out), token, clip.get("score"), clip.get("hook_text", "")),
            )
            reviewing = review_mode(conn)
            for platform in platforms:
                by_hand = is_manual(publishers.get(platform))
                status = "review" if reviewing else ("ready" if by_hand else "queued")
                conn.execute("INSERT INTO posts (clip_id, platform, status, scheduled_at) VALUES (?, ?, ?, ?)",
                             (cur.lastrowid, platform, status, slot))
            conn.commit()
            queued += 1
            log.info("queued clip %s for %s at %s: %s", cur.lastrowid, platforms, slot, clip["title"])
        finished = True
        if not queued:
            _note(conn, f'No moment in "{src.title}" was strong enough to post. Try a different video.')
    finally:
        conn.execute("UPDATE sources SET processed_at = datetime('now') WHERE identifier = ?", (src.identifier,))
        conn.commit()
        shutil.rmtree(work, ignore_errors=True)
        if job.whole:
            Path(src.video_url).unlink(missing_ok=True)  # the combined copy; your own files are kept for a retry
        if job.item and job.item.get("kind") in ("file", "project"):
            attempts = job.item.get("attempts", 0) + 1
            if finished or queued or attempts >= MAX_UPLOAD_ATTEMPTS:
                manual.discard(job.item)  # used (or given up on): free the disk
            else:  # it failed part-way: keep your upload and try it again on the next run
                manual.add(conn, {**job.item, "attempts": attempts}, front=True)
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


PLATFORM_LABELS = {"youtube": "YouTube", "tiktok": "TikTok", "instagram": "Instagram"}


def send_alerts(conn: sqlite3.Connection, webhook_url: str, dashboard_url: str, http=None) -> int:
    """Message your phone once when clips are waiting for approval and once when a clip you post
    yourself is due. Returns how many things were announced."""
    if not webhook_url:
        return 0
    alerted = json.loads(db.get(conn, "alerted", "[]"))
    seen = set(alerted)
    lines, new = [], []
    waiting = conn.execute("SELECT DISTINCT c.id, c.title FROM clips c JOIN posts p ON p.clip_id = c.id"
                           " WHERE p.status = 'review' ORDER BY c.id").fetchall()
    fresh = [r for r in waiting if f"review:{r['id']}" not in seen]
    if fresh:
        lines.append(f"{len(fresh)} new clip{'s' if len(fresh) > 1 else ''} waiting for your approval: "
                     + "; ".join(r["title"] for r in fresh[:5]))
        new += [f"review:{r['id']}" for r in fresh]
    due = conn.execute("SELECT p.id, p.platform, c.title FROM posts p JOIN clips c ON c.id = p.clip_id"
                       " WHERE p.status = 'ready' AND p.scheduled_at <= datetime('now') ORDER BY p.id").fetchall()
    for r in due:
        if f"ready:{r['id']}" not in seen:
            lines.append(f"Time to post on {PLATFORM_LABELS.get(r['platform'], r['platform'])}: {r['title']}")
            new.append(f"ready:{r['id']}")
    if not lines:
        return 0
    if dashboard_url:
        lines.append(f"Open the dashboard: {dashboard_url}")
    body = "\n".join(lines)
    import requests  # local import keeps the module light for tests

    resp = (http or requests).post(webhook_url, json={"content": body[:1900], "text": body}, timeout=30)
    resp.raise_for_status()
    db.put(conn, "alerted", json.dumps((alerted + new)[-500:]))
    return len(new)


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
