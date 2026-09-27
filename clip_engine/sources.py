"""Find openly licensed long-form videos on the Internet Archive.

Uploaders set archive.org licenses themselves, so anyone can mislabel a TV
show as CC0. To avoid copyright strikes we only search curated collections
whose public-domain status the Archive vouches for (TRUSTED_COLLECTIONS).

Only licenses that allow commercial reuse and modification are accepted,
because clips are edited and posted to a monetizable channel:
CC0, public domain, CC BY and CC BY-SA. NonCommercial (NC) and
NoDerivatives (ND) licenses are rejected.
"""
import re
from dataclasses import dataclass, field

import requests

SEARCH_URL = "https://archive.org/advancedsearch.php"
METADATA_URL = "https://archive.org/metadata/{identifier}"
DOWNLOAD_URL = "https://archive.org/download/{identifier}/{name}"
USER_AGENT = "clip-engine/1.0"
MAX_DOWNLOAD_BYTES = 2_500_000_000
TRUSTED_COLLECTIONS = [
    "prelinger",               # Prelinger Archives: public-domain educational/industrial films
    "feature_films",           # Archive-curated public-domain feature films
    "fedflix",                 # US government films (public domain)
    "nasa",                    # NASA footage (public domain)
    "usgovernmentdocuments",
]

_ALLOWED = [
    re.compile(r"creativecommons\.org/publicdomain/(zero|mark)/"),
    re.compile(r"creativecommons\.org/licenses/publicdomain/?"),
    re.compile(r"creativecommons\.org/licenses/by/"),
    re.compile(r"creativecommons\.org/licenses/by-sa/"),
]


def license_allowed(url: str | None) -> bool:
    if not url:
        return False
    url = url.lower()
    if "-nc" in url or "-nd" in url:
        return False
    return any(p.search(url) for p in _ALLOWED)


def license_name(url: str) -> str:
    url = url.lower()
    if "publicdomain" in url:
        return "CC0" if "zero" in url else "Public Domain"
    if "by-sa" in url:
        return "CC BY-SA"
    return "CC BY"


@dataclass
class Source:
    identifier: str
    title: str
    creator: str
    license_url: str
    video_url: str
    duration: float
    page: str = ""  # link to the original; defaults to the archive.org item page
    heatmap: list = field(default_factory=list)  # YouTube "most replayed" points, when available

    @property
    def page_url(self) -> str:
        return self.page or f"https://archive.org/details/{self.identifier}"


def _duration(value) -> float:
    """archive.org lengths are seconds ("1834.2") or clock time ("30:34")."""
    if value is None:
        return 0.0
    value = str(value)
    try:
        if ":" in value:
            secs = 0.0
            for part in value.split(":"):
                secs = secs * 60 + float(part)
            return secs
        return float(value)
    except ValueError:
        return 0.0


def search(topic: str, rows: int = 15, session: requests.Session | None = None) -> list[str]:
    """Return identifiers of popular licensed videos matching the topic."""
    http = session or requests.Session()
    # Slashes must be escaped inside archive.org's wildcard queries.
    query = (
        f'({topic}) AND mediatype:movies AND '
        f'collection:({" OR ".join(TRUSTED_COLLECTIONS)}) AND '
        r'(licenseurl:*licenses\/by\/* OR licenseurl:*licenses\/by-sa\/* OR licenseurl:*publicdomain*)'
    )
    resp = http.get(
        SEARCH_URL,
        params={
            "q": query,
            "fl[]": ["identifier", "licenseurl"],
            "sort[]": "downloads desc",
            "rows": rows,
            "output": "json",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    docs = resp.json().get("response", {}).get("docs", [])
    return [d["identifier"] for d in docs if license_allowed(d.get("licenseurl"))]


def fetch(identifier: str, min_minutes: int = 10, session: requests.Session | None = None) -> Source | None:
    """Load item metadata and pick its best MP4. None if unusable."""
    http = session or requests.Session()
    resp = http.get(METADATA_URL.format(identifier=identifier), headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    meta = data.get("metadata", {})
    license_url = meta.get("licenseurl")
    if not license_allowed(license_url):
        return None
    mp4s = [f for f in data.get("files", []) if f.get("name", "").lower().endswith(".mp4")]
    if not mp4s:
        return None
    # Highest quality file that is still reasonable to download.
    small_enough = [f for f in mp4s if int(f.get("size", 0) or 0) <= MAX_DOWNLOAD_BYTES]
    if not small_enough:
        return None
    best = max(small_enough, key=lambda f: int(f.get("size", 0) or 0))
    duration = _duration(best.get("length"))
    if duration < min_minutes * 60:
        return None

    def text(value) -> str:
        return ", ".join(value) if isinstance(value, list) else (value or "")

    return Source(
        identifier=identifier,
        title=text(meta.get("title")) or identifier,
        creator=text(meta.get("creator")) or "Unknown",
        license_url=license_url,
        video_url=DOWNLOAD_URL.format(identifier=identifier, name=best["name"]),
        duration=duration,
    )
