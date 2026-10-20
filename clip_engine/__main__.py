"""Entry point.

python -m clip_engine run        # run forever: dashboard + scheduler (what Railway runs)
python -m clip_engine produce    # one production cycle
python -m clip_engine publish    # post everything that's due
python -m clip_engine score      # one scoring cycle
python -m clip_engine report     # what the strategy has learned, earnings, links to submit
"""
import logging
import sys
import threading
import time

import anthropic

from . import dashboard, db, pipeline, platforms, strategy
from .config import Settings
from . import youtube
from .youtube import YouTube

TICK_MINUTES = 5
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


_producer: threading.Thread | None = None


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


def start_producer(settings: Settings, claude, publishers: dict) -> threading.Thread:
    """Make clips in the background: a cycle can take half an hour, and scheduled posts
    must keep going out meanwhile. The thread uses its own database and API clients."""
    global _producer

    def run() -> None:
        conn = db.connect(settings.db_path)
        try:
            yt = YouTube(settings) if settings.youtube_refresh_token else None
            pipeline.produce(conn, settings, claude, yt, publishers, CLIPS_PER_CYCLE)
        except Exception as exc:
            log.exception("produce failed; will retry next cycle")
            pipeline.notify(settings.digest_webhook_url, f"clip-engine: produce failed and will retry: {exc!r}")
        finally:
            conn.close()

    _producer = threading.Thread(target=run, daemon=True, name="producer")
    _producer.start()
    return _producer


def tick(conn, settings: Settings, claude, yt, pubs) -> None:
    """One pass of the scheduler. Every step is isolated so one failure doesn't stop the rest."""
    if db.get(conn, "paused") == "1":
        return
    steps = [("publish", lambda: pipeline.publish_due(conn, settings, pubs))]
    if due(conn, "last_score", SCORE_EVERY_HOURS):
        steps += [("score", lambda: pipeline.score(conn, settings, pubs)),
                  ("views", lambda: pipeline.refresh_views(conn, pubs)),
                  ("digest", lambda: pipeline.send_digest(conn, settings.digest_webhook_url))]
    producing = _producer is not None and _producer.is_alive()
    if not producing and (db.get(conn, "run_now") == "1" or due(conn, "last_produce", PRODUCE_EVERY_HOURS)):
        db.put(conn, "run_now", "0")
        start_producer(settings, claude, pubs)
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

    claude = anthropic.Anthropic()
    settings, yt, pubs = build_publishers(conn, settings)
    if command == "produce":
        pipeline.produce(conn, settings, claude, yt, pubs, CLIPS_PER_CYCLE)
    elif command == "publish":
        pipeline.publish_due(conn, settings, pubs)
    elif command == "score":
        pipeline.score(conn, settings, pubs)
    elif command == "run":
        dashboard.start(settings)
        signature = connection_signature(conn, settings)
        while True:
            # Picks up Connect YouTube and the automatic-posting switch without a restart.
            if connection_signature(conn, settings) != signature:
                signature = connection_signature(conn, settings)
                settings, yt, pubs = build_publishers(conn, settings)
            tick(conn, settings, claude, yt, pubs)
            time.sleep(TICK_MINUTES * 60)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
