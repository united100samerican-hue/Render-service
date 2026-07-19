from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

from bridge import TelegramToTikTokBridge

log = logging.getLogger("tiktok.service")


def _s(v: Any) -> str:
    return str(v or "").strip()


class TikTokService:
    def __init__(self) -> None:
        self.ready = False
        self.backend_error = ""
        self._booted = False
        self._boot_lock = asyncio.Lock()
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
                log.error("TikTok service boot failed: missing env")
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
                log.info("TikTok service booted")
            except Exception as e:
                self.ready = False
                self.backend_error = f"{type(e).__name__}: {e}"
                self._booted = True
                log.exception("TikTok service boot failed")

    def _chat_id(self, payload: dict[str, Any]) -> int:
        raw = payload.get("chatId") or payload.get("chat_id") or payload.get("chat_id_") or 0
        return int(raw)

    def _mode(self, payload: dict[str, Any]) -> str:
        return _s(payload.get("mode")) or "live"

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
        rtmp_url = self._rtmp_url(payload)

        if not rtmp_url:
            return {"ok": False, "error": "missing_tiktok_rtmp_url"}

        if mode == "bridge_audio":
            return await self.bridge.enable_bridge(
                chat_id=chat_id,
                rtmp_url=rtmp_url,
                title=title,
                join_as=join_as,
                invite_hash=invite_hash,
            )

        return await self.bridge.start_live(
            chat_id=chat_id,
            rtmp_url=rtmp_url,
            title=title,
            join_as=join_as,
            invite_hash=invite_hash,
        )

    async def bridge_enable(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        if not self.ready or not self.bridge:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error or 'missing_env'}"}

        chat_id = self._chat_id(payload)
        title = _s(payload.get("title")) or "TikTok Live"
        join_as = payload.get("join_as")
        invite_hash = _s(payload.get("invite_hash")) or None
        rtmp_url = self._rtmp_url(payload)

        if not rtmp_url:
            return {"ok": False, "error": "missing_tiktok_rtmp_url"}

        return await self.bridge.enable_bridge(
            chat_id=chat_id,
            rtmp_url=rtmp_url,
            title=title,
            join_as=join_as,
            invite_hash=invite_hash,
        )

    async def bridge_disable(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        if not self.ready or not self.bridge:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error or 'missing_env'}"}

        chat_id = self._chat_id(payload)
        return await self.bridge.disable_bridge(chat_id)

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
        title = _s(payload.get("title")) or _s(payload.get("source_url")) or "TikTok Live"
        source_url = _s(payload.get("source_url"))
        return {
            "ok": True,
            "state": {
                "title": title,
                "source_url": source_url,
                "mode": _s(payload.get("mode")) or "live",
                "duration": int(float(payload.get("duration") or 0)),
                "viewers": int(float(payload.get("viewers") or 0)),
            },
        }


service = TikTokService()