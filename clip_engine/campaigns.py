"""Clipping campaigns: creators who pay per view for clips of their videos.

Campaigns are configured as JSON (CAMPAIGNS_JSON env var or campaigns.json in
DATA_DIR), one object per campaign you've been accepted into:

[
  {
    "id": "some-podcast",
    "name": "Some Podcast clipping",
    "program_url": "https://whop.com/...",          # where the terms live
    "rate_per_1k_views": 1.5,                         # USD
    "sources": ["https://www.youtube.com/@somepodcast/videos",
                "https://drive.google.com/file/d/..."],
    "caption_required": "@somepodcast #somepodcast",  # must appear in every post
    "rules": "No clips of sponsor reads. Keep profanity bleeped.",
    "formats": ["short"],
    "active": true,
    "ends_on": "2026-12-31"
  }
]

Only add campaigns whose terms you've accepted: they are what give you the
right to repost the creator's content.
"""
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from .sources import Source

log = logging.getLogger(__name__)

# FTC: paid clipping is paid promotion and must be disclosed.
DISCLOSURE = "#ad"
LATEST_PER_SOURCE = 10


@dataclass
class Campaign:
    id: str
    name: str
    program_url: str
    rate_per_1k_views: float
    sources: list[str]
    caption_required: str = ""
    rules: str = ""
    formats: list[str] = field(default_factory=lambda: ["short"])
    active: bool = True
    ends_on: str | None = None

    def is_live(self, today: date | None = None) -> bool:
        today = today or date.today()
        return self.active and (not self.ends_on or date.fromisoformat(self.ends_on) >= today)


def load(data_dir: Path) -> list[Campaign]:
    raw = os.environ.get("CAMPAIGNS_JSON")
    path = data_dir / "campaigns.json"
    if not raw and path.exists():
        raw = path.read_text()
    if not raw:
        return []
    return [Campaign(**c) for c in json.loads(raw)]


def live(campaigns: list[Campaign]) -> list[Campaign]:
    return [c for c in campaigns if c.is_live()]


def by_id(campaigns: list[Campaign]) -> dict[str, Campaign]:
    return {c.id: c for c in campaigns}


def _ydl(opts: dict):
    import yt_dlp  # only needed in campaign mode

    return yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, **opts})


def latest_videos(url: str) -> list[str]:
    """Video URLs from a channel/playlist (newest first), or the URL itself for a single video."""
    with _ydl({"extract_flat": "in_playlist", "playlistend": LATEST_PER_SOURCE}) as ydl:
        info = ydl.extract_info(url, download=False)
    if info.get("_type") in ("playlist", "multi_video"):
        return [e.get("url") or e.get("webpage_url") for e in info.get("entries") or [] if e]
    return [info.get("webpage_url") or url]


def describe(campaign: Campaign, video_url: str) -> Source:
    """Title, creator and duration of one campaign video."""
    with _ydl({}) as ydl:
        info = ydl.extract_info(video_url, download=False)
    return Source(
        identifier=video_url,
        title=info.get("title") or video_url,
        creator=info.get("uploader") or info.get("channel") or campaign.name,
        license_url=campaign.program_url,
        video_url=video_url,
        duration=float(info.get("duration") or 0),
        page=info.get("webpage_url") or video_url,
    )


def download(video_url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    opts = {
        "outtmpl": str(dest.with_suffix(".%(ext)s")),
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
    }
    with _ydl(opts) as ydl:
        ydl.download([video_url])
    return dest.with_suffix(".mp4")


def caption_lines(campaign: Campaign, src: Source, fmt: str) -> str:
    lines = [f"Clip from {src.creator}: {src.page_url}"]
    if campaign.caption_required:
        lines.append(campaign.caption_required)
    lines.append(f"{DISCLOSURE} Paid clip for the {campaign.name} program.")
    if fmt == "short":
        lines.append("#Shorts")
    return "\n".join(lines)
