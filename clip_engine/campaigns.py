"""Clipping campaigns: creators who pay per view for clips of their videos.

Campaigns are normally added from the dashboard's "Add campaign" form, which saves
them to campaigns.json in DATA_DIR. They can also be set as JSON in the
CAMPAIGNS_JSON env var, one object per campaign you've been accepted into:

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
    "platforms": ["youtube", "tiktok", "instagram"],  # where the program pays for views
    "niche": "business and money podcast",            # used to research what's trending
    "allow_music": true,                              # false if the program bans added music
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
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .policy import watermarked
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
    platforms: list[str] = field(default_factory=lambda: ["youtube", "tiktok", "instagram"])
    niche: str = ""           # what the creator's content is about, for trend research
    allow_music: bool = True  # False if the program forbids adding background music
    active: bool = True
    ends_on: str | None = None

    def is_live(self, today: date | None = None) -> bool:
        today = today or date.today()
        return self.active and (not self.ends_on or date.fromisoformat(self.ends_on) >= today)


PLATFORMS = ("youtube", "tiktok", "instagram")


def _from_env() -> list[Campaign]:
    raw = os.environ.get("CAMPAIGNS_JSON")
    return [Campaign(**c) for c in json.loads(raw)] if raw else []


def _from_file(data_dir: Path) -> list[Campaign]:
    path = data_dir / "campaigns.json"
    return [Campaign(**c) for c in json.loads(path.read_text())] if path.exists() else []


def load(data_dir: Path) -> list[Campaign]:
    """Campaigns added from the dashboard (campaigns.json in DATA_DIR) plus any set in CAMPAIGNS_JSON.
    A dashboard campaign with the same id replaces the environment one."""
    saved = _from_file(data_dir)
    ids = {c.id for c in saved}
    return saved + [c for c in _from_env() if c.id not in ids]


def env_ids() -> set[str]:
    """Campaigns defined in the CAMPAIGNS_JSON setting, which the dashboard can't delete."""
    return {c.id for c in _from_env()}


def save(data_dir: Path, campaign: Campaign) -> None:
    """Add or replace one dashboard-managed campaign."""
    saved = [c for c in _from_file(data_dir) if c.id != campaign.id] + [campaign]
    _write(data_dir, saved)


def remove(data_dir: Path, campaign_id: str) -> None:
    _write(data_dir, [c for c in _from_file(data_dir) if c.id != campaign_id])


def _write(data_dir: Path, campaigns: list[Campaign]) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / "campaigns.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps([asdict(c) for c in campaigns], indent=2))
    tmp.replace(path)


def slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]


class CampaignError(ValueError):
    """A form value that can't be used, worded for the person filling in the form."""


def from_form(form: dict[str, str], platforms: list[str]) -> Campaign:
    """Build a campaign from the dashboard form, or raise CampaignError explaining what to fix."""
    name = form.get("name", "").strip()
    if not name or not slug(name):
        raise CampaignError("Give the campaign a name.")
    program_url = form.get("program_url", "").strip()
    if not program_url.startswith(("http://", "https://")):
        raise CampaignError("Paste the campaign's page link (starting with https://).")
    try:
        rate = float(form.get("rate", "").replace("$", "").strip())
    except ValueError:
        raise CampaignError("Pay per 1,000 views must be a number, like 1.50.")
    if not 0 < rate < 1000:
        raise CampaignError("Pay per 1,000 views must be more than 0.")
    sources = [s.strip() for s in re.split(r"[\s,]+", form.get("sources", "")) if s.strip()]
    bad = [s for s in sources if not s.startswith(("http://", "https://"))]
    if not sources or bad:
        raise CampaignError("Add at least one link to the creator's channel, playlist or videos (each starting with https://).")
    marked = [s for s in sources if watermarked(s)]
    if marked:
        raise CampaignError("TikTok, Instagram and Facebook videos carry the app's watermark, which TikTok and Instagram "
                            "treat as unoriginal. Use the creator's YouTube channel or the campaign's original files: "
                            + ", ".join(marked))
    chosen = [p for p in platforms if p in PLATFORMS]
    if not chosen:
        raise CampaignError("Pick at least one platform the campaign pays for.")
    ends_on = form.get("ends_on", "").strip() or None
    if ends_on:
        try:
            date.fromisoformat(ends_on)
        except ValueError:
            raise CampaignError("End date must look like 2026-12-31.")
    return Campaign(
        id=form.get("id", "").strip() or slug(name),
        name=name,
        program_url=program_url,
        rate_per_1k_views=rate,
        sources=sources,
        caption_required=form.get("caption_required", "").strip(),
        rules=form.get("rules", "").strip(),
        platforms=chosen,
        niche=form.get("niche", "").strip(),
        allow_music=form.get("allow_music") == "on",
        active=form.get("active", "on") == "on",
        ends_on=ends_on,
    )


def live(campaigns: list[Campaign]) -> list[Campaign]:
    return [c for c in campaigns if c.is_live()]


def by_id(campaigns: list[Campaign]) -> dict[str, Campaign]:
    return {c.id: c for c in campaigns}


def _cookie_file() -> str | None:
    """YouTube often blocks cloud servers; cookies from a signed-in browser (YTDLP_COOKIES,
    Netscape cookies.txt contents) get past the "confirm you're not a bot" check."""
    cookies = os.environ.get("YTDLP_COOKIES")
    if not cookies:
        return None
    path = Path(os.environ.get("DATA_DIR", "./data")) / "yt-cookies.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cookies)
    return str(path)


def _ydl(opts: dict):
    import yt_dlp  # only needed in campaign mode

    base = {"quiet": True, "no_warnings": True, "retries": 5, "fragment_retries": 5}
    cookie_file = _cookie_file()
    if cookie_file:
        base["cookiefile"] = cookie_file
    return yt_dlp.YoutubeDL({**base, **opts})


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
        heatmap=info.get("heatmap") or [],
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
