"""YouTube Data API and Analytics API access for one channel."""
import dataclasses
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import requests

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from . import db
from .config import Settings

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
CATEGORY_EDUCATION = "27"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


def redirect_uri(settings: Settings) -> str:
    """Where Google sends the person back after they approve access. Register this exact
    address as an authorized redirect URI on the OAuth client."""
    return f"{settings.public_base_url}/oauth/youtube/callback"


def authorize_url(settings: Settings, state: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "client_id": settings.youtube_client_id, "redirect_uri": redirect_uri(settings), "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent", "state": state,
    })


def exchange_code(settings: Settings, code: str, http=None) -> str:
    """Trade the one-time code from Google for a long-lived refresh token."""
    resp = (http or requests).post(TOKEN_URL, data={
        "code": code, "client_id": settings.youtube_client_id, "client_secret": settings.youtube_client_secret,
        "redirect_uri": redirect_uri(settings), "grant_type": "authorization_code"}, timeout=30)
    data = resp.json()
    if "refresh_token" not in data:
        raise ValueError(data.get("error_description") or data.get("error") or "Google didn't return a login token")
    return data["refresh_token"]


def revoke(token: str, http=None) -> bool:
    """Ask Google to cancel a login token so it stops working everywhere. True if Google accepted it."""
    if not token:
        return False
    return (http or requests).post(REVOKE_URL, data={"token": token}, timeout=30).status_code == 200


def stored_token(conn: sqlite3.Connection, settings: Settings) -> str:
    """The refresh token saved by the dashboard's Connect YouTube button, else the environment one."""
    return db.get(conn, "youtube_refresh_token") or settings.youtube_refresh_token


def auto_posting_on(conn: sqlite3.Connection) -> bool:
    """YouTube only posts automatically once you've switched it on, after Google approves the project.
    Before that, API uploads are kept private, so clips are better posted from the app."""
    return db.get(conn, "youtube_auto") == "on"


def effective_settings(conn: sqlite3.Connection, settings: Settings) -> Settings:
    return dataclasses.replace(settings, youtube_refresh_token=stored_token(conn, settings))


def credentials(settings: Settings) -> Credentials:
    return Credentials(
        token=None,
        refresh_token=settings.youtube_refresh_token,
        client_id=settings.youtube_client_id,
        client_secret=settings.youtube_client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )


class YouTube:
    def __init__(self, settings: Settings):
        creds = credentials(settings)
        self.settings = settings
        self.api = build("youtube", "v3", credentials=creds, cache_discovery=False)
        self.analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)

    def trending_titles(self, max_results: int = 50) -> list[str]:
        resp = self.api.videos().list(
            part="snippet", chart="mostPopular", regionCode=self.settings.trend_region, maxResults=max_results
        ).execute()
        return [item["snippet"]["title"] for item in resp.get("items", [])]

    last_privacy: str | None = None  # privacy YouTube actually applied to the latest upload

    def upload(self, path: Path, title: str, description: str, tags: list[str], privacy: str | None = None) -> str:
        requested = privacy or self.settings.privacy_status
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:15],
                "categoryId": CATEGORY_EDUCATION,
            },
            "status": {
                "privacyStatus": requested,
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = self.api.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
        self.last_privacy = (response.get("status") or {}).get("privacyStatus")
        return response["id"]

    def test_upload(self, workdir: Path) -> dict:
        """Upload a 3-second unlisted video to see whether Google keeps API uploads private.

        Returns {"video_id", "requested", "applied", "locked"}. The video is harmless and can be
        deleted in YouTube Studio; the API permission we ask for can't delete videos.
        """
        import subprocess

        workdir.mkdir(parents=True, exist_ok=True)
        path = workdir / "connection-test.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x223344:s=720x1280:r=15:d=3",
                        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "3", "-shortest", "-c:v", "libx264",
                        "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True, capture_output=True)
        try:
            video_id = self.upload(path, "Clip Engine connection test (safe to delete)",
                                   "Automatic connection test. You can delete this video.", [], privacy="unlisted")
        finally:
            path.unlink(missing_ok=True)
        applied = self.last_privacy
        return {"video_id": video_id, "requested": "unlisted", "applied": applied, "locked": applied == "private"}

    def performance(self, video_ids: list[str], since: date) -> dict[str, tuple[int, int]]:
        """views and subscribersGained per video since a date."""
        if not video_ids:
            return {}
        resp = self.analytics.reports().query(
            ids="channel==MINE",
            startDate=since.isoformat(),
            endDate=(date.today() + timedelta(days=1)).isoformat(),
            metrics="views,subscribersGained",
            dimensions="video",
            filters="video==" + ",".join(video_ids[:200]),
            maxResults=200,
        ).execute()
        out = {vid: (0, 0) for vid in video_ids}
        for vid, views, subs in resp.get("rows", []):
            out[vid] = (int(views), int(subs))
        # Analytics lags about two days; fall back to live view counts when it's empty.
        missing = [v for v, (views, _) in out.items() if views == 0]
        if missing:
            live = self.api.videos().list(part="statistics", id=",".join(missing[:50])).execute()
            for item in live.get("items", []):
                out[item["id"]] = (int(item["statistics"].get("viewCount", 0)), out[item["id"]][1])
        return out
