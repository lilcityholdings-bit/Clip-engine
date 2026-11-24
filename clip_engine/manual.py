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
MAX_VIDEOS = 4   # most different videos made from one set of files
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


# The item being clipped right now is kept here too, so a restart in the middle of a run (a deploy, a crash)
# doesn't lose your upload: on start-up it goes back to the front of the queue.
INFLIGHT_KEY = "manual_inflight"
MAX_RECOVERIES = 3


def claim(conn: sqlite3.Connection, item: dict) -> None:
    db.put(conn, INFLIGHT_KEY, json.dumps(item))


def release(conn: sqlite3.Connection) -> None:
    db.put(conn, INFLIGHT_KEY, "")


def recover_inflight(conn: sqlite3.Connection) -> bool:
    """Put an item that was being clipped when the server stopped back at the front of the queue (a few tries,
    then its files are deleted). Returns True if there was one."""
    raw = db.get(conn, INFLIGHT_KEY, "")
    if not raw:
        return False
    item = json.loads(raw)
    release(conn)
    attempts = item.get("attempts", 0) + 1
    if attempts >= MAX_RECOVERIES:
        discard(item)
        return False
    add(conn, {**item, "attempts": attempts}, front=True)
    return True


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


def clean_length(value: str) -> str:
    """The length chosen for a combined video: "60" (up to a minute) or the default "30" (15 to 30 seconds)."""
    return "60" if (value or "").strip() == "60" else "30"


def clean_captions(value: str) -> list[str]:
    """Captions the owner typed, one per line (at most 4, 150 characters each)."""
    lines = [re.sub(r"[\x00-\x1f\x7f]", " ", line).strip() for line in (value or "").splitlines()]
    return [line[:150] for line in lines if line][:MAX_VIDEOS]


def clean_notes(value: str) -> str:
    """The owner's own instructions for how a video should be cut (kept short; control characters removed)."""
    text = re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", value or "")).strip()
    return text[:600]


LENGTH_CHOICES = {"10-20": "10 to 20 seconds", "15-25": "15 to 25 seconds", "25-35": "25 to 35 seconds",
                  "35-58": "35 to 58 seconds"}


def clean_seconds(value: str) -> str:
    """The clip length you chose, "10-20" and so on, or "" to let the engine decide."""
    value = (value or "").strip()
    return value if value in LENGTH_CHOICES else ""


def seconds_range(value: str) -> tuple[int, int] | None:
    value = clean_seconds(value)
    if not value:
        return None
    lo, hi = value.split("-")
    return int(lo), int(hi)


REDO_KEY = "redo_sources"
REDO_DAYS = 3


def keep_for_redo(conn: sqlite3.Connection, identifier: str, item: dict) -> None:
    """Keep an upload (its files) for a few days so a rejected clip can be redone with new instructions."""
    import time
    kept = _redo(conn)
    kept[identifier] = {"item": item, "until": time.time() + REDO_DAYS * 86400}
    db.put(conn, REDO_KEY, json.dumps(kept))


def _redo(conn: sqlite3.Connection) -> dict:
    try:
        return json.loads(db.get(conn, REDO_KEY, "{}") or "{}")
    except ValueError:
        return {}


def take_for_redo(conn: sqlite3.Connection, identifier: str) -> dict | None:
    """The kept upload for a source, removed from the store (it goes back in the queue), or None if it is gone."""
    kept = _redo(conn)
    entry = kept.pop(identifier, None)
    if entry is None:
        return None
    db.put(conn, REDO_KEY, json.dumps(kept))
    item = entry["item"]
    if item.get("kind") == "file" and not Path(item["ref"]).exists():
        return None
    if item.get("kind") == "project" and not any(Path(f).exists() for f in item.get("files", [])):
        return None
    return item


def expire_redo(conn: sqlite3.Connection) -> None:
    """Delete kept uploads older than REDO_DAYS."""
    import time
    kept = _redo(conn)
    live = {k: v for k, v in kept.items() if v.get("until", 0) > time.time()}
    for k, v in kept.items():
        if k not in live:
            discard(v["item"])
    if live != kept:
        db.put(conn, REDO_KEY, json.dumps(live))


def redo_files(conn: sqlite3.Connection) -> list[str]:
    out: list[str] = []
    for v in _redo(conn).values():
        item = v["item"]
        out += [item["ref"]] if item.get("kind") == "file" else item.get("files", [])
    return out


def clean_count(value: str) -> int:
    """How many different videos to make from a set of files: 1 to 4 (the most one upload may produce)."""
    try:
        return max(1, min(MAX_VIDEOS, int(value)))
    except (TypeError, ValueError):
        return 1


def finish_batch(conn: sqlite3.Connection, batch_id: str, item: dict) -> int:
    """Queue the batch's files, in the order they were uploaded, as one video to build. Returns the file count."""
    batches = _batches(conn)
    batch = batches.pop(batch_id, None)
    db.put(conn, BATCH_KEY, json.dumps(batches))
    if not batch or not batch["files"]:
        raise UploadError("Those files did not arrive. Try again.")
    if len(batch["files"]) == 1 and not is_image(batch["files"][0]):
        # one video has nothing to be combined with: cut it into clips instead
        add(conn, {**item, "kind": "file", "ref": batch["files"][0],
                   "title": item.get("title") or Path(batch["files"][0]).stem[:60]})
    else:
        add(conn, {**item, "kind": "project", "files": batch["files"]})
    return len(batch["files"])
