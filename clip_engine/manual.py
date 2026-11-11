"""Videos you hand to the engine yourself: a file uploaded from your phone, or a link.

Some campaigns give clippers a source video to download (a Google Drive or Dropbox file, or a
download button) instead of a channel to watch. Those videos are queued here and clipped before
anything the engine finds on its own.
"""
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from . import db

ALLOWED_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
MAX_UPLOAD_MB = int(os.environ.get("MAX_UPLOAD_MB", "2000"))
CHUNK = 1024 * 1024
QUEUE_KEY = "manual_queue"


class UploadError(ValueError):
    """Something the owner can fix; the message says what."""


def upload_dir(data_dir: Path) -> Path:
    return data_dir / "uploads"


def extension(filename: str) -> str:
    ext = Path(filename or "").suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise UploadError("Upload an .mp4, .mov, .mkv, .webm or .m4v video.")
    return ext


def save_upload(stream, length: int, filename: str, data_dir: Path) -> Path:
    """Copy `length` bytes from the request to a new file under uploads/, a megabyte at a time,
    so a large video never has to fit in memory."""
    ext = extension(filename)
    if length <= 0:
        raise UploadError("The file is empty.")
    if length > MAX_UPLOAD_MB * 1024 * 1024:
        raise UploadError(f"That file is over {MAX_UPLOAD_MB} MB. Use a link instead, or trim it first.")
    folder = upload_dir(data_dir)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{secrets.token_hex(8)}{ext}"
    remaining = length
    try:
        with open(path, "wb") as out:
            while remaining:
                block = stream.read(min(CHUNK, remaining))
                if not block:
                    raise UploadError("The upload was cut off. Try again on a steady connection.")
                out.write(block)
                remaining -= len(block)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def clean_link(url: str) -> str:
    url = (url or "").strip()
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise UploadError("Paste the video's link, starting with https://")
    return url


def clean_text(value: str, limit: int = 120) -> str:
    return re.sub(r"\s+", " ", value or "").strip()[:limit]


def queue(conn: sqlite3.Connection) -> list[dict]:
    return json.loads(db.get(conn, QUEUE_KEY, "[]"))


def add(conn: sqlite3.Connection, item: dict, front: bool = False) -> None:
    items = queue(conn)
    db.put(conn, QUEUE_KEY, json.dumps([item] + items if front else items + [item]))


def pop(conn: sqlite3.Connection) -> dict | None:
    items = queue(conn)
    if not items:
        return None
    db.put(conn, QUEUE_KEY, json.dumps(items[1:]))
    return items[0]


def discard(item: dict) -> None:
    """Delete the uploaded file of a queue item that won't be used."""
    if item.get("kind") == "file":
        Path(item["ref"]).unlink(missing_ok=True)
