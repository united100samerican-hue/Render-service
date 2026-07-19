from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from telethon import TelegramClient

try:
    from pytgcalls import GroupCallFactory
    from pytgcalls.types.input_stream import AudioVideoPiped
    from pytgcalls.types.input_stream.quality import HighQualityAudio, HighQualityVideo
except Exception:
    try:
        from py_tgcalls import GroupCallFactory
        from py_tgcalls.types.input_stream import AudioVideoPiped
        from py_tgcalls.types.input_stream.quality import HighQualityAudio, HighQualityVideo
    except Exception:
        GroupCallFactory = None
        AudioVideoPiped = None
        HighQualityAudio = None
        HighQualityVideo = None

from config import settings
from session import sessions

log = logging.getLogger("tiktok.player")
cfg = settings()


class TikTokPlayer:
    """
    المكتبة القديمة / الكلاسيكية
    تنقل بث تيك توك (فيديو + صوت) مباشرة إلى مكالمة تليجرام المرئية
    """

    def __init__(self, client: TelegramClient) -> None:
        if GroupCallFactory is None or AudioVideoPiped is None:
            raise RuntimeError("pytgcalls_video_support_unavailable")

        self.client = client
        self.factory = GroupCallFactory(client)
        self._calls: dict[int, Any] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        chat_id = int(chat_id)
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def state(self, chat_id: int) -> dict[str, Any]:
        s = sessions.get(chat_id)
        return {
            "running": s.player.running,
            "connected": s.player.connected,
            "restarting": s.player.restarting,
            "ffmpeg_pid": s.player.ffmpeg_pid,
            "last_error": s.player.last_error,
            "started_at": int(s.player.started_at or 0),
        }

    async def start(
        self,
        *,
        chat_id: int,
        video_url: str,
        audio_url: str = "",
        title: str = "TikTok Live",
        join_as: Any = None,
        invite_hash: str | None = None,
    ) -> dict[str, Any]:

        async with self._lock(chat_id):
            s = sessions.get(chat_id)

            if s.player.running:
                return {"ok": True, "state": s.public()}

            if not video_url:
                return {"ok": False, "error": "missing_video_url"}

            s.player.running = False
            s.player.connected = False
            s.player.restarting = False
            s.player.last_error = ""
            s.player.started_at = time.time()
            s.title = title or "TikTok Live"
            s.status = "starting"
            s.touch()

            try:
                group_call = self.factory.get_group_call()
                self._calls[chat_id] = group_call

                # تشغيل البث مباشرة (فيديو + صوت)
                stream = AudioVideoPiped(
                    video_url,
                    audio_parameters=HighQualityAudio(),
                    video_parameters=HighQualityVideo(),
                )

                await group_call.join(
                    chat_id,
                    stream,
                    join_as=join_as,
                    invite_hash=invite_hash,
                )

                s.player.running = True
                s.player.connected = True
                s.status = "playing"
                s.touch()

                log.info("[PLAYER] Started video stream → Telegram | chat_id=%s", chat_id)
                return {"ok": True, "state": s.public()}

            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                s.player.last_error = err
                s.status = "error"
                s.touch()
                await self.stop(chat_id)
                log.exception("[PLAYER] Failed to start chat_id=%s", chat_id)
                return {"ok": False, "error": err}

    async def restart(
        self,
        *,
        chat_id: int,
        video_url: str,
        audio_url: str = "",
    ) -> dict[str, Any]:

        s = sessions.get(chat_id)
        s.player.restarting = True
        s.touch()

        await self.stop(chat_id)
        await asyncio.sleep(1.2)

        result = await self.start(
            chat_id=chat_id,
            video_url=video_url,
            audio_url=audio_url,
            title=s.title,
            join_as=s.join_as,
            invite_hash=s.invite_hash,
        )

        s.player.restarting = False
        s.touch()
        return result

    async def stop(self, chat_id: int) -> dict[str, Any]:
        async with self._lock(chat_id):
            s = sessions.get(chat_id)
            s.player.running = False
            s.player.connected = False
            s.status = "stopping"
            s.touch()

            gc = self._calls.pop(chat_id, None)
            if gc:
                try:
                    stop_fn = getattr(gc, "stop", None) or getattr(gc, "leave", None)
                    if stop_fn:
                        maybe = stop_fn()
                        if asyncio.iscoroutine(maybe):
                            await maybe
                except Exception as e:
                    log.warning("[PLAYER] stop error: %s", e)

            s.player.ffmpeg_pid = 0
            s.player.last_error = ""
            s.status = "idle"
            s.touch()
            return {"ok": True, "state": s.public()}


player: TikTokPlayer | None = None


def create_player(client: TelegramClient) -> TikTokPlayer:
    global player
    if player is None:
        player = TikTokPlayer(client)
    return player