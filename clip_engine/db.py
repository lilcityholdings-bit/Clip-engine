"""SQLite storage: sources, clips, per-platform posts, strategy stats and settings."""
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    identifier TEXT PRIMARY KEY,      -- archive.org item id or campaign video URL
    title TEXT NOT NULL,
    creator TEXT,
    license_url TEXT NOT NULL,        -- license, or the campaign terms that grant permission
    source_url TEXT NOT NULL,
    topic TEXT,                       -- topic or campaign id
    processed_at TEXT
);
CREATE TABLE IF NOT EXISTS clips (
    id INTEGER PRIMARY KEY,
    source_identifier TEXT NOT NULL REFERENCES sources(identifier),
    campaign_id TEXT,                 -- clipping campaign this clip belongs to, if any
    start_sec REAL NOT NULL,
    end_sec REAL NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL,        -- full description including credit/disclosure
    tags TEXT NOT NULL,               -- JSON list
    arms TEXT NOT NULL,               -- JSON {dimension: arm} chosen for this clip
    file_path TEXT,                   -- rendered video, deleted once every post is done
    media_token TEXT,                 -- secret path the file is served at (Instagram fetches it)
    ai_score INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    views INTEGER,                    -- total across platforms when scored
    subscribers_gained INTEGER,
    scored_at TEXT
);
CREATE TABLE IF NOT EXISTS posts (
    id INTEGER PRIMARY KEY,
    clip_id INTEGER NOT NULL REFERENCES clips(id),
    platform TEXT NOT NULL,           -- youtube | tiktok | instagram
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | posted | failed
    scheduled_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    external_id TEXT,
    url TEXT,
    error TEXT,
    posted_at TEXT,
    views INTEGER,
    submitted_at TEXT,                -- when the link was sent for campaign submission
    UNIQUE (clip_id, platform)
);
CREATE TABLE IF NOT EXISTS arm_stats (
    dimension TEXT NOT NULL,
    arm TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    total REAL NOT NULL DEFAULT 0,    -- sum of rewards
    total_sq REAL NOT NULL DEFAULT 0, -- sum of squared rewards
    PRIMARY KEY (dimension, arm)
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    # Each thread (worker, dashboard) opens its own connection; wait out brief write locks.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")  # dashboard reads while the worker writes
    conn.executescript(SCHEMA)
    return conn


def get(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def put(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, value))
    conn.commit()


def clips_today(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM clips WHERE created_at >= datetime('now', '-1 day')").fetchone()[0]


def posts_today(conn: sqlite3.Connection, platform: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM posts WHERE platform = ? AND status = 'posted' AND posted_at >= datetime('now', '-1 day')",
        (platform,),
    ).fetchone()[0]
