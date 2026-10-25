"""Platform rules every clip has to pass, so the accounts aren't demoted, demonetized or banned.

What the platforms enforce (checked October 2026):
- YouTube: reused content needs added value, and mass-produced or near-identical uploads count as
  "inauthentic content", which can cost monetization for the whole channel. More than 60 hashtags
  and every hashtag is ignored; unrelated hashtags can get a video removed.
- Instagram: accounts that mostly repost others' work are left out of recommendations. Watermarks,
  crops and credit alone don't count as original; commentary or a new angle does. Since Dec 2025
  a post may carry at most 5 hashtags, and generic ones (#reels, #explore) are discouraged.
- TikTok: imported media with no new edits, or with another app's watermark, is unoriginal and
  kept off the For You feed. Commercial content must use the branded-content setting.
"""
import re
import sqlite3
from urllib.parse import urlsplit

MAX_HASHTAGS = 5  # Instagram's cap; well inside YouTube's 60 and TikTok's caption limit
GENERIC_HASHTAGS = {
    "fyp", "foryou", "foryoupage", "fy", "viral", "viralvideo", "trending", "explore", "explorepage",
    "reels", "reel", "reelsinstagram", "instagram", "instagood", "tiktok", "shorts", "youtubeshorts", "short",
    "follow", "followme", "like", "likeforlike", "followforfollow", "video", "videos",
}
# Videos downloaded from these apps carry their logo, which TikTok and Instagram treat as unoriginal.
WATERMARKED_HOSTS = ("tiktok.com", "instagram.com", "facebook.com", "fb.watch", "snapchat.com", "likee.video")
SIMILAR_TITLE = 0.7  # share of words in common that makes two titles near-duplicates
RECENT_DAYS = 30


def clean_hashtags(tags: list[str]) -> list[str]:
    """At most 5 specific hashtags: generic ones and repeats removed, each one a single word."""
    out, seen = [], set()
    for tag in tags:
        word = tag.strip().lstrip("#")
        if not word or not re.fullmatch(r"\w+", word) or word.lower() in GENERIC_HASHTAGS:
            continue
        if word.lower() not in seen:
            seen.add(word.lower())
            out.append(f"#{word}")
    return out[:MAX_HASHTAGS]


def watermarked(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return any(host == h or host.endswith("." + h) for h in WATERMARKED_HOSTS)


def _words(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", title.lower()) if len(w) > 2}


def similar(a: str, b: str) -> bool:
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return False
    return len(wa & wb) / len(wa | wb) >= SIMILAR_TITLE


def recent_titles(conn: sqlite3.Connection, days: int = RECENT_DAYS, limit: int = 40) -> list[str]:
    rows = conn.execute("SELECT title FROM clips WHERE created_at >= datetime('now', ?) ORDER BY id DESC LIMIT ?",
                        (f"-{days} days", limit)).fetchall()
    return [r[0] for r in rows]


def near_duplicate(title: str, others: list[str]) -> str | None:
    """The earlier title this one nearly repeats, if any. Look-alike uploads read as mass-produced."""
    return next((o for o in others if similar(title, o)), None)
