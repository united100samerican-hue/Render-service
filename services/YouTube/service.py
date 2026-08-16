from __future__ import annotations

import time

from telethon import TelegramClient
from telethon.sessions import StringSession

from .cleanup import Cleaner
from .config import Settings
from .errors import SocialMediaError
from .extractor import Extractor
from .models import MediaRequest, Session
from .player import Player
from .session import SessionStore


class SocialMediaService:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self.extractor = Extractor(settings)
        self.cleaner = Cleaner()
        self.sessions = SessionStore()
        self.client: TelegramClient | None = None
        self.player: Player | None = None
        self.ready = False
        self.backend_error = ""

    async def ensure_ready(self) -> None:
        if self.ready:
            return
        if not self.s.api_id or not self.s.api_hash or not self.s.session_string:
            self.backend_error = "missing_env: API_ID/API_HASH/SESSION_STRING"
            return
        try:
            if self.client is None:
                self.client = TelegramClient(StringSession(self.s.session_string), self.s.api_id, self.s.api_hash)
            if not self.client.is_connected():
                await self.client.connect()
            if not await self.client.is_user_authorized():
                self.backend_error = "telegram_session_not_authorized"
                return
            if self.player is None:
                self.player = Player(self.client)
                await self.player.start()
            self.ready = True
            self.backend_error = ""
        except Exception as exc:
            self.ready = False
            self.backend_error = f"{type(exc).__name__}: {exc}"

    async def close(self) -> None:
        for session in self.sessions.values():
            await self.cleaner.remove(session.local_path)
        self.sessions = SessionStore()
        if self.player:
            await self.player.stop()
        if self.client:
            await self.client.disconnect()
        self.player = None
        self.client = None
        self.ready = False

    async def meta(self, request: MediaRequest) -> dict:
        await self.ensure_ready()
        if not self.ready:
            return {"ok": False, "action": "meta", "error": "service_not_ready", "detail": self.backend_error}
        info = await self.extractor.inspect(request.url)
        return {
            "ok": True,
            "action": "meta",
            "state": {
                "chat_id": request.chat_id,
                "title": info.title,
                "source_url": info.webpage_url,
                "duration": info.duration,
                "thumbnail": info.thumbnail,
                "extractor": info.extractor,
                "source_id": info.source_id,
                "video": info.has_video,
                "live": info.is_live,
                "has_audio": info.has_audio,
                "direct_available": bool(info.direct_url),
                "cookies_configured": bool(self.s.cookies_file and self.s.cookies_file.is_file()),
            },
        }

    async def _switch_locked(self, request: MediaRequest, action: str) -> dict:
        if not self.ready or not self.player:
            return {"ok": False, "action": action, "error": "service_not_ready", "detail": self.backend_error}
        info = await self.extractor.inspect(request.url)
        previous = self.sessions.get(request.chat_id)
        previous_path = previous.local_path
        new_path = ""
        try:
            if info.is_live:
                if not info.direct_url:
                    raise SocialMediaError("live_stream_unavailable")
                source = info.direct_url
            else:
                new_path = await self.extractor.download_vod(info)
                source = new_path

            await self.player.play(request.chat_id, source)

            session = Session(
                chat_id=request.chat_id,
                status="playing",
                title=info.title or request.title,
                source_url=info.webpage_url or request.url,
                duration=info.duration or request.duration,
                position=max(0, request.offset),
                thumbnail=info.thumbnail,
                extractor=info.extractor,
                source_id=info.source_id,
                video=info.has_video,
                live=info.is_live,
                local_path=new_path,
                direct_url=info.direct_url if info.is_live else "",
                updated_at=time.time(),
                error="",
            )
            self.sessions.put(session)

            if previous_path and previous_path != new_path:
                await self.cleaner.remove(previous_path)

            return {
                "ok": True,
                "action": action,
                "played": True,
                "switched": bool(previous.source_url),
                "state": session.to_dict(),
            }
        except Exception as exc:
            if new_path:
                await self.cleaner.remove(new_path)
            previous.error = f"{type(exc).__name__}: {exc}"
            previous.updated_at = time.time()
            self.sessions.put(previous)
            if isinstance(exc, SocialMediaError):
                return {"ok": False, "action": action, "error": exc.code, "detail": exc.detail, "state": previous.to_dict()}
            return {"ok": False, "action": action, "error": type(exc).__name__, "detail": str(exc), "state": previous.to_dict()}

    async def start(self, request: MediaRequest) -> dict:
        await self.ensure_ready()
        async with self.sessions.lock(request.chat_id):
            return await self._switch_locked(request, "start")

    async def next(self, request: MediaRequest) -> dict:
        await self.ensure_ready()
        async with self.sessions.lock(request.chat_id):
            return await self._switch_locked(request, "next")

    async def skip(self, request: MediaRequest) -> dict:
        await self.ensure_ready()
        async with self.sessions.lock(request.chat_id):
            return await self._switch_locked(request, "skip")

    async def pause(self, chat_id: int) -> dict:
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            session = self.sessions.get(chat_id)
            if not self.player or session.status not in {"playing", "paused"}:
                return {"ok": False, "action": "pause", "error": "no_active_media"}
            try:
                await self.player.pause(chat_id)
                session.status = "paused"
                session.updated_at = time.time()
                self.sessions.put(session)
                return {"ok": True, "action": "pause", "state": session.to_dict()}
            except Exception as exc:
                session.error = f"{type(exc).__name__}: {exc}"
                self.sessions.put(session)
                return {"ok": False, "action": "pause", "error": type(exc).__name__, "detail": str(exc)}

    async def resume(self, chat_id: int) -> dict:
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            session = self.sessions.get(chat_id)
            if not self.player or session.status != "paused":
                return {"ok": False, "action": "resume", "error": "not_paused"}
            try:
                await self.player.resume(chat_id)
                session.status = "playing"
                session.updated_at = time.time()
                self.sessions.put(session)
                return {"ok": True, "action": "resume", "state": session.to_dict()}
            except Exception as exc:
                session.error = f"{type(exc).__name__}: {exc}"
                self.sessions.put(session)
                return {"ok": False, "action": "resume", "error": type(exc).__name__, "detail": str(exc)}

    async def seek(self, chat_id: int, delta: int, position: int | None = None) -> dict:
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            session = self.sessions.get(chat_id)
            if session.live:
                return {"ok": False, "action": "seek", "error": "live_seek_unsupported"}
            if not self.player or session.status not in {"playing", "paused"}:
                return {"ok": False, "action": "seek", "error": "no_active_media"}
            try:
                await self.player.seek(chat_id, int(delta))
                target = max(0, int(position) if position is not None else int(session.position) + int(delta))
                if session.duration:
                    target = min(target, session.duration)
                session.position = target
                session.updated_at = time.time()
                self.sessions.put(session)
                return {"ok": True, "action": "seek", "position": target, "state": session.to_dict()}
            except Exception as exc:
                session.error = f"{type(exc).__name__}: {exc}"
                self.sessions.put(session)
                return {"ok": False, "action": "seek", "error": type(exc).__name__, "detail": str(exc)}

    async def stop(self, chat_id: int) -> dict:
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            session = self.sessions.get(chat_id)
            try:
                left = await self.player.leave(chat_id) if self.player else False
                await self.cleaner.remove(session.local_path)
                self.sessions.remove(chat_id)
                return {"ok": True, "action": "stop", "stopped": True, "left_call": left}
            except Exception as exc:
                self.sessions.remove(chat_id)
                return {"ok": False, "action": "stop", "error": type(exc).__name__, "detail": str(exc)}

    def state(self, chat_id: int) -> dict:
        session = self.sessions.get(chat_id)
        return {
            "ok": True,
            "chat_id": int(chat_id),
            "ready": self.ready,
            "backend_error": self.backend_error,
            "active_sessions": self.sessions.count_active(),
            "session": session.to_dict(),
        }
