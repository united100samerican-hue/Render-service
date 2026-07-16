from __future__ import annotations

import asyncio
import inspect
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
import yt_dlp
from telethon import TelegramClient
from telethon.sessions import StringSession

from TikTokLive import TikTokLiveClient
from TikTokLive.events import ConnectEvent, DisconnectEvent, RoomUserSeqEvent

try:
    from pytgcalls import PyTgCalls
except Exception:  # pragma: no cover
    PyTgCalls = None

AudioPiped = None
AudioVideoPiped = None
for _import_path in (
    "pytgcalls.types.input_stream",
    "pytgcalls.types.input_streams",
    "pytgcalls.types",
):
    try:
        _mod = __import__(_import_path, fromlist=["AudioPiped", "AudioVideoPiped"])
        AudioPiped = getattr(_mod, "AudioPiped", AudioPiped)
        AudioVideoPiped = getattr(_mod, "AudioVideoPiped", AudioVideoPiped)
    except Exception:
        pass

logger = logging.getLogger("tiktok_service")

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

TMP_ROOT = Path(os.getenv("TIKTOK_TMP_ROOT", "/tmp/tiktok-service")).resolve()
TMP_ROOT.mkdir(parents=True, exist_ok=True)


@dataclass
class TikTokSession:
    chat_id: int
    client: Optional[TikTokLiveClient] = None
    viewers: int = 0
    title: str = ""
    username: str = ""
    source_url: str = ""
    is_active: bool = False
    started_at: float = 0.0
    last_seen_at: float = 0.0
    task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    temp_files: set[str] = field(default_factory=set)

    def duration_seconds(self) -> int:
        if not self.started_at:
            return 0
        end = self.last_seen_at if (not self.is_active and self.last_seen_at >= self.started_at) else time.time()
        return max(0, int(end - self.started_at))

    def as_state(self) -> Dict[str, Any]:
        return {
            "status": "playing" if self.is_active else "idle",
            "viewers": int(self.viewers or 0),
            "title": self.title or "",
            "username": self.username or "",
            "source_url": self.source_url or "",
            "duration": self.duration_seconds(),
            "elapsed": self.duration_seconds(),
            "started_at": int(self.started_at) if self.started_at else 0,
            "last_seen_at": int(self.last_seen_at) if self.last_seen_at else 0,
        }


@dataclass
class StartRequest:
    chat_id: int
    tiktok_url: str
    video: bool = True


class TikTokService:
    def __init__(self):
        self.api_id = API_ID
        self.api_hash = API_HASH
        self.session_string = SESSION_STRING
        if not self.api_id or not self.api_hash or not self.session_string:
            raise RuntimeError("missing_tiktok_env")

        self.client: Optional[TelegramClient] = None
        self.pytgcalls: Optional[PyTgCalls] = None
        self.sessions: Dict[int, TikTokSession] = {}
        self._boot_lock = asyncio.Lock()
        self._ready = False

    @property
    def ready(self) -> bool:
        return self._ready

    def sessions_count(self) -> int:
        return sum(1 for s in self.sessions.values() if s.is_active)

    def _cookie_file_path(self) -> Optional[str]:
        cookiefile = os.getenv("TIKTOK_COOKIES_FILE", "").strip()
        if cookiefile:
            if Path(cookiefile).exists():
                return cookiefile
            logger.warning("TIKTOK_COOKIES_FILE is set but file does not exist: %s", cookiefile)

        raw_cookies = os.getenv("TIKTOK_COOKIES_TEXT", "").strip() or os.getenv("TIKTOK_COOKIES", "").strip()
        if raw_cookies:
            tmp_path = Path(tempfile.gettempdir()) / "tiktok_cookies.txt"
            try:
                tmp_path.write_text(raw_cookies, encoding="utf-8")
                return str(tmp_path)
            except Exception:
                logger.exception("Failed to write TikTok cookies to temp file")
        return None

    async def _maybe_await(self, value: Any) -> Any:
        return await value if inspect.isawaitable(value) else value

    def _remember_temp(self, session: TikTokSession, path: str) -> str:
        session.temp_files.add(path)
        return path

    def _extract_unique_id(self, url: str) -> Optional[str]:
        if not url:
            return None
        txt = str(url).strip()
        for pattern in (r"@([\w\.-]+)", r"tiktok\.com/@([\w\.-]+)"):
            m = __import__("re").search(pattern, txt, __import__("re").I)
            if m:
                return m.group(1)
        return None

    def _pick_stream_url(self, info: dict) -> Optional[str]:
        for key in ("url", "play_url", "stream_url"):
            val = info.get(key)
            if val:
                return str(val)
        fmts = info.get("formats") or []
        best = None
        best_score = -1
        for f in fmts:
            score = 0
            if f.get("acodec") and f.get("acodec") != "none":
                score += 1
            if f.get("vcodec") and f.get("vcodec") != "none":
                score += 1
            if score > best_score and f.get("url"):
                best_score = score
                best = f
        return best.get("url") if best else None

    async def _get_stream_url(self, url: str) -> Optional[str]:
        try:
            ydl_opts = {"format": "best", "quiet": True, "no_warnings": True, "noplaylist": True}
            cookiefile = self._cookie_file_path()
            if cookiefile:
                ydl_opts["cookiefile"] = cookiefile

            def _extract() -> Optional[str]:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=False)
                    if not info:
                        return None
                    if info.get("is_live") and info.get("url"):
                        return str(info["url"])
                    return self._pick_stream_url(info)

            return await asyncio.to_thread(_extract)
        except Exception as exc:
            logger.warning("TikTok stream extraction failed: %s", exc)
            return None

    async def _join_with_fallbacks(self, chat_id: int, stream_url: str, video: bool) -> None:
        if not self.pytgcalls:
            raise RuntimeError("pytgcalls_not_ready")

        joined = False
        if video and AudioVideoPiped is not None and hasattr(self.pytgcalls, "join_group_call"):
            try:
                await self.pytgcalls.join_group_call(chat_id, AudioVideoPiped(stream_url))
                joined = True
            except Exception as exc:
                logger.warning("AudioVideoPiped join failed: %s", exc)

        if not joined and AudioPiped is not None and hasattr(self.pytgcalls, "join_group_call"):
            try:
                await self.pytgcalls.join_group_call(chat_id, AudioPiped(stream_url))
                joined = True
            except Exception as exc:
                logger.warning("AudioPiped join failed: %s", exc)

        if not joined and hasattr(self.pytgcalls, "play"):
            try:
                await self.pytgcalls.play(chat_id, stream_url)
                joined = True
            except Exception as exc:
                logger.warning("play() fallback failed: %s", exc)

        if not joined:
            raise RuntimeError("tiktok_join_failed")

    def _attach_events(self, session: TikTokSession) -> None:
        if not session.client:
            return

        @session.client.on(ConnectEvent)
        async def on_connect(_: ConnectEvent):
            session.is_active = True
            now = time.time()
            session.last_seen_at = now
            if not session.started_at:
                session.started_at = now

        @session.client.on(RoomUserSeqEvent)
        async def on_viewers(event: RoomUserSeqEvent):
            session.viewers = int(getattr(event, "user_count", getattr(event, "viewer_count", 0)) or 0)
            session.last_seen_at = time.time()

        @session.client.on(DisconnectEvent)
        async def on_disconnect(_: DisconnectEvent):
            session.is_active = False
            session.last_seen_at = time.time()

        async def _run_client():
            try:
                await session.client.start()
            except Exception as exc:
                logger.warning("TikTok client task stopped: %s", exc)

        session.task = asyncio.create_task(_run_client())

    async def boot(self) -> None:
        if self._ready:
            return
        async with self._boot_lock:
            if self._ready:
                return
            self.client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
            if not self.client.is_connected():
                await self._maybe_await(self.client.start())
            self.pytgcalls = PyTgCalls(self.client)
            await self._maybe_await(self.pytgcalls.start())
            self._ready = True
            logger.info("TikTokService booted successfully")

    def _ensure_session(self, chat_id: int) -> TikTokSession:
        session = self.sessions.get(chat_id)
        if not session:
            session = TikTokSession(chat_id=chat_id)
            self.sessions[chat_id] = session
        return session

    async def start(self, payload: StartRequest) -> Dict[str, Any]:
        await self.boot()
        session = self._ensure_session(payload.chat_id)

        async with session.lock:
            try:
                tiktok_url = (payload.tiktok_url or "").strip()
                if not tiktok_url:
                    return {"ok": False, "error": "رابط تيك توك غير موجود"}

                stream_url = await self._get_stream_url(tiktok_url)
                if not stream_url:
                    return {"ok": False, "error": "تعذر استخراج رابط البث"}

                unique_id = self._extract_unique_id(tiktok_url)
                session.client = TikTokLiveClient(unique_id=unique_id) if unique_id else None
                session.source_url = tiktok_url
                session.title = "TikTok Live"
                session.username = unique_id or "unknown"
                session.viewers = 0
                session.is_active = False
                session.started_at = 0.0
                session.last_seen_at = 0.0

                if session.client:
                    self._attach_events(session)

                await self._join_with_fallbacks(payload.chat_id, stream_url, payload.video)

                now = time.time()
                session.is_active = True
                session.started_at = now
                session.last_seen_at = now

                return {"ok": True, "state": session.as_state()}
            except Exception as e:
                logger.error("TikTok start error: %s", e)
                return {"ok": False, "error": str(e)}

    async def stop(self, chat_id: int) -> Dict[str, Any]:
        session = self.sessions.get(chat_id)
        if not session or not session.is_active:
            return {"ok": False, "error": "لا يوجد بث نشط"}

        async with session.lock:
            try:
                if self.pytgcalls:
                    try:
                        if hasattr(self.pytgcalls, "leave_group_call"):
                            await self.pytgcalls.leave_group_call(chat_id)
                    except Exception:
                        pass

                    try:
                        if hasattr(self.pytgcalls, "stop"):
                            await self.pytgcalls.stop(chat_id)
                    except Exception:
                        pass

                if session.client:
                    try:
                        await session.client.disconnect()
                    except Exception:
                        pass

                if session.task and not session.task.done():
                    session.task.cancel()

                session.last_seen_at = time.time()
                session.is_active = False
                session.viewers = 0

                return {"ok": True, "state": session.as_state()}
            except Exception as e:
                logger.error("TikTok stop error: %s", e)
                return {"ok": False, "error": str(e)}

    async def state(self, chat_id: int) -> Dict[str, Any]:
        session = self.sessions.get(chat_id)
        if not session:
            return {"status": "idle", "viewers": 0, "title": "", "username": "", "source_url": "", "duration": 0, "elapsed": 0}
        return session.as_state()

    async def cleanup(self, chat_id: int) -> None:
        session = self.sessions.get(chat_id)
        if not session:
            return
        async with session.lock:
            try:
                if session.task and not session.task.done():
                    session.task.cancel()
            finally:
                session.is_active = False
                session.viewers = 0
                if session.client:
                    try:
                        await session.client.disconnect()
                    except Exception:
                        pass
                for p in list(session.temp_files):
                    try:
                        Path(p).unlink(missing_ok=True)
                    except Exception:
                        pass
                session.temp_files.clear()

    async def shutdown(self) -> None:
        for chat_id in list(self.sessions.keys()):
            await self.cleanup(chat_id)
        if self.pytgcalls:
            try:
                if hasattr(self.pytgcalls, "stop"):
                    await self.pytgcalls.stop()
            except Exception:
                pass
        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass


service = TikTokService()