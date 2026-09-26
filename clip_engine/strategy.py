"""Self-adjusting strategy: Thompson sampling over independent choice dimensions.

Each upload records which "arm" it used in every dimension (format, topic,
title style, clip length). Once a video is old enough, its views and
subscribers become a reward that updates every arm it used. New choices are
drawn by sampling each arm's plausible mean reward and taking the best, which
favors what works while still trying new and under-tested options.
"""
import json
import math
import random
import sqlite3

TITLE_STYLES = ["question", "number", "bold_claim", "curiosity_gap", "how_to"]
FORMATS = ["short", "long"]
# Clip length arms, in seconds, per format. Shorts must stay under 60s.
LENGTHS = {
    "short": ["20-35", "35-58"],
    "long": ["120-300", "300-600"],
}
SUBSCRIBER_WEIGHT = 50  # one subscriber is worth about 50 views
PRIOR_SD = 2.0


def reward(views: int, subscribers_gained: int) -> float:
    return math.log1p(max(views, 0) + SUBSCRIBER_WEIGHT * max(subscribers_gained, 0))


def _stats(conn: sqlite3.Connection, dimension: str) -> dict[str, tuple[int, float, float]]:
    rows = conn.execute(
        "SELECT arm, n, total, total_sq FROM arm_stats WHERE dimension = ?", (dimension,)
    ).fetchall()
    return {r["arm"]: (r["n"], r["total"], r["total_sq"]) for r in rows}


def choose(conn: sqlite3.Connection, dimension: str, arms: list[str], rng: random.Random | None = None) -> str:
    if not arms:
        raise ValueError(f"no arms to choose from for {dimension}")
    rng = rng or random
    stats = _stats(conn, dimension)
    seen = [s for s in stats.values() if s[0] > 0]
    prior_mean = sum(s[1] for s in seen) / sum(s[0] for s in seen) if seen else 5.0

    def sample(arm: str) -> float:
        n, total, total_sq = stats.get(arm, (0, 0.0, 0.0))
        if n == 0:
            return rng.gauss(prior_mean, PRIOR_SD)
        mean = total / n
        var = max(total_sq / n - mean * mean, 0.25)
        return rng.gauss(mean, math.sqrt(var / n))

    return max(arms, key=sample)


def plan(conn: sqlite3.Connection, topics: list[str], rng: random.Random | None = None) -> dict[str, str]:
    """Choose the arms for the next upload."""
    fmt = choose(conn, "format", FORMATS, rng)
    return {
        "format": fmt,
        "length": choose(conn, "length", [f"{fmt}:{r}" for r in LENGTHS[fmt]], rng),
        "topic": choose(conn, "topic", topics, rng),
        "title_style": choose(conn, "title_style", TITLE_STYLES, rng),
    }


def length_range(arms: dict[str, str]) -> tuple[int, int]:
    lo, hi = arms["length"].split(":")[1].split("-")
    return int(lo), int(hi)


def record(conn: sqlite3.Connection, arms_json: str, value: float) -> None:
    for dimension, arm in json.loads(arms_json).items():
        conn.execute(
            """INSERT INTO arm_stats (dimension, arm, n, total, total_sq) VALUES (?, ?, 1, ?, ?)
               ON CONFLICT(dimension, arm) DO UPDATE SET
                 n = n + 1, total = total + excluded.total, total_sq = total_sq + excluded.total_sq""",
            (dimension, arm, value, value * value),
        )
    conn.commit()


def leaderboard(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT dimension, arm, n, total / n AS mean FROM arm_stats WHERE n > 0 ORDER BY dimension, mean DESC"
    ).fetchall()
    return [dict(r) for r in rows]
