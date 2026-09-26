"""Entry point.

python -m clip_engine run      # run forever (what Railway runs)
python -m clip_engine produce  # one production cycle
python -m clip_engine score    # one scoring cycle
python -m clip_engine report   # what the strategy has learned, earnings, links to submit
python -m clip_engine submitted all|VIDEO_ID...  # mark campaign links as submitted
"""
import logging
import sys
import time

import anthropic

from . import pipeline, strategy
from .config import Settings
from .db import connect
from .youtube import YouTube

CYCLE_HOURS = 6
CLIPS_PER_CYCLE = 2

log = logging.getLogger("clip_engine")


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    command = argv[1] if len(argv) > 1 else "run"
    settings = Settings()
    conn = connect(settings.db_path)

    if command == "report":
        print("What works (higher score = more views/earnings):")
        for row in strategy.leaderboard(conn):
            print(f"  {row['dimension']:12} {row['arm']:24} n={row['n']:<4} score={row['mean']:.2f}")
        print("\nEstimated campaign earnings:")
        for row in pipeline.earnings(conn, settings):
            print(f"  {row['campaign']:24} posts={row['posts']:<4} views={row['views']:<9} ${row['estimated_usd']}")
        print("\nLinks to submit:")
        for row in pipeline.pending_submissions(conn):
            print(f"  [{row['campaign_id']}] {row['url']}  {row['title']}")
        return 0
    if command == "submitted":
        ids = [r["video_id"] for r in pipeline.pending_submissions(conn)] if argv[2:] == ["all"] else argv[2:]
        pipeline.mark_submitted(conn, ids)
        print(f"marked {len(ids)} as submitted")
        return 0

    claude = anthropic.Anthropic()
    yt = YouTube(settings)
    if command == "produce":
        pipeline.produce(conn, settings, claude, yt, CLIPS_PER_CYCLE)
    elif command == "score":
        pipeline.score(conn, settings, yt)
    elif command == "run":
        while True:
            for step, fn in (("score", lambda: pipeline.score(conn, settings, yt)),
                             ("produce", lambda: pipeline.produce(conn, settings, claude, yt, CLIPS_PER_CYCLE)),
                             ("digest", lambda: pipeline.send_digest(conn, settings.digest_webhook_url))):
                try:
                    fn()
                except Exception:
                    log.exception("%s cycle failed; will retry next cycle", step)
            time.sleep(CYCLE_HOURS * 3600)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
