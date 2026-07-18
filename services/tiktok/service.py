from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
import yt_dlp
from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    from pytgcalls import PyTgCalls
except Exception:
    PyTgCalls = None

AudioPiped = None
AudioVideoPiped = None
for _mod_name in ("pytgcalls.types.input_stream", "pytgcalls.types.input_streams", "pytgcalls.types"):
    try:
        _mod = __import__(_mod_name, fromlist=["AudioPiped", "AudioVideoPiped"])
        AudioPiped = AudioPiped or getattr(_mod, "AudioPiped", None)
        AudioVideoPiped = AudioVideoPiped or getattr(_mod, "AudioVideoPiped", None)
    except Exception:
        pass

logger = logging.getLogger("tiktok_service")

API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
TMP_ROOT = Path(os.getenv("TIKTOK_TMP_ROOT", "/tmp/tiktok-service")).resolve()
TMP_ROOT.mkdir(parents=True, exist_ok=True)


@dataclass
class StartRequest:
    chat_id: int
    tiktok_url: str
    video: bool = True
    mode: str = "live"


@dataclass
class TikTokSession:
    chat_id: int
    source_url: str = ""
    username: str = ""
    title: str = ""
    viewers: int = 0
    duration: int = 0
    status: str = "idle"
    mode: str = "live"
    panel_message_id: int = 0
    live_message_id: int = 0
    updated_at: int = 0
    started_at: float = 0.0
    last_seen_at: float = 0.0
    is_active: bool = False
    last_error: str = ""
    temp_files: set[str] = field(default_factory=set)
    task: Optional[asyncio.Task] = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def as_state(self) -> dict[str, Any]:
        elapsed = int(time.time() - self.started_at) if self.started_at else 0
        return {
            "status": "playing" if self.is_active else "idle",
            "mode": self.mode,
            "viewers": int(self.viewers or 0),
            "title": self.title or "",
            "username": self.username or "",
            "source_url": self.source_url or "",
            "duration": int(self.duration or 0),
            "elapsed": elapsed,
            "started_at": int(self.started_at) if self.started_at else 0,
            "last_seen_at": int(self.last_seen_at) if self.last_seen_at else 0,
        }


class TikTokService:
    def __init__(self) -> None:
        self.ready = False
        self.backend_error = ""
        self.client: Optional[TelegramClient] = None
        self.pytgcalls: Optional[Any] = None
        self.sessions: dict[int, TikTokSession] = {}
        self._boot_lock = asyncio.Lock()
        self._booted = False

    def sessions_count(self) -> int:
        return sum(1 for s in self.sessions.values() if s.is_active)

    async def _maybe(self, v: Any) -> Any:
        return await v if inspect.isawaitable(v) else v

    def _ensure_session(self, chat_id: int) -> TikTokSession:
        s = self.sessions.get(chat_id)
        if not s:
            s = TikTokSession(chat_id=chat_id)
            self.sessions[chat_id] = s
        return s

    def _tmp(self, suffix: str) -> str:
        fd, path = tempfile.mkstemp(prefix="tiktok_", suffix=suffix, dir=str(TMP_ROOT))
        os.close(fd)
        return path

    def _remember(self, s: TikTokSession, path: str) -> str:
        s.temp_files.add(path)
        return path

    def _cookie_file_path(self) -> Optional[str]:
        cookiefile = os.getenv("TIKTOK_COOKIES_FILE", "").strip()
        if cookiefile and Path(cookiefile).exists():
            return cookiefile
        raw = (os.getenv("TIKTOK_COOKIES_TEXT", "").strip() or os.getenv("TIKTOK_COOKIES", "").strip())
        if raw:
            p = Path(tempfile.gettempdir()) / "tiktok_cookies.txt"
            try:
                p.write_text(raw, encoding="utf-8")
                return str(p)
            except Exception:
                logger.exception("Failed to write cookie file")
        return None

    def _extract_unique_id(self, url: str) -> Optional[str]:
        txt = str(url or "").strip()
        if not txt:
            return None
        m = re.search(r"(?:tiktok\.com/@|@)([\w\.-]+)", txt, re.I)
        return m.group(1) if m else None

    def _pick_stream_url(self, info: dict) -> Optional[str]:
        for key in ("url", "play_url", "stream_url"):
            if info.get(key):
                return str(info[key])
        fmts = info.get("formats") or []
        best, score = None, -1
        for f in fmts:
            if not f.get("url"):
                continue
            s = 0
            if f.get("acodec") and f.get("acodec") != "none":
                s += 1
            if f.get("vcodec") and f.get("vcodec") != "none":
                s += 1
            if s > score:
                best, score = f, s
        return best.get("url") if best else None

    async def _get_stream_url(self, url: str) -> Optional[str]:
        cookiefile = self._cookie_file_path()
        ydl_opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "format": "best"}
        if cookiefile:
            ydl_opts["cookiefile"] = cookiefile

        def _extract() -> Optional[str]:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
                if not info:
                    return None
                return str(info.get("url") or self._pick_stream_url(info) or "")

        try:
            return await asyncio.to_thread(_extract)
        except Exception as e:
            logger.warning("TikTok stream extraction failed: %s", e)
            return None

    async def _join(self, chat_id: int, stream_url: str, video: bool) -> None:
        if not self.pytgcalls:
            raise RuntimeError("pytgcalls_not_ready")

        joined = False

        if video and AudioVideoPiped is not None and hasattr(self.pytgcalls, "join_group_call"):
            try:
                await self.pytgcalls.join_group_call(chat_id, AudioVideoPiped(stream_url))
                joined = True
            except Exception as e:
                logger.warning("AudioVideoPiped join failed: %s", e)

        if not joined and AudioPiped is not None and hasattr(self.pytgcalls, "join_group_call"):
            try:
                await self.pytgcalls.join_group_call(chat_id, AudioPiped(stream_url))
                joined = True
            except Exception as e:
                logger.warning("AudioPiped join failed: %s", e)

        if not joined and hasattr(self.pytgcalls, "play"):
            try:
                await self.pytgcalls.play(chat_id, stream_url)
                joined = True
            except Exception as e:
                logger.warning("play() fallback failed: %s", e)

        if not joined:
            raise RuntimeError("tiktok_join_failed")

    async def boot(self) -> None:
        if self._booted:
            return
        async with self._boot_lock:
            if self._booted:
                return

            if not SESSION_STRING or not API_ID or not API_HASH:
                self.backend_error = "missing_env"
                self.ready = False
                return

            if PyTgCalls is None:
                self.backend_error = "pytgcalls_missing"
                self.ready = False
                return

            try:
                self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
                await self._maybe(self.client.start())
                self.pytgcalls = PyTgCalls(self.client)
                await self._maybe(self.pytgcalls.start())
                self.ready = True
                self.backend_error = ""
                self._booted = True
                logger.info("TikTokService booted successfully")
            except Exception as e:
                self.ready = False
                self.backend_error = f"{type(e).__name__}: {e}"
                logger.exception("TikTok boot failed")
                try:
                    if self.client:
                        await self._maybe(self.client.disconnect())
                except Exception:
                    pass
                self.client = None
                self.pytgcalls = None

    async def start(self, payload: StartRequest) -> dict[str, Any]:
        await self.boot()
        if not self.ready:
            return {"ok": False, "error": f"service_not_ready: {self.backend_error or 'missing_env'}"}

        s = self._ensure_session(payload.chat_id)
        async with s.lock:
            try:
                source = str(payload.tiktok_url or "").strip().rstrip("/")
                if not source:
                    return {"ok": False, "error": "رابط تيك توك غير موجود"}

                s.mode = "bridge_audio" if str(payload.mode or "").strip() == "bridge_audio" else "live"
                s.source_url = source
                s.username = self._extract_unique_id(source) or ""
                s.title = "TikTok Live"
                s.viewers = 0
                s.duration = 0
                s.status = "starting"
                s.is_active = False
                s.started_at = 0.0
                s.last_seen_at = time.time()
                s.last_error = ""

                stream_url = await self._get_stream_url(source)
                if not stream_url:
                    return {"ok": False, "error": "تعذر استخراج رابط البث"}

                await self._join(payload.chat_id, stream_url, payload.video)

                now = time.time()
                s.is_active = True
                s.started_at = now
                s.last_seen_at = now
                s.status = "playing"
                return {"ok": True, "state": s.as_state()}
            except Exception as e:
                s.status = "error"
                s.last_error = f"{type(e).__name__}: {e}"
                logger.exception("TikTok start error")
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def stop(self, chat_id: int) -> dict[str, Any]:
        s = self.sessions.get(chat_id)
        if not s:
            return {"ok": False, "error": "لا توجد جلسة"}

        async with s.lock:
            try:
                if self.pytgcalls:
                    for name in ("leave_group_call", "stop", "leave"):
                        fn = getattr(self.pytgcalls, name, None)
                        if not callable(fn):
                            continue
                        try:
                            if name == "stop":
                                try:
                                    await self._maybe(fn(chat_id))
                                except TypeError:
                                    await self._maybe(fn())
                            else:
                                try:
                                    await self._maybe(fn())
                                except TypeError:
                                    await self._maybe(fn(chat_id))
                            break
                        except Exception as e:
                            logger.warning("TikTok leave failed (%s): %s", name, e)

                if s.task and not s.task.done():
                    s.task.cancel()
                s.task = None

                s.is_active = False
                s.viewers = 0
                s.status = "stopped"
                s.last_seen_at = time.time()
                s.last_error = ""
                return {"ok": True, "state": s.as_state()}
            except Exception as e:
                s.last_error = f"{type(e).__name__}: {e}"
                logger.exception("TikTok stop error")
                return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    async def state(self, chat_id: int) -> dict[str, Any]:
        s = self.sessions.get(chat_id)
        return s.as_state() if s else {
            "status": "idle",
            "mode": "live",
            "viewers": 0,
            "title": "",
            "username": "",
            "source_url": "",
            "duration": 0,
            "elapsed": 0,
        }


service = TikTokService()