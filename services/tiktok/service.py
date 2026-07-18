from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

from bridge import TelegramToTikTokBridge

logger = logging.getLogger("tiktok.service")


def _s(v: Any) -> str:
    return str(v or "").strip()


class TikTokService:
    def __init__(self) -> None:
        self._boot_lock = asyncio.Lock()
        self._booted = False
        self.ready = False
        self.backend_error = ""
        self.client: TelegramClient | None = None
        self.bridge: TelegramToTikTokBridge | None = None

    async def boot(self) -> None:
        if self._booted:
            return
        async with self._boot_lock:
            if self._booted:
                return

            api_id = _s(os.getenv("API_ID"))
            api_hash = _s(os.getenv("API_HASH"))
            session_string = _s(os.getenv("SESSION_STRING"))

            if not api_id or not api_hash or not session_string:
                self.ready = False
                self.backend_error = "missing_env"
                self._booted = True
                return

            try:
                self.client = TelegramClient(
                    StringSession(session_string),
                    int(api_id),
                    api_hash,
                )
                await self.client.start()

                self.bridge = TelegramToTikTokBridge(
                    self.client,
                    ffmpeg_bin=_s(os.getenv("FFMPEG_BIN")) or "ffmpeg",
                    video_size=_s(os.getenv("TIKTOK_VIDEO_SIZE")) or "1280x720",
                    fps=int(_s(os.getenv("TIKTOK_FPS")) or "30"),
                    audio_bitrate_kbps=int(_s(os.getenv("TIKTOK_AUDIO_BITRATE")) or "128"),
                    output_sample_rate=int(_s(os.getenv("TIKTOK_SAMPLE_RATE")) or "48000"),
                    output_channels=int(_s(os.getenv("TIKTOK_CHANNELS")) or "2"),
                )

                self.ready = True
                self.backend_error = ""
                self._booted = True
                logger.info("TikTok service booted")

            except Exception as e:
                self.ready = False
                self.backend_error = f"{type(e).__name__}: {e}"
                self._booted = True
                logger.exception("TikTok service boot failed")

    def _chat_id(self, payload: dict[str, Any]) -> int:
        raw = payload.get("chatId") or payload.get("chat_id") or payload.get("chat_id_") or 0
        return int(raw)

    def _mode(self, payload: dict[str, Any]) -> str:
        return _s(payload.get("mode")) or "bridge_audio"

    def _rtmp_url(self, payload: dict[str, Any]) -> str:
        source_url = _s(payload.get("source_url"))
        rtmp_url = _s(payload.get("rtmp_url"))
        env_url = _s(os.getenv("TIKTOK_RTMP_URL"))

        if rtmp_url:
            return rtmp_url
        if env_url:
            return env_url
        if source_url.startswith("rtmp://") or source_url.startswith("rtmps://"):
            return source_url
        return ""

    async def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        if not self.ready or not self.bridge:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error or 'missing_env'}"}

        chat_id = self._chat_id(payload)
        title = _s(payload.get("title")) or "TikTok Live"
        mode = self._mode(payload)
        join_as = payload.get("join_as")
        invite_hash = _s(payload.get("invite_hash")) or None

        if mode != "bridge_audio":
            return {"ok": False, "error": "live_mode_not_supported_in_this_pack"}

        rtmp_url = self._rtmp_url(payload)
        if not rtmp_url:
            return {"ok": False, "error": "missing_tiktok_rtmp_url"}

        return await self.bridge.start(
            chat_id=chat_id,
            rtmp_url=rtmp_url,
            title=title,
            join_as=join_as,
            invite_hash=invite_hash,
        )

    async def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        if not self.ready or not self.bridge:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error or 'missing_env'}"}
        chat_id = self._chat_id(payload)
        return await self.bridge.stop(chat_id)

    async def state(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        if not self.ready or not self.bridge:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error or 'missing_env'}"}
        chat_id = self._chat_id(payload)
        return {"ok": True, "state": self.bridge.state(chat_id)}

    async def meta(self, payload: dict[str, Any]) -> dict[str, Any]:
        chat_id = self._chat_id(payload)
        title = _s(payload.get("title"))
        if self.bridge:
            st = self.bridge._get_state(chat_id)  # internal but practical for this service
            if title:
                st.title = title
            if _s(payload.get("mode")):
                st.mode = _s(payload.get("mode"))
            st.last_seen_at = time.time()
            return {"ok": True, "state": st.public()}
        return {"ok": False, "error": "bridge_not_ready"}


service = TikTokService()