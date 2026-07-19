from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from telethon import TelegramClient
from telethon.sessions import StringSession

from bridge import create_bridge, TelegramToTikTokBridge
from player import create_player, TikTokPlayer
from receiver import receiver
from session import sessions
from config import settings

log = logging.getLogger("tiktok.service")
cfg = settings()


class TikTokService:
    def __init__(self) -> None:
        self.ready = False
        self.backend_error = ""
        self._booted = False
        self._boot_lock = asyncio.Lock()
        self.client: TelegramClient | None = None
        self.player: TikTokPlayer | None = None
        self.bridge: TelegramToTikTokBridge | None = None

    async def boot(self) -> None:
        if self._booted:
            return

        async with self._boot_lock:
            if self._booted:
                return

            api_id = cfg.api_id
            api_hash = cfg.api_hash
            session_string = cfg.session_string

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

                self.player = create_player(self.client)
                self.bridge = create_bridge(self.client)

                self.ready = True
                self.backend_error = ""
                self._booted = True
                log.info("TikTok service booted successfully")
            except Exception as e:
                self.ready = False
                self.backend_error = f"{type(e).__name__}: {e}"
                self._booted = True
                log.exception("TikTok service boot failed")

    def _chat_id(self, payload: dict[str, Any]) -> int:
        raw = payload.get("chatId") or payload.get("chat_id") or 0
        return int(raw)

    def _rtmp_url(self, payload: dict[str, Any]) -> str:
        rtmp = str(payload.get("rtmp_url") or "").strip()
        if rtmp:
            return rtmp
        env_url = str(os.getenv("TIKTOK_RTMP_URL") or "").strip()
        if env_url:
            return env_url
        source = str(payload.get("source_url") or "").strip()
        if source.startswith("rtmp"):
            return source
        return ""

    async def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        يبدأ دائماً بنقل بث تيك توك → مكالمة تليجرام (فيديو + صوت)
        """
        await self.boot()
        if not self.ready or not self.player:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error}"}

        chat_id = self._chat_id(payload)
        source_url = str(payload.get("source_url") or "").strip()
        title = str(payload.get("title") or "TikTok Live").strip()
        join_as = payload.get("join_as")
        invite_hash = str(payload.get("invite_hash") or "") or None

        if not source_url:
            return {"ok": False, "error": "missing_source_url"}

        # 1. استخراج روابط البث من تيك توك
        resolved = await receiver.resolve(chat_id=chat_id, url=source_url)
        if not resolved.get("ok"):
            return {"ok": False, "error": resolved.get("error") or "resolve_failed"}

        video_url = resolved["video_url"]
        audio_url = resolved.get("audio_url") or video_url
        final_title = resolved.get("title") or title

        s = sessions.get(chat_id)
        s.source_url = source_url
        s.title = final_title
        s.join_as = join_as
        s.invite_hash = invite_hash
        s.touch()

        # 2. تشغيل البث داخل مكالمة تليجرام (المكتبة القديمة)
        result = await self.player.start(
            chat_id=chat_id,
            video_url=video_url,
            audio_url=audio_url,
            title=final_title,
            join_as=join_as,
            invite_hash=invite_hash,
        )

        if not result.get("ok"):
            return result

        return {
            "ok": True,
            "state": sessions.get(chat_id).public(),
            "resolved": {
                "title": final_title,
                "thumbnail": resolved.get("thumbnail") or "",
                "uploader": resolved.get("uploader") or "",
            },
        }

    async def bridge_enable(self, payload: dict[str, Any]) -> dict[str, Any]:
        """
        تفعيل الجسر العكسي: صوت مكالمة تليجرام → تيك توك
        (يعمل فوق طبقة الفيديو الحالية)
        """
        await self.boot()
        if not self.ready or not self.bridge:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error}"}

        chat_id = self._chat_id(payload)
        rtmp_url = self._rtmp_url(payload)
        title = str(payload.get("title") or sessions.get(chat_id).title or "TikTok Live")

        if not rtmp_url:
            return {"ok": False, "error": "missing_tiktok_rtmp_url"}

        result = await self.bridge.enable(
            chat_id=chat_id,
            rtmp_url=rtmp_url,
            title=title,
            join_as=payload.get("join_as"),
            invite_hash=str(payload.get("invite_hash") or "") or None,
        )
        return result

    async def bridge_disable(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        if not self.ready or not self.bridge:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error}"}

        chat_id = self._chat_id(payload)
        return await self.bridge.disable(chat_id)

    async def stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        chat_id = self._chat_id(payload)

        # إيقاف الجسر أولاً
        if self.bridge:
            await self.bridge.disable(chat_id)

        # ثم إيقاف مشغل الفيديو
        if self.player:
            await self.player.stop(chat_id)

        sessions.reset(chat_id)
        return {"ok": True, "state": sessions.get(chat_id).public()}

    async def state(self, payload: dict[str, Any]) -> dict[str, Any]:
        await self.boot()
        chat_id = self._chat_id(payload)
        return {"ok": True, "state": sessions.get(chat_id).public()}

    async def meta(self, payload: dict[str, Any]) -> dict[str, Any]:
        source_url = str(payload.get("source_url") or "").strip()
        if not source_url:
            return {"ok": False, "error": "missing_source_url"}

        meta = await receiver.metadata(source_url)
        return meta


service = TikTokService()