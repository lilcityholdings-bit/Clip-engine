"""SQLite storage for sources, uploads and strategy statistics."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    identifier TEXT PRIMARY KEY,      -- archive.org item id
    title TEXT NOT NULL,
    creator TEXT,
    license_url TEXT NOT NULL,
    source_url TEXT NOT NULL,
    topic TEXT,
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS uploads (
    video_id TEXT PRIMARY KEY,        -- YouTube video id
    source_identifier TEXT NOT NULL REFERENCES sources(identifier),
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    title TEXT NOT NULL,
    arms TEXT NOT NULL,               -- JSON {dimension: arm} chosen for this upload
    uploaded_at TEXT NOT NULL DEFAULT (datetime('now')),
    views INTEGER,
    subscribers_gained INTEGER,
    scored_at TEXT
);
CREATE TABLE IF NOT EXISTS arm_stats (
    dimension TEXT NOT NULL,
    arm TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,    -- sum of rewards
    total_sq REAL NOT NULL DEFAULT 0, -- sum of squared rewards
    PRIMARY KEY (dimension, arm)
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def uploads_today(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM uploads WHERE uploaded_at >= datetime('now', '-1 day')"
    ).fetchone()
    return row[0]
