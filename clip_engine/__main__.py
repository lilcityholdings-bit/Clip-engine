"""Entry point.

python -m clip_engine run        # run forever: dashboard + scheduler (what Railway runs)
python -m clip_engine produce    # one production cycle
python -m clip_engine publish    # post everything that's due
python -m clip_engine score      # one scoring cycle
python -m clip_engine report     # what the strategy has learned, earnings, links to submit
"""
import logging
import os
import subprocess
import sys
import time

import anthropic
import requests

from . import dashboard, db, pipeline, platforms, strategy
from .config import Settings
from . import youtube
from .youtube import YouTube

TICK_MINUTES = 5
# While clips are being made, touch the network this often. Railway puts a sleeping-enabled service to
# sleep after 10 minutes without outbound traffic, and a long transcription makes none.
KEEP_AWAKE_URL = "https://www.gstatic.com/generate_204"
PRODUCE_EVERY_HOURS = 3
SCORE_EVERY_HOURS = 1
CLIPS_PER_CYCLE = 2

log = logging.getLogger("clip_engine")


def due(conn, key: str, every_hours: float) -> bool:
    """True (and records the run) if the task hasn't run in the last every_hours."""
    last = float(db.get(conn, key, "0"))
    if time.time() - last < every_hours * 3600:
        return False
    db.put(conn, key, str(time.time()))
    return True


_producer: subprocess.Popen | None = None


def connection_signature(conn, settings: Settings) -> tuple:
    return (youtube.stored_token(conn, settings), youtube.auto_posting_on(conn))


def build_publishers(conn, base_settings: Settings):
    """(settings with the current YouTube token, YouTube client or None, publishers by platform)."""
    settings = youtube.effective_settings(conn, base_settings)
    yt = YouTube(settings) if settings.youtube_refresh_token else None
    pubs = platforms.enabled(settings, conn, yt)
    auto = [name for name, pub in pubs.items() if not platforms.is_manual(pub)]
    db.put(conn, "auto_platforms", ",".join(auto))
    log.info("posting automatically to: %s; from your phone: %s", ", ".join(auto) or "none",
             ", ".join(n for n in pubs if n not in auto) or "none")
    return settings, yt, pubs


def start_producer() -> subprocess.Popen:
    """Make clips in a separate process. Whisper and the video frames need up to 3 GB; when the
    process exits that memory goes back, so the always-on part stays small (Railway bills memory
    by the minute). Scheduled posts keep going out meanwhile."""
    global _producer
    _producer = subprocess.Popen([sys.executable, "-m", "clip_engine", "produce"])
    return _producer


def producing() -> bool:
    return _producer is not None and _producer.poll() is None


def can_produce() -> bool:
    """Clips need Claude. Without a key, skip the download and transcription instead of failing after them."""
    return bool(os.environ.get("ANTHROPIC_API_KEY"))


def keep_awake() -> None:
    try:
        requests.head(KEEP_AWAKE_URL, timeout=10)
    except requests.RequestException:
        pass


def tick(conn, settings: Settings, pubs) -> None:
    """One pass of the scheduler. Every step is isolated so one failure doesn't stop the rest."""
    if db.get(conn, "paused") == "1":
        return
    steps = [("publish", lambda: pipeline.publish_due(conn, settings, pubs))]
    if due(conn, "last_score", SCORE_EVERY_HOURS):
        steps += [("score", lambda: pipeline.score(conn, settings, pubs)),
                  ("views", lambda: pipeline.refresh_views(conn, pubs)),
                  ("digest", lambda: pipeline.send_digest(conn, settings.digest_webhook_url))]
    if can_produce() and not producing() and (
            db.get(conn, "run_now") == "1" or due(conn, "last_produce", PRODUCE_EVERY_HOURS)):
        db.put(conn, "run_now", "0")
        start_producer()
    for name, fn in steps:
        try:
            fn()
        except Exception as exc:
            log.exception("%s failed; will retry later", name)
            pipeline.notify(settings.digest_webhook_url, f"clip-engine: {name} failed and will retry: {exc!r}")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    command = argv[1] if len(argv) > 1 else "run"
    settings = Settings()
    conn = db.connect(settings.db_path)

    if command == "report":
        print("What works (higher score = more views/earnings):")
        for row in strategy.leaderboard(conn):
            print(f"  {row['dimension']:12} {row['arm']:24} n={row['n']:<4} score={row['mean']:.2f}")
        print("\nEstimated campaign earnings:")
        for row in pipeline.earnings(conn, settings):
            print(f"  {row['campaign']:24} posts={row['posts']:<4} views={row['views']:<9} ${row['estimated_usd']}")
        print("\nLinks to submit:")
        for row in pipeline.pending_submissions(conn):
            print(f"  [{row['campaign_id']}] {row['platform']:9} {row['url']}  {row['title']}")
        return 0

    # base holds only the Railway variables. The saved YouTube login is layered on top each time, so
    # a Disconnect isn't undone by an old copy of the login kept in memory.
    base = settings
    settings, yt, pubs = build_publishers(conn, base)
    if command == "produce":
        try:
            pipeline.produce(conn, settings, anthropic.Anthropic(), yt, pubs, CLIPS_PER_CYCLE)
        except Exception as exc:
            log.exception("produce failed; will retry next cycle")
            pipeline.notify(settings.digest_webhook_url, f"clip-engine: produce failed and will retry: {exc!r}")
            return 1
    elif command == "publish":
        pipeline.publish_due(conn, settings, pubs)
    elif command == "score":
        pipeline.score(conn, settings, pubs)
    elif command == "run":
        dashboard.start(base)
        signature = connection_signature(conn, base)
        while True:
            # Picks up Connect, Disconnect and the automatic-posting switch without a restart.
            if connection_signature(conn, base) != signature:
                signature = connection_signature(conn, base)
                settings, yt, pubs = build_publishers(conn, base)
            tick(conn, settings, pubs)
            if producing():
                keep_awake()
            time.sleep(TICK_MINUTES * 60)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
