from __future__ import annotations

import asyncio
import inspect
import logging
import os
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    from pytgcalls import GroupCallFactory
except Exception:
    from pytgcalls.group_call_factory import GroupCallFactory  # type: ignore

logger = logging.getLogger("audio_service")

SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

TMP_ROOT = Path(os.getenv("AUDIO_TMP_ROOT", "/tmp/audio-service")).resolve()
TMP_ROOT.mkdir(parents=True, exist_ok=True)


@dataclass
class Track:
    id: str
    chat_id: int
    source_type: str = "telegram"
    source_id: str = ""
    title: str = ""
    duration: int = 0
    local_path: str = ""
    original_path: str = ""
    offset: int = 0
    created_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Session:
    chat_id: int
    gc: Any = None
    current: Optional[Track] = None
    queue: list[Track] = field(default_factory=list)
    status: str = "idle"
    paused: bool = False
    started_at: float = 0.0
    pause_started_at: float = 0.0
    paused_seconds: float = 0.0
    last_error: str = ""
    last_update_at: float = 0.0
    temp_files: set[str] = field(default_factory=set)
    ended_hooked: bool = False


class AudioService:
    def __init__(self) -> None:
        self.ready = False
        self.backend_error = ""
        self.client: Optional[TelegramClient] = None
        self.factory: Any = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self._lock = asyncio.Lock()
        self._sessions: dict[int, Session] = {}

    def active_sessions_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status in {"playing", "paused"})

    def queues_count(self) -> int:
        return sum(len(s.queue) for s in self._sessions.values())

    async def _maybe(self, v: Any) -> Any:
        return await v if inspect.isawaitable(v) else v

    def _sess(self, chat_id: int) -> Session:
        s = self._sessions.get(chat_id)
        if not s:
            s = Session(chat_id=chat_id)
            self._sessions[chat_id] = s
        return s

    def _env_ok(self) -> tuple[bool, str]:
        miss = []
        if not SESSION_STRING:
            miss.append("SESSION_STRING")
        if not API_ID:
            miss.append("API_ID")
        if not API_HASH:
            miss.append("API_HASH")
        return (False, "missing_env: " + ", ".join(miss)) if miss else (True, "")

    def _tmp(self, suffix: str) -> str:
        fd, p = tempfile.mkstemp(prefix="audio_", suffix=suffix, dir=str(TMP_ROOT))
        os.close(fd)
        return p

    def _remember(self, s: Session, path: str) -> str:
        s.temp_files.add(path)
        return path

    async def _download_http(self, url: str) -> str:
        ext = Path(url.split("?", 1)[0]).suffix or ".bin"
        out = self._tmp(ext)
        timeout = httpx.Timeout(30.0, connect=30.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                with open(out, "wb") as f:
                    async for chunk in r.aiter_bytes(256 * 1024):
                        if chunk:
                            f.write(chunk)
        return out

    async def _download_telegram(self, file_id: str) -> str:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN missing")
        timeout = httpx.Timeout(30.0, connect=30.0, read=None, write=30.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getFile", params={"file_id": file_id})
            j = r.json()
            if not j.get("ok"):
                raise RuntimeError(j.get("description") or "getFile_failed")
            file_path = j["result"]["file_path"]
            ext = Path(file_path).suffix or ".bin"
            out = self._tmp(ext)
            async with client.stream("GET", f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as dl:
                dl.raise_for_status()
                with open(out, "wb") as f:
                    async for chunk in dl.aiter_bytes(256 * 1024):
                        if chunk:
                            f.write(chunk)
        return out

    async def _prepare_source(self, source_type: str, source_id: str, s: Session) -> str:
        src = str(source_id or "").strip()
        if not src:
            raise ValueError("source_id_required")

        if src.startswith(("http://", "https://")):
            src = await self._download_http(src)
        elif source_type in {"telegram", "tg", "file_id"} or len(src) >= 20:
            if Path(src).exists():
                pass
            else:
                src = await self._download_telegram(src)
        else:
            p = Path(src).expanduser()
            if p.exists():
                src = str(p.resolve())
            else:
                raise ValueError("source_not_found")

        # normalize to OGG/OPUS so GroupCallFile always gets a stable local file
        ogg = self._tmp(".ogg")
        subprocess.run(
            ["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "libopus", "-b:a", "128k", ogg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        self._remember(s, src)
        self._remember(s, ogg)
        return ogg

    def _trim(self, src: str, offset: int, s: Session) -> str:
        if offset <= 0:
            return src
        ogg = self._tmp(".ogg")
        subprocess.run(
            ["ffmpeg", "-y", "-ss", str(max(0, int(offset))), "-i", src, "-vn", "-c:a", "libopus", "-b:a", "128k", ogg],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
        self._remember(s, ogg)
        return ogg

    async def _cleanup_temp(self, s: Session) -> None:
        for p in list(s.temp_files):
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:
                pass
        s.temp_files.clear()

    async def ensure_ready(self) -> None:
        async with self._lock:
            if self.ready:
                return
            ok, reason = self._env_ok()
            if not ok:
                self.backend_error = reason
                self.ready = False
                return
            try:
                self.loop = asyncio.get_running_loop()
                self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
                await self._maybe(self.client.start())
                self.factory = GroupCallFactory(self.client, mtproto_backend=GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON)
                self.ready = True
                self.backend_error = ""
                logger.info("audio service ready")
            except Exception as e:
                self.ready = False
                self.backend_error = f"{type(e).__name__}: {e}"
                logger.exception("audio init failed")
                try:
                    if self.client:
                        await self._maybe(self.client.disconnect())
                except Exception:
                    pass
                self.client = None
                self.factory = None

    def _state(self, chat_id: int) -> dict[str, Any]:
        s = self._sess(chat_id)
        cur = s.current.to_dict() if s.current else None
        elapsed = 0
        if s.status == "playing":
            elapsed = max(0, int(time.time() - s.started_at - s.paused_seconds))
        elif s.status == "paused" and s.pause_started_at:
            elapsed = max(0, int(s.pause_started_at - s.started_at - s.paused_seconds))
        remaining = max(0, int((s.current.duration if s.current else 0) - elapsed)) if s.current else 0
        return {
            "ok": True,
            "ready": self.ready,
            "playing": s.status == "playing",
            "paused": s.status == "paused",
            "status": s.status,
            "chat_id": chat_id,
            "current": cur,
            "queue": [t.to_dict() for t in s.queue],
            "queue_size": len(s.queue),
            "elapsed": elapsed,
            "remaining": remaining,
            "offset": s.current.offset if s.current else 0,
            "last_error": s.last_error,
        }

    def _make_gc(self, path: str):
        return self.factory.get_file_group_call(input_filename=path, play_on_repeat=False)

    def _bind_ended(self, s: Session) -> None:
        if s.ended_hooked or not self.loop:
            return
        gc = s.gc
        if not gc:
            return

        @gc.on_playout_ended
        def _on_end(group_call, filename):
            if self.loop and not self.loop.is_closed():
                self.loop.call_soon_threadsafe(lambda: self.loop.create_task(self._advance(s.chat_id)))

        s.ended_hooked = True

    async def _stop_backend(self, s: Session) -> bool:
        gc = s.gc
        if not gc:
            return False
        ok = False
        for name in ("leave_current_group_call", "stop"):
            fn = getattr(gc, name, None)
            if callable(fn):
                try:
                    await self._maybe(fn())
                    ok = True
                except Exception as e:
                    s.last_error = f"{type(e).__name__}: {e}"
        return ok

    async def _open_and_start(self, s: Session, chat_id: int, path: str) -> dict[str, Any]:
        s.gc = self._make_gc(path)
        s.ended_hooked = False
        self._bind_ended(s)
        await self._maybe(s.gc.start(chat_id, enable_action=False))
        s.status = "playing"
        s.paused = False
        s.started_at = time.time()
        s.pause_started_at = 0.0
        s.paused_seconds = 0.0
        s.last_update_at = time.time()
        return self._state(chat_id)

    async def meta(self, p) -> dict[str, Any]:
        s = self._sess(p.chat_id)
        if not s.current:
            s.current = Track(id=uuid.uuid4().hex, chat_id=p.chat_id, source_type=p.source_type, source_id=p.source_id, title=p.title, duration=int(p.duration or 0), created_at=time.time())
        s.current.source_type = p.source_type
        s.current.source_id = p.source_id
        s.current.title = p.title
        s.current.duration = int(p.duration or 0)
        s.last_update_at = time.time()
        return self._state(p.chat_id)

    async def start(self, p) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")
        async with self._lock:
            s = self._sess(p.chat_id)
            await self._stop_backend(s)
            await self._cleanup_temp(s)
            src = await self._prepare_source(p.source_type, p.source_id, s)
            duration = int(p.duration or 0) or 0
            track = Track(id=uuid.uuid4().hex, chat_id=p.chat_id, source_type=p.source_type, source_id=p.source_id, title=p.title, duration=duration, local_path=src, original_path=src, offset=max(0, int(p.offset or 0)), created_at=time.time())
            if track.offset > 0:
                src = self._trim(src, track.offset, s)
            s.current = track
            s.current.local_path = src
            s.status = "starting"
            return await self._open_and_start(s, p.chat_id, src)

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")
        async with self._lock:
            s = self._sess(chat_id)
            if not s.gc or s.status != "playing":
                return self._state(chat_id)
            fn = getattr(s.gc, "pause_playout", None)
            if not callable(fn):
                raise RuntimeError("pause_not_supported")
            await self._maybe(fn())
            s.status = "paused"
            s.paused = True
            s.pause_started_at = time.time()
            s.last_update_at = time.time()
            return self._state(chat_id)

    async def resume(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")
        async with self._lock:
            s = self._sess(chat_id)
            if not s.gc or s.status != "paused":
                return self._state(chat_id)
            fn = getattr(s.gc, "resume_playout", None)
            if not callable(fn):
                raise RuntimeError("resume_not_supported")
            await self._maybe(fn())
            if s.pause_started_at:
                s.paused_seconds += max(0.0, time.time() - s.pause_started_at)
            s.pause_started_at = 0.0
            s.status = "playing"
            s.paused = False
            s.last_update_at = time.time()
            return self._state(chat_id)

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")
        async with self._lock:
            s = self._sess(chat_id)
            ok = await self._stop_backend(s)
            if s.gc:
                try:
                    await self._cleanup_temp(s)
                except Exception:
                    pass
            s.gc = None
            s.current = None
            s.queue.clear()
            s.status = "idle"
            s.paused = False
            s.started_at = 0.0
            s.pause_started_at = 0.0
            s.paused_seconds = 0.0
            s.last_update_at = time.time()
            if not ok:
                # avoid unknown in worker.js; return a real error string
                return {"ok": False, "error": f"backend_stop_failed: {s.last_error or 'method_not_supported'}", **self._state(chat_id)}
            return {"ok": True, "action": "stop", **self._state(chat_id)}

    async def seek(self, chat_id: int, delta: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")
        async with self._lock:
            s = self._sess(chat_id)
            if not s.current or not s.current.local_path:
                raise RuntimeError("no_active_track")
            new_offset = max(0, int((s.current.offset or 0) + int(delta)))
            base = s.current.original_path or s.current.local_path
            await self._stop_backend(s)
            try:
                await self._cleanup_temp(s)
            except Exception:
                pass
            new_path = self._trim(base, new_offset, s)
            s.current.offset = new_offset
            s.current.local_path = new_path
            s.gc = self._make_gc(new_path)
            s.ended_hooked = False
            self._bind_ended(s)
            await self._maybe(s.gc.start(chat_id, enable_action=False))
            s.status = "playing"
            s.paused = False
            s.started_at = time.time()
            s.pause_started_at = 0.0
            s.paused_seconds = 0.0
            return self._state(chat_id)

    async def enqueue(self, p) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")
        async with self._lock:
            s = self._sess(p.chat_id)
            src = await self._prepare_source(p.source_type, p.source_id, s)
            t = Track(id=uuid.uuid4().hex, chat_id=p.chat_id, source_type=p.source_type, source_id=p.source_id, title=p.title, duration=int(p.duration or 0), local_path=src, original_path=src, created_at=time.time())
            s.queue.append(t)
            if p.auto_start and s.status not in {"playing", "paused"}:
                nxt = s.queue.pop(0)
                s.current = nxt
                return await self._open_and_start(s, p.chat_id, nxt.local_path)
            return self._state(p.chat_id)

    async def queue_list(self, chat_id: int) -> dict[str, Any]:
        async with self._lock:
            return self._state(chat_id)

    async def queue_clear(self, chat_id: int) -> dict[str, Any]:
        async with self._lock:
            s = self._sess(chat_id)
            s.queue.clear()
            return self._state(chat_id)

    async def skip(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")
        async with self._lock:
            s = self._sess(chat_id)
            await self._stop_backend(s)
            if s.queue:
                nxt = s.queue.pop(0)
                s.current = nxt
                await self._cleanup_temp(s)
                s.gc = self._make_gc(nxt.local_path)
                s.ended_hooked = False
                self._bind_ended(s)
                await self._maybe(s.gc.start(chat_id, enable_action=False))
                s.status = "playing"
                s.paused = False
                s.started_at = time.time()
                s.pause_started_at = 0.0
                s.paused_seconds = 0.0
                return self._state(chat_id)
            s.gc = None
            s.current = None
            s.status = "idle"
            s.paused = False
            return self._state(chat_id)

    async def _advance(self, chat_id: int) -> None:
        async with self._lock:
            s = self._sess(chat_id)
            if s.queue:
                nxt = s.queue.pop(0)
                await self._stop_backend(s)
                await self._cleanup_temp(s)
                s.current = nxt
                s.gc = self._make_gc(nxt.local_path)
                s.ended_hooked = False
                self._bind_ended(s)
                await self._maybe(s.gc.start(chat_id, enable_action=False))
                s.status = "playing"
                s.paused = False
                s.started_at = time.time()
                s.pause_started_at = 0.0
                s.paused_seconds = 0.0
            else:
                s.gc = None
                s.current = None
                s.status = "idle"
                s.paused = False

    def state(self, chat_id: int) -> dict[str, Any]:
        return self._state(chat_id)

    async def status(self, chat_id: int) -> dict[str, Any]:
        return self._state(chat_id)


service = AudioService()