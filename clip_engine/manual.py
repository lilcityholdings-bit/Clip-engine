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
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import db

VIDEO_EXTENSIONS = (".mp4", ".mov", ".mkv", ".webm", ".m4v")
IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp")
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS + IMAGE_EXTENSIONS
MAX_PIECES = 30  # most files that can be combined into one video
BATCH_KEY = "manual_batches"
BATCH_MAX_AGE_HOURS = 24
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
        raise UploadError("Upload a video (.mp4, .mov, .mkv, .webm, .m4v) or a photo (.jpg, .png, .webp).")
    return ext


def is_image(path) -> bool:
    return Path(str(path)).suffix.lower() in IMAGE_EXTENSIONS


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
    """Delete the uploaded file(s) of a queue item that won't be used."""
    if item.get("kind") == "file":
        Path(item["ref"]).unlink(missing_ok=True)
    elif item.get("kind") == "project":
        for name in item.get("files", []):
            Path(name).unlink(missing_ok=True)


# Several files meant to become ONE video arrive one request at a time. They wait here, by batch id,
# until the last one lands; then the whole set is queued as a single "project".

def clean_batch_id(value: str) -> str:
    value = (value or "").strip().lower()
    if value and not re.fullmatch(r"[a-z0-9]{6,40}", value):
        raise UploadError("That upload was not recognised. Reload the page and try again.")
    return value


def _batches(conn: sqlite3.Connection) -> dict:
    return json.loads(db.get(conn, BATCH_KEY, "{}"))


def expire_batches(conn: sqlite3.Connection, now: float | None = None) -> None:
    """Delete the files of a batch that never finished (a dropped connection part-way)."""
    now = time.time() if now is None else now
    batches = _batches(conn)
    fresh = {}
    for batch_id, batch in batches.items():
        if now - batch["at"] > BATCH_MAX_AGE_HOURS * 3600:
            discard({"kind": "project", "files": batch["files"]})
        else:
            fresh[batch_id] = batch
    if fresh != batches:
        db.put(conn, BATCH_KEY, json.dumps(fresh))


def add_to_batch(conn: sqlite3.Connection, batch_id: str, path: Path) -> int:
    """Remember one more file of an unfinished batch; returns how many it holds."""
    expire_batches(conn)
    batches = _batches(conn)
    batch = batches.setdefault(batch_id, {"at": time.time(), "files": []})
    if len(batch["files"]) >= MAX_PIECES:
        Path(path).unlink(missing_ok=True)
        raise UploadError(f"That is more than {MAX_PIECES} files for one video. Use fewer, or make two videos.")
    batch["files"].append(str(path))
    db.put(conn, BATCH_KEY, json.dumps(batches))
    return len(batch["files"])


def finish_batch(conn: sqlite3.Connection, batch_id: str, item: dict) -> int:
    """Queue the batch's files, in the order they were uploaded, as one video to build. Returns the file count."""
    batches = _batches(conn)
    batch = batches.pop(batch_id, None)
    db.put(conn, BATCH_KEY, json.dumps(batches))
    if not batch or not batch["files"]:
        raise UploadError("Those files did not arrive. Try again.")
    add(conn, {**item, "kind": "project", "files": batch["files"]})
    return len(batch["files"])
