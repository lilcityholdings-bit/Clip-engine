"""YouTube Data API and Analytics API access for one channel."""
from datetime import date, timedelta
from pathlib import Path

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

from .config import Settings

SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
CATEGORY_EDUCATION = "27"


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

    def upload(self, path: Path, title: str, description: str, tags: list[str]) -> str:
        body = {
            "snippet": {
                "title": title[:100],
                "description": description[:5000],
                "tags": tags[:15],
                "categoryId": CATEGORY_EDUCATION,
            },
            "status": {
                "privacyStatus": self.settings.privacy_status,
                "selfDeclaredMadeForKids": False,
            },
        }
        media = MediaFileUpload(str(path), mimetype="video/mp4", chunksize=8 * 1024 * 1024, resumable=True)
        request = self.api.videos().insert(part="snippet,status", body=body, media_body=media)
        response = None
        while response is None:
            _, response = request.next_chunk()
        return response["id"]

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
