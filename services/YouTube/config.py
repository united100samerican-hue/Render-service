from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os


@dataclass(frozen=True, slots=True)
class Settings:
    api_id: int
    api_hash: str
    session_string: str
    keepalive_secret: str
    media_dir: Path
    max_media_bytes: int
    max_duration: int
    request_timeout: int
    max_height: int
    cookies_file: Path | None

    @classmethod
    def from_env(cls) -> "Settings":
        media_dir = Path(os.getenv("SOCIAL_MEDIA_DIR", "/tmp/render_social_media")).resolve()
        media_dir.mkdir(parents=True, exist_ok=True)
        cookie_raw = os.getenv("SOCIAL_COOKIES_FILE", "").strip()
        return cls(
            api_id=int(os.getenv("API_ID", "0") or 0),
            api_hash=os.getenv("API_HASH", "").strip(),
            session_string=os.getenv("SESSION_STRING", "").strip(),
            keepalive_secret=os.getenv("KEEPALIVE_SECRET", "").strip(),
            media_dir=media_dir,
            max_media_bytes=max(8 * 1024 * 1024, int(os.getenv("SOCIAL_MAX_MEDIA_BYTES", str(1024**3)))),
            max_duration=max(0, int(os.getenv("SOCIAL_MAX_DURATION", str(6 * 60 * 60)))),
            request_timeout=max(15, int(os.getenv("SOCIAL_REQUEST_TIMEOUT", "120"))),
            max_height=max(360, int(os.getenv("SOCIAL_MAX_HEIGHT", "720"))),
            cookies_file=Path(cookie_raw).resolve() if cookie_raw else None,
        )