"""Entry point.

python -m clip_engine run      # run forever (what Railway runs)
python -m clip_engine produce  # one production cycle
python -m clip_engine score    # one scoring cycle
python -m clip_engine report   # print what the strategy has learned
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
        for row in strategy.leaderboard(conn):
            print(f"{row['dimension']:12} {row['arm']:24} n={row['n']:<4} score={row['mean']:.2f}")
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
                             ("produce", lambda: pipeline.produce(conn, settings, claude, yt, CLIPS_PER_CYCLE))):
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
