"""Publishers: post a rendered clip to YouTube, TikTok or Instagram and read back views.

Each platform is enabled only when its credentials are set. All share one
interface:

    publish(clip, path, public_url) -> (external_id, url)
    stats(external_ids)             -> {external_id: (views, subscribers_gained)}

TikTok and Instagram tokens expire, so refreshed tokens are stored in the kv
table and survive restarts.
"""
import json
import logging
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

from . import db
from .config import Settings

log = logging.getLogger(__name__)


class PublishError(RuntimeError):
    pass


class YouTubePublisher:
    name = "youtube"

    def __init__(self, yt, daily_limit: int):
        self.yt = yt
        self.daily_limit = daily_limit

    def publish(self, clip: dict, path: Path, public_url: str) -> tuple[str, str]:
        video_id = self.yt.upload(path, clip["title"], clip["description"], json.loads(clip["tags"]))
        return video_id, f"https://youtu.be/{video_id}"

    def stats(self, ids: list[str]) -> dict[str, tuple[int, int]]:
        return self.yt.performance(ids, date.today() - timedelta(days=60))


class TikTokPublisher:
    """TikTok Content Posting API (Direct Post). Needs the video.publish and video.list scopes.

    Until TikTok audits the app, posts can only be private (TIKTOK_PRIVACY=SELF_ONLY).
    """
    name = "tiktok"
    API = "https://open.tiktokapis.com"
    MAX_SINGLE_CHUNK = 64 * 1024 * 1024
    CHUNK = 10 * 1024 * 1024

    def __init__(self, conn: sqlite3.Connection, client_key: str, client_secret: str, refresh_token: str,
                 privacy: str, daily_limit: int, http=None):
        self.conn = conn
        self.client_key = client_key
        self.client_secret = client_secret
        self.initial_refresh_token = refresh_token
        self.privacy = privacy
        self.daily_limit = daily_limit
        self.http = http or requests.Session()

    def _token(self) -> str:
        token = db.get(self.conn, "tiktok_access_token")
        expires = float(db.get(self.conn, "tiktok_access_expires", "0"))
        if token and expires > time.time() + 300:
            return token
        refresh = db.get(self.conn, "tiktok_refresh_token") or self.initial_refresh_token
        resp = self.http.post(f"{self.API}/v2/oauth/token/", data={
            "client_key": self.client_key, "client_secret": self.client_secret,
            "grant_type": "refresh_token", "refresh_token": refresh,
        }, timeout=30)
        data = resp.json()
        if "access_token" not in data:
            raise PublishError(f"TikTok token refresh failed: {data}")
        db.put(self.conn, "tiktok_access_token", data["access_token"])
        db.put(self.conn, "tiktok_access_expires", str(time.time() + float(data.get("expires_in", 86400))))
        db.put(self.conn, "tiktok_refresh_token", data.get("refresh_token", refresh))
        return data["access_token"]

    def _post(self, path: str, body: dict) -> dict:
        resp = self.http.post(f"{self.API}{path}", json=body, timeout=60, headers={
            "Authorization": f"Bearer {self._token()}", "Content-Type": "application/json; charset=UTF-8"})
        data = resp.json()
        err = data.get("error", {})
        if err.get("code") not in (None, "ok"):
            raise PublishError(f"TikTok {path}: {err.get('code')} {err.get('message')}")
        return data.get("data", {})

    @classmethod
    def chunking(cls, size: int) -> tuple[int, int]:
        """(chunk_size, total_chunk_count). Small files go in one piece; the last chunk takes the remainder."""
        if size <= cls.MAX_SINGLE_CHUNK:
            return size, 1
        return cls.CHUNK, size // cls.CHUNK

    def publish(self, clip: dict, path: Path, public_url: str) -> tuple[str, str]:
        size = path.stat().st_size
        chunk_size, count = self.chunking(size)
        caption = f"{clip['title']}\n\n{clip['description']}"[:2200]
        init = self._post("/v2/post/publish/video/init/", {
            "post_info": {
                "title": caption,
                "privacy_level": self.privacy,
                "disable_duet": False, "disable_stitch": False, "disable_comment": False,
                "video_cover_timestamp_ms": 1000,
                # Paid clips promote a third party: TikTok's branded-content label.
                "brand_content_toggle": bool(clip.get("campaign_id")),
                "brand_organic_toggle": False,
            },
            "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                            "chunk_size": chunk_size, "total_chunk_count": count},
        })
        with open(path, "rb") as f:
            for i in range(count):
                first = i * chunk_size
                last = size - 1 if i == count - 1 else first + chunk_size - 1
                f.seek(first)
                body = f.read(last - first + 1)
                resp = self.http.put(init["upload_url"], data=body, timeout=300, headers={
                    "Content-Type": "video/mp4", "Content-Length": str(len(body)),
                    "Content-Range": f"bytes {first}-{last}/{size}"})
                if resp.status_code not in (200, 201, 206):
                    raise PublishError(f"TikTok upload chunk {i} failed: HTTP {resp.status_code}")
        publish_id = init["publish_id"]
        for _ in range(60):  # up to ~10 minutes for TikTok to process
            status = self._post("/v2/post/publish/status/fetch/", {"publish_id": publish_id})
            if status.get("status") == "PUBLISH_COMPLETE":
                ids = status.get("publicaly_available_post_id") or []
                post_id = str(ids[0]) if ids else publish_id
                return post_id, f"https://www.tiktok.com/video/{post_id}" if ids else ""
            if status.get("status") == "FAILED":
                raise PublishError(f"TikTok publish failed: {status.get('fail_reason')}")
            time.sleep(10)
        raise PublishError("TikTok publish timed out")

    def stats(self, ids: list[str]) -> dict[str, tuple[int, int]]:
        numeric = [i for i in ids if i.isdigit()]
        out = {i: (0, 0) for i in ids}
        for start in range(0, len(numeric), 20):
            resp = self.http.post(f"{self.API}/v2/video/query/?fields=id,view_count", timeout=30,
                                  json={"filters": {"video_ids": numeric[start:start + 20]}},
                                  headers={"Authorization": f"Bearer {self._token()}"})
            for v in resp.json().get("data", {}).get("videos", []):
                out[str(v["id"])] = (int(v.get("view_count", 0)), 0)
        return out


class InstagramPublisher:
    """Instagram API with Instagram Login: publishes Reels from a public video URL."""
    name = "instagram"
    API = "https://graph.instagram.com/v25.0"

    def __init__(self, conn: sqlite3.Connection, user_id: str, access_token: str, daily_limit: int, http=None):
        self.conn = conn
        self.user_id = user_id
        self.initial_token = access_token
        self.daily_limit = daily_limit
        self.http = http or requests.Session()

    def _token(self) -> str:
        token = db.get(self.conn, "instagram_token") or self.initial_token
        refreshed = float(db.get(self.conn, "instagram_token_refreshed", "0"))
        if time.time() - refreshed > 7 * 86400:  # long-lived tokens last 60 days; refresh weekly
            resp = self.http.get("https://graph.instagram.com/refresh_access_token", timeout=30,
                                 params={"grant_type": "ig_refresh_token", "access_token": token})
            data = resp.json()
            if "access_token" in data:
                token = data["access_token"]
                db.put(self.conn, "instagram_token", token)
                db.put(self.conn, "instagram_token_refreshed", str(time.time()))
            else:
                log.warning("Instagram token refresh failed: %s", data)
        return token

    def _call(self, method: str, path: str, **params) -> dict:
        params["access_token"] = self._token()
        resp = self.http.request(method, f"{self.API}/{path}", params=params, timeout=60)
        data = resp.json()
        if "error" in data:
            raise PublishError(f"Instagram {path}: {data['error'].get('message')}")
        return data

    def publish(self, clip: dict, path: Path, public_url: str) -> tuple[str, str]:
        if not public_url:
            raise PublishError("Instagram needs PUBLIC_BASE_URL so it can fetch the video")
        caption = f"{clip['title']}\n\n{clip['description']}"[:2200]
        container = self._call("POST", f"{self.user_id}/media", media_type="REELS", video_url=public_url,
                               caption=caption, share_to_feed="true")["id"]
        for _ in range(60):
            status = self._call("GET", container, fields="status_code").get("status_code")
            if status == "FINISHED":
                break
            if status in ("ERROR", "EXPIRED"):
                raise PublishError(f"Instagram processing {status}")
            time.sleep(10)
        else:
            raise PublishError("Instagram processing timed out")
        media_id = self._call("POST", f"{self.user_id}/media_publish", creation_id=container)["id"]
        link = self._call("GET", media_id, fields="permalink").get("permalink", "")
        return media_id, link

    def stats(self, ids: list[str]) -> dict[str, tuple[int, int]]:
        out = {}
        for media_id in ids:
            try:
                data = self._call("GET", f"{media_id}/insights", metric="views")
                values = data.get("data", [{}])[0].get("values", [{}])
                out[media_id] = (int(values[0].get("value", 0)), 0)
            except Exception:
                log.exception("Instagram insights failed for %s", media_id)
                out[media_id] = (0, 0)
        return out


def enabled(settings: Settings, conn: sqlite3.Connection, yt) -> dict:
    """Publishers whose credentials are configured, by name."""
    pubs = {}
    if settings.youtube_refresh_token and yt is not None:
        pubs["youtube"] = YouTubePublisher(yt, settings.max_uploads_per_day)
    if os.environ.get("TIKTOK_CLIENT_KEY") and os.environ.get("TIKTOK_REFRESH_TOKEN"):
        pubs["tiktok"] = TikTokPublisher(
            conn, os.environ["TIKTOK_CLIENT_KEY"], os.environ.get("TIKTOK_CLIENT_SECRET", ""),
            os.environ["TIKTOK_REFRESH_TOKEN"], os.environ.get("TIKTOK_PRIVACY", "SELF_ONLY"),
            int(os.environ.get("TIKTOK_MAX_PER_DAY", "15")))
    if os.environ.get("INSTAGRAM_USER_ID") and os.environ.get("INSTAGRAM_ACCESS_TOKEN"):
        pubs["instagram"] = InstagramPublisher(
            conn, os.environ["INSTAGRAM_USER_ID"], os.environ["INSTAGRAM_ACCESS_TOKEN"],
            int(os.environ.get("INSTAGRAM_MAX_PER_DAY", "25")))
    return pubs


def next_slot(hour_utc: int, now: datetime | None = None) -> datetime:
    """Next time the clock reads hour_utc:00 UTC (now if we're within that hour)."""
    now = now or datetime.now(timezone.utc)
    slot = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if slot + timedelta(hours=1) <= now:
        slot += timedelta(days=1)
    return max(slot, now)

