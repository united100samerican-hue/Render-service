from __future__ import annotations

import os


def _s(name: str, default: str = "") -> str:
    return str(os.getenv(name, default) or "").strip()


def _i(name: str, default: int = 0) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except Exception:
        return default


API_ID = _i("API_ID")
API_HASH = _s("API_HASH")
AUDIO_SESSION_STRING = _s("AUDIO_SESSION_STRING")
SESSION_STRING = AUDIO_SESSION_STRING or _s("SESSION_STRING")
BOT_TOKEN = _s("BOT_TOKEN")
KEEPALIVE_SECRET = _s("KEEPALIVE_SECRET")

# Local-only BgUtils provider. No Render/Web environment variable is required.
POT_PROVIDER_URL = "http://127.0.0.1:4416"

YOUTUBE_COOKIES = _s("YOUTUBE_COOKIES")

R2_ENDPOINT = _s("R2_ENDPOINT")
R2_BUCKET = _s("R2_BUCKET", "audio-cache")
R2_ACCESS_KEY_ID = _s("R2_ACCESS_KEY_ID")
R2_SECRET_ACCESS_KEY = _s("R2_SECRET_ACCESS_KEY")

try:
    R2_PRESIGN_SECONDS = max(300, min(604800, int(_s("R2_PRESIGN_SECONDS", "86400"))))
except Exception:
    R2_PRESIGN_SECONDS = 86400

AUDIO_CACHE_ENABLED = bool(
    R2_ENDPOINT
    and R2_BUCKET
    and R2_ACCESS_KEY_ID
    and R2_SECRET_ACCESS_KEY
)
