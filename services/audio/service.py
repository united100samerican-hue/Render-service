from __future__ import annotations

import asyncio
import inspect
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field
from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    from pytgcalls import PyTgCalls  # preferred import path
    PYTGCALLS_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    try:
        from py_tgcalls import PyTgCalls  # fallback in some environments
        PYTGCALLS_IMPORT_ERROR = ""
    except Exception:
        PyTgCalls = None
        PYTGCALLS_IMPORT_ERROR = str(exc)

logger = logging.getLogger("audio_service")

SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

USER_AGENT = "RenderAudioService/2.0"
TMP_ROOT = Path(os.getenv("AUDIO_TMP_ROOT", "/tmp/audio-service")).resolve()
TMP_ROOT.mkdir(parents=True, exist_ok=True)


class MetaRequest(BaseModel):
    chat_id: int = Field(..., description="Telegram chat id")
    source_type: str = Field(default="url")
    source_id: str = Field(default="")
    title: str = Field(default="")
    duration: int = Field(default=0)


class StartRequest(BaseModel):
    chat_id: int
    source_type: str = Field(default="url")
    source_id: str = Field(default="")
    title: str = Field(default="")
    duration: int = Field(default=0)
    offset: int = Field(default=0)


class ControlRequest(BaseModel):
    chat_id: int


class SeekRequest(BaseModel):
    chat_id: int
    delta: int = 0


class QueueAddRequest(BaseModel):
    chat_id: int
    source_type: str = Field(default="url")
    source_id: str = Field(default="")
    title: str = Field(default="")
    duration: int = Field(default=0)
    requested_by: str = Field(default="")
    auto_start: bool = Field(default=True)


class QueueListRequest(BaseModel):
    chat_id: int


class QueueClearRequest(BaseModel):
    chat_id: int


@dataclass
class QueueItem:
    id: str
    chat_id: int
    source_type: str = "url"
    source_id: str = ""
    title: str = ""
    duration: int = 0
    requested_by: str = ""
    created_at: float = 0.0
    local_path: str = ""
    offset: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AudioSession:
    chat_id: int
    status: str = "idle"
    paused: bool = False
    started_at: float = 0.0
    pause_started_at: float = 0.0
    paused_seconds: float = 0.0
    last_error: str = ""
    current: Optional[QueueItem] = None
    current_play_path: str = ""
    current_expected_duration: int = 0
    current_offset: int = 0
    last_update_at: float = 0.0
    auto_task: Optional[asyncio.Task] = field(default=None, repr=False, compare=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["current"] = self.current.to_dict() if self.current else None
        data.pop("auto_task", None)
        return data


class AudioService:
    def __init__(self) -> None:
        self.ready: bool = False
        self.backend_error: str = ""
        self.client: Optional[TelegramClient] = None
        self.calls: Any = None
        self._lock = asyncio.Lock()
        self._sessions: dict[int, AudioSession] = {}
        self._queues: dict[int, list[QueueItem]] = {}

    def active_sessions_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status in {"playing", "paused"})

    def queues_count(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def _env_ok(self) -> tuple[bool, str]:
        missing: list[str] = []
        if not SESSION_STRING:
            missing.append("SESSION_STRING")
        if not API_ID:
            missing.append("API_ID")
        if not API_HASH:
            missing.append("API_HASH")
        if missing:
            return False, "missing_env: " + ", ".join(missing)
        return True, ""

    async def _maybe_await(self, value: Any) -> Any:
        if inspect.isawaitable(value):
            return await value
        return value

    async def _call_variants(self, obj: Any, candidates: list[tuple[str, tuple[Any, ...], dict[str, Any]]]) -> Any:
        if obj is None:
            raise RuntimeError("backend_not_ready")
        last_exc: Exception | None = None
        for method_name, args, kwargs in candidates:
            fn = getattr(obj, method_name, None)
            if not callable(fn):
                continue
            try:
                return await self._maybe_await(fn(*args, **kwargs))
            except TypeError as exc:
                last_exc = exc
                continue
            except Exception as exc:
                last_exc = exc
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError("method_not_supported")

    async def _backend_play(self, chat_id: int, media_path: str) -> Any:
        candidates = [
            ("play", (chat_id, media_path), {}),
            ("play", (), {"chat_id": chat_id, "source": media_path}),
            ("play", (), {"chat_id": chat_id, "file": media_path}),
            ("play", (), {"chat_id": chat_id, "media": media_path}),
            ("start", (chat_id, media_path), {}),
            ("start", (), {"chat_id": chat_id, "source": media_path}),
            ("start", (), {"chat_id": chat_id, "file": media_path}),
        ]
        return await self._call_variants(self.calls, candidates)

    async def _backend_pause(self, chat_id: int) -> Any:
        return await self._call_variants(
            self.calls,
            [
                ("pause", (chat_id,), {}),
                ("pause", (), {"chat_id": chat_id}),
                ("pause_playout", (chat_id,), {}),
                ("pause_playout", (), {"chat_id": chat_id}),
            ],
        )

    async def _backend_resume(self, chat_id: int) -> Any:
        return await self._call_variants(
            self.calls,
            [
                ("resume", (chat_id,), {}),
                ("resume", (), {"chat_id": chat_id}),
                ("resume_playout", (chat_id,), {}),
                ("resume_playout", (), {"chat_id": chat_id}),
            ],
        )

    async def _backend_stop(self, chat_id: int) -> Any:
        return await self._call_variants(
            self.calls,
            [
                ("stop", (), {}),
                ("stop", (chat_id,), {}),
                ("stop", (), {"chat_id": chat_id}),
                ("leave_current_group_call", (), {}),
                ("leave_current_group_call", (chat_id,), {}),
                ("leave_group_call", (), {}),
                ("leave_group_call", (chat_id,), {}),
                ("leave", (), {}),
                ("leave", (chat_id,), {}),
                ("stop_stream", (chat_id,), {}),
            ],
        )

    def _session(self, chat_id: int) -> AudioSession:
        s = self._sessions.get(chat_id)
        if not s:
            s = AudioSession(chat_id=chat_id)
            self._sessions[chat_id] = s
        return s

    def _queue(self, chat_id: int) -> list[QueueItem]:
        return self._queues.setdefault(chat_id, [])

    def _probe_duration(self, path: str) -> int:
        try:
            cp = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    path,
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            return max(0, int(float((cp.stdout or "0").strip() or 0)))
        except Exception:
            return 0

    def _make_temp_path(self, suffix: str) -> Path:
        fd, raw = tempfile.mkstemp(prefix="audio_", suffix=suffix, dir=str(TMP_ROOT))
        os.close(fd)
        return Path(raw)

    def _normalize_source(self, source_type: str, source_id: str) -> str:
        src = str(source_id or "").strip()
        if not src:
            raise ValueError("source_id_required")

        if src.startswith(("http://", "https://")):
            return src

        st = str(source_type or "").lower().strip()
        if st in {"file", "path", "local"}:
            p = Path(src).expanduser()
            if not p.exists():
                raise ValueError(f"file_not_found: {src}")
            return str(p.resolve())

        p = Path(src).expanduser()
        if p.exists():
            return str(p.resolve())

        return src

    async def _stream_download_url(self, url: str) -> str:
        ext = Path(url.split("?", 1)[0]).suffix or ".bin"
        out = self._make_temp_path(ext)
        async with httpx.AsyncClient(timeout=120, headers={"User-Agent": USER_AGENT}, follow_redirects=True) as client:
            async with client.stream("GET", url) as r:
                r.raise_for_status()
                with open(out, "wb") as f:
                    async for chunk in r.aiter_bytes(1024 * 256):
                        if chunk:
                            f.write(chunk)
        return str(out)

    async def _download_via_bot_api(self, file_id: str) -> str:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN missing")

        async with httpx.AsyncClient(timeout=120, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            j = r.json()
            if not j.get("ok"):
                raise RuntimeError(j.get("description") or "getFile_failed")
            file_path = j["result"]["file_path"]
            ext = Path(file_path).suffix or ".bin"
            out = self._make_temp_path(ext)
            async with client.stream("GET", f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}") as dl:
                dl.raise_for_status()
                with open(out, "wb") as f:
                    async for chunk in dl.aiter_bytes(1024 * 256):
                        if chunk:
                            f.write(chunk)
        return str(out)

    async def _materialize_source(self, source_type: str, source_id: str) -> str:
        src = self._normalize_source(source_type, source_id)

        if src.startswith(("http://", "https://")):
            return await self._stream_download_url(src)

        p = Path(src)
        if p.exists():
            return str(p.resolve())

        st = str(source_type or "").lower().strip()
        if st in {"telegram", "tg", "telegram_file", "file_id"} or len(src) >= 20:
            return await self._download_via_bot_api(src)

        return src

    def _trim_media(self, source_path: str, offset_seconds: int) -> str:
        if offset_seconds <= 0:
            return source_path
        out = self._make_temp_path(".ogg")
        cmd = [
            "ffmpeg",
            "-y",
            "-ss",
            str(max(0, int(offset_seconds))),
            "-i",
            source_path,
            "-vn",
            "-c:a",
            "libopus",
            "-b:a",
            "128k",
            str(out),
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return str(out)

    def _cancel_auto(self, chat_id: int) -> None:
        s = self._sessions.get(chat_id)
        if not s or not s.auto_task:
            return
        if not s.auto_task.done():
            s.auto_task.cancel()
        s.auto_task = None

    def _remaining_seconds(self, s: AudioSession) -> int:
        if not s.current_expected_duration:
            return 0
        elapsed = max(0.0, time.time() - s.started_at - s.paused_seconds)
        return max(0, int(s.current_expected_duration - elapsed))

    async def _auto_advance(self, chat_id: int, seconds: int, current_id: str) -> None:
        try:
            await asyncio.sleep(max(1, seconds))
            async with self._lock:
                s = self._sessions.get(chat_id)
                if not s or s.status != "playing" or not s.current or s.current.id != current_id:
                    return
            await self.skip(chat_id, _internal=True)
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("auto_advance_failed", extra={"chat_id": chat_id})

    def _schedule_auto_advance(self, chat_id: int) -> None:
        s = self._sessions.get(chat_id)
        if not s or not s.current or s.current_expected_duration <= 0:
            return
        self._cancel_auto(chat_id)
        remaining = self._remaining_seconds(s)
        if remaining <= 0:
            return
        s.auto_task = asyncio.create_task(self._auto_advance(chat_id, remaining, s.current.id))

    def _state(self, chat_id: int) -> dict[str, Any]:
        s = self._sessions.get(chat_id)
        q = [item.to_dict() for item in self._queue(chat_id)]
        if not s:
            return {
                "ok": True,
                "ready": self.ready,
                "playing": False,
                "paused": False,
                "chat_id": chat_id,
                "current": None,
                "queue": q,
                "queue_size": len(q),
                "last_error": "",
                "elapsed": 0,
                "remaining": 0,
            }
        elapsed = 0
        if s.status == "playing":
            elapsed = max(0, int(time.time() - s.started_at - s.paused_seconds))
        elif s.status == "paused" and s.pause_started_at:
            elapsed = max(0, int(s.pause_started_at - s.started_at - s.paused_seconds))
        remaining = max(0, int(s.current_expected_duration - elapsed)) if s.current_expected_duration else 0
        return {
            "ok": True,
            "ready": self.ready,
            "playing": s.status == "playing",
            "paused": s.status == "paused",
            "chat_id": s.chat_id,
            "current": s.current.to_dict() if s.current else None,
            "queue": q,
            "queue_size": len(q),
            "last_error": s.last_error,
            "elapsed": elapsed,
            "remaining": remaining,
            "status": s.status,
            "offset": s.current_offset,
        }

    async def ensure_ready(self) -> None:
        async with self._lock:
            if self.ready:
                return

            ok, reason = self._env_ok()
            if not ok:
                self.backend_error = reason
                self.ready = False
                logger.error("audio env missing: %s", reason)
                return

            if PyTgCalls is None:
                self.backend_error = f"pytgcalls_import_error: {PYTGCALLS_IMPORT_ERROR}"
                self.ready = False
                logger.error("audio backend import failed: %s", self.backend_error)
                return

            try:
                self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
                await self._maybe_await(self.client.start())

                self.calls = PyTgCalls(self.client)
                await self._maybe_await(self.calls.start())

                self.ready = True
                self.backend_error = ""
                logger.info("audio service ready")
            except Exception as exc:
                self.ready = False
                self.backend_error = str(exc)
                logger.exception("audio init failed")
                try:
                    if self.client:
                        await self._maybe_await(self.client.disconnect())
                except Exception:
                    pass
                self.client = None
                self.calls = None

    async def meta(self, payload: MetaRequest) -> dict[str, Any]:
        async with self._lock:
            s = self._session(payload.chat_id)
            if not s.current:
                s.current = QueueItem(
                    id=uuid.uuid4().hex,
                    chat_id=payload.chat_id,
                    source_type=payload.source_type,
                    source_id=payload.source_id,
                    title=payload.title,
                    duration=int(payload.duration or 0),
                    created_at=time.time(),
                )
            s.current.source_type = payload.source_type
            s.current.source_id = payload.source_id
            s.current.title = payload.title
            s.current.duration = int(payload.duration or 0)
            s.last_update_at = time.time()
            return self._state(payload.chat_id)

    async def _start_item_locked(self, chat_id: int, item: QueueItem, offset: int = 0, replace_current: bool = True) -> dict[str, Any]:
        self._cancel_auto(chat_id)
        session = self._session(chat_id)

        if session.status in {"playing", "paused"}:
            try:
                await self._backend_stop(chat_id)
            except Exception:
                pass

        source = item.local_path or await self._materialize_source(item.source_type, item.source_id)
        item.local_path = source
        item.offset = max(0, int(offset))
        play_path = self._trim_media(source, item.offset) if item.offset > 0 else source
        expected_duration = max(0, item.duration - item.offset) if item.duration else 0

        session.current = item if replace_current else item
        session.current_play_path = play_path
        session.current_expected_duration = expected_duration
        session.current_offset = item.offset
        session.started_at = time.time()
        session.pause_started_at = 0.0
        session.paused_seconds = 0.0
        session.status = "playing"
        session.paused = False
        session.last_error = ""
        session.last_update_at = time.time()

        try:
            await self._backend_play(chat_id, play_path)
        except Exception as exc:
            session.status = "error"
            session.last_error = f"{type(exc).__name__}: {exc}"
            raise

        self._schedule_auto_advance(chat_id)
        return self._state(chat_id)

    async def start(self, payload: StartRequest) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            source = await self._materialize_source(payload.source_type, payload.source_id)
            item_duration = int(payload.duration or 0) or self._probe_duration(source)
            item = QueueItem(
                id=uuid.uuid4().hex,
                chat_id=payload.chat_id,
                source_type=payload.source_type,
                source_id=source,
                title=payload.title,
                duration=item_duration,
                created_at=time.time(),
                local_path=source,
                offset=max(0, int(payload.offset or 0)),
            )
            return await self._start_item_locked(payload.chat_id, item, offset=item.offset)

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            s = self._session(chat_id)
            if s.status != "playing":
                return self._state(chat_id)
            await self._backend_pause(chat_id)
            self._cancel_auto(chat_id)
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
            s = self._session(chat_id)
            if s.status != "paused":
                return self._state(chat_id)
            await self._backend_resume(chat_id)
            if s.pause_started_at:
                s.paused_seconds += max(0.0, time.time() - s.pause_started_at)
                s.pause_started_at = 0.0
            s.status = "playing"
            s.paused = False
            s.last_update_at = time.time()
            self._schedule_auto_advance(chat_id)
            return self._state(chat_id)

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            self._cancel_auto(chat_id)
            try:
                await self._backend_stop(chat_id)
            except Exception as exc:
                s = self._session(chat_id)
                s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions.pop(chat_id, None)
            return {
                "ok": True,
                "ready": self.ready,
                "playing": False,
                "paused": False,
                "chat_id": chat_id,
                "current": None,
                "queue": [i.to_dict() for i in self._queue(chat_id)],
                "queue_size": len(self._queue(chat_id)),
                "last_error": "",
                "elapsed": 0,
                "remaining": 0,
                "status": "stopped",
                "offset": 0,
            }

    async def seek(self, chat_id: int, delta: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            s = self._session(chat_id)
            if not s.current:
                raise RuntimeError("no_active_track")
            base = s.current.local_path or s.current.source_id
            if not Path(base).exists():
                raise RuntimeError("seek_requires_local_source")
            new_offset = max(0, int(s.current_offset + int(delta)))
            s.current_offset = new_offset
            self._cancel_auto(chat_id)
            try:
                await self._backend_stop(chat_id)
            except Exception:
                pass
            s.started_at = time.time()
            s.pause_started_at = 0.0
            s.paused_seconds = 0.0
            s.status = "playing"
            s.paused = False
            s.current_play_path = self._trim_media(base, new_offset) if new_offset > 0 else base
            if s.current.duration:
                s.current_expected_duration = max(0, s.current.duration - new_offset)
            try:
                await self._backend_play(chat_id, s.current_play_path)
            except Exception as exc:
                s.status = "error"
                s.last_error = f"{type(exc).__name__}: {exc}"
                raise
            self._schedule_auto_advance(chat_id)
            return self._state(chat_id)

    async def enqueue(self, payload: QueueAddRequest) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            source = await self._materialize_source(payload.source_type, payload.source_id)
            duration = int(payload.duration or 0) or self._probe_duration(source)
            item = QueueItem(
                id=uuid.uuid4().hex,
                chat_id=payload.chat_id,
                source_type=payload.source_type,
                source_id=source,
                title=payload.title,
                duration=duration,
                requested_by=payload.requested_by,
                created_at=time.time(),
                local_path=source,
            )
            self._queue(payload.chat_id).append(item)
            s = self._session(payload.chat_id)
            if payload.auto_start and s.status not in {"playing", "paused"}:
                next_item = self._queue(payload.chat_id).pop(0)
                return await self._start_item_locked(payload.chat_id, next_item, offset=0)
            return self._state(payload.chat_id)

    async def queue_list(self, chat_id: int) -> dict[str, Any]:
        async with self._lock:
            s = self._session(chat_id)
            return {
                "ok": True,
                "ready": self.ready,
                "chat_id": chat_id,
                "current": s.current.to_dict() if s.current else None,
                "queue": [i.to_dict() for i in self._queue(chat_id)],
                "queue_size": len(self._queue(chat_id)),
            }

    async def queue_clear(self, chat_id: int) -> dict[str, Any]:
        async with self._lock:
            self._queues[chat_id] = []
            s = self._session(chat_id)
            return {
                "ok": True,
                "ready": self.ready,
                "chat_id": chat_id,
                "current": s.current.to_dict() if s.current else None,
                "queue": [],
                "queue_size": 0,
            }

    async def skip(self, chat_id: int, _internal: bool = False) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            self._cancel_auto(chat_id)
            try:
                await self._backend_stop(chat_id)
            except Exception:
                pass
            q = self._queue(chat_id)
            if q:
                next_item = q.pop(0)
                return await self._start_item_locked(chat_id, next_item, offset=0)
            self._sessions.pop(chat_id, None)
            return {
                "ok": True,
                "ready": self.ready,
                "playing": False,
                "paused": False,
                "chat_id": chat_id,
                "current": None,
                "queue": [],
                "queue_size": 0,
                "last_error": "",
                "elapsed": 0,
                "remaining": 0,
                "status": "stopped",
                "offset": 0,
            }

    def state(self, chat_id: int) -> dict[str, Any]:
        return self._state(chat_id)