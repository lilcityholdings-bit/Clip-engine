"""Settings read from environment variables. See .env.example."""
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    anthropic_model: str = "claude-opus-5"
    youtube_client_id: str = field(default_factory=lambda: os.environ.get("YOUTUBE_CLIENT_ID", ""))
    youtube_client_secret: str = field(default_factory=lambda: os.environ.get("YOUTUBE_CLIENT_SECRET", ""))
    youtube_refresh_token: str = field(default_factory=lambda: os.environ.get("YOUTUBE_REFRESH_TOKEN", ""))
    youtube_api_key: str = field(default_factory=lambda: os.environ.get("YOUTUBE_API_KEY", ""))
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")))
    max_uploads_per_day: int = field(default_factory=lambda: int(os.environ.get("MAX_UPLOADS_PER_DAY", "5")))
    privacy_status: str = field(default_factory=lambda: os.environ.get("PRIVACY_STATUS", "public"))
    trend_region: str = field(default_factory=lambda: os.environ.get("TREND_REGION", "US"))
    score_after_hours: int = field(default_factory=lambda: int(os.environ.get("SCORE_AFTER_HOURS", "48")))
    whisper_model: str = field(default_factory=lambda: os.environ.get("WHISPER_MODEL", "base"))
    digest_webhook_url: str = field(default_factory=lambda: os.environ.get("DIGEST_WEBHOOK_URL", ""))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "clip_engine.db"

    @property
    def work_dir(self) -> Path:
        return self.data_dir / "work"
