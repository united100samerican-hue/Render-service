from __future__ import annotations

import asyncio
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

import httpx
from pydantic import BaseModel, Field

logger = logging.getLogger("audio_service")

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except Exception as exc:  # pragma: no cover
    TelegramClient = None  # type: ignore[assignment]
    StringSession = None  # type: ignore[assignment]
    TELETHON_IMPORT_ERROR = str(exc)
else:
    TELETHON_IMPORT_ERROR = ""

try:
    from pytgcalls import PyTgCalls
except Exception:
    try:
        from py_tgcalls import PyTgCalls  # type: ignore
    except Exception as exc:  # pragma: no cover
        PyTgCalls = None  # type: ignore[assignment]
        PYTGCALLS_IMPORT_ERROR = str(exc)
    else:
        PYTGCALLS_IMPORT_ERROR = ""
else:
    PYTGCALLS_IMPORT_ERROR = ""

SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
USER_AGENT = "RenderAudioService/4.0"

MISSING_ENV = [name for name, value in (
    ("SESSION_STRING", SESSION_STRING),
    ("API_ID", str(API_ID) if API_ID else ""),
    ("API_HASH", API_HASH),
) if not value]


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
class AudioSession:
    chat_id: int
    source_type: str = "url"
    source_id: str = ""
    title: str = ""
    duration: int = 0
    status: str = "idle"
    paused: bool = False
    started_at: float = 0.0
    updated_at: float = 0.0
    last_error: str = ""
    local_path: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QueueItem:
    chat_id: int
    source_type: str
    source_id: str
    title: str = ""
    duration: int = 0
    requested_by: str = ""
    auto_start: bool = True
    enqueued_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioService:
    def __init__(self) -> None:
        self.ready: bool = False
        self.backend_error: str = ""
        self.client: Optional[Any] = None
        self.calls: Optional[Any] = None
        self._lock = asyncio.Lock()
        self._sessions: dict[int, AudioSession] = {}
        self._queues: dict[int, deque[QueueItem]] = {}
        self._state_file: Path | None = None

    def active_sessions_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status in {"playing", "paused"})

    def queues_count(self) -> int:
        return sum(len(q) for q in self._queues.values())

    def _queue(self, chat_id: int) -> deque[QueueItem]:
        q = self._queues.get(chat_id)
        if q is None:
            q = deque()
            self._queues[chat_id] = q
        return q

    def state(self, chat_id: int) -> dict[str, Any]:
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        q = self._queues.get(chat_id) or deque()
        return {
            "ok": True,
            "chat_id": chat_id,
            "ready": self.ready,
            "backend_error": self.backend_error,
            "session": s.to_dict(),
            "queue_length": len(q),
            "queue_head": q[0].to_dict() if q else None,
        }

    def _env_ok(self) -> tuple[bool, str]:
        if MISSING_ENV:
            return False, "missing_env: " + ", ".join(MISSING_ENV)
        return True, ""

    async def _maybe_await(self, value: Any) -> Any:
        if asyncio.iscoroutine(value) or asyncio.isfuture(value):
            return await value
        return value

    async def _call_any(self, obj: Any, method_names: list[str], *args: Any, **kwargs: Any) -> bool:
        if not obj:
            return False
        for name in method_names:
            fn = getattr(obj, name, None)
            if not callable(fn):
                continue
            try:
                await self._maybe_await(fn(*args, **kwargs))
                return True
            except TypeError:
                # try the next variant
                continue
            except Exception as exc:
                logger.debug("backend call %s failed: %s", name, exc)
                continue
        return False

    async def _download_via_bot_api(self, file_id: str) -> str:
        if not BOT_TOKEN:
            raise RuntimeError("BOT_TOKEN missing")
        async with httpx.AsyncClient(timeout=120, headers={"User-Agent": USER_AGENT}) as client:
            r = await client.get(
                f"https://api.telegram.org/bot{BOT_TOKEN}/getFile",
                params={"file_id": file_id},
            )
            r.raise_for_status()
            j = r.json()
            if not j.get("ok"):
                raise RuntimeError(j.get("description") or "getFile_failed")
            file_path = j["result"]["file_path"]
            dl = await client.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}")
            dl.raise_for_status()
            suffix = Path(file_path).suffix or ".bin"
            fd, out_path = tempfile.mkstemp(prefix="audio_", suffix=suffix)
            os.close(fd)
            with open(out_path, "wb") as f:
                f.write(dl.content)
            return out_path

    def _normalize_source(self, source_type: str, source_id: str) -> tuple[str, str]:
        st = (source_type or "url").strip().lower()
        src = (source_id or "").strip()
        if not src:
            raise ValueError("source_id_required")
        if st in {"file", "path", "local"}:
            p = Path(src).expanduser()
            if not p.exists():
                raise ValueError(f"file_not_found: {src}")
            return "file", str(p.resolve())
        if st in {"file_id", "fileid", "telegram_file"}:
            return "file_id", src
        return "url", src

    async def _materialize_source(self, source_type: str, source_id: str) -> str:
        st, src = self._normalize_source(source_type, source_id)
        if st == "file":
            return src
        if st == "file_id":
            return await self._download_via_bot_api(src)
        return src

    async def _ensure_client(self) -> None:
        if self.ready:
            return
        ok, why = self._env_ok()
        if not ok:
            self.backend_error = why
            self.ready = False
            return
        if TelegramClient is None or PyTgCalls is None or StringSession is None:
            self.ready = False
            self.backend_error = f"import_error: telethon={TELETHON_IMPORT_ERROR or 'ok'}; pytgcalls={PYTGCALLS_IMPORT_ERROR or 'ok'}"
            return
        self.client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
        try:
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

    async def ensure_ready(self) -> None:
        await self._ensure_client()

    def _touch(self, s: AudioSession) -> AudioSession:
        s.updated_at = time.time()
        return s

    async def meta(self, payload: MetaRequest) -> dict[str, Any]:
        s = self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id)
        s.source_type = payload.source_type
        s.source_id = payload.source_id
        s.title = payload.title
        s.duration = payload.duration
        self._sessions[payload.chat_id] = self._touch(s)
        return {"ok": True, "action": "meta", "state": self.state(payload.chat_id)}

    async def start(self, payload: StartRequest) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id)
        s.source_type = payload.source_type
        s.source_id = payload.source_id
        s.title = payload.title
        s.duration = payload.duration
        s.status = "starting"
        s.paused = False
        s.started_at = s.started_at or time.time()
        s.last_error = ""
        self._sessions[payload.chat_id] = self._touch(s)

        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[payload.chat_id] = self._touch(s)
            return {"ok": False, "action": "start", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(payload.chat_id)}

        try:
            source = await self._materialize_source(payload.source_type, payload.source_id)
            s.local_path = source if Path(source).exists() else ""
            self._sessions[payload.chat_id] = self._touch(s)

            # Try common call/play variants without failing the whole request.
            played = False
            if self.calls:
                played = await self._call_any(
                    self.calls,
                    [
                        "play",
                        "start",
                        "join",
                        "join_group_call",
                        "create",
                    ],
                    payload.chat_id,
                    source,
                    title=payload.title,
                    duration=payload.duration,
                    offset=payload.offset,
                )
                if not played:
                    played = await self._call_any(
                        self.calls,
                        [
                            "play",
                            "start",
                            "join",
                            "join_group_call",
                        ],
                        payload.chat_id,
                        source,
                    )

            s.status = "playing" if played else "idle"
            s.paused = False
            s.last_error = "" if played else "backend_start_noop"
            self._sessions[payload.chat_id] = self._touch(s)
            return {"ok": True, "action": "start", "played": played, "state": self.state(payload.chat_id)}
        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[payload.chat_id] = self._touch(s)
            return {"ok": False, "action": "start", "error": type(exc).__name__, "detail": str(exc), "state": self.state(payload.chat_id)}

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "pause", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(chat_id)}

        try:
            paused = await self._call_any(self.calls, ["pause", "pause_stream"], chat_id) if self.calls else False
            s.status = "paused" if paused else s.status
            s.paused = True if paused else s.paused
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "pause", "paused": paused, "state": self.state(chat_id)}
        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "pause", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def resume(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "resume", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(chat_id)}

        try:
            resumed = await self._call_any(self.calls, ["resume", "resume_stream"], chat_id) if self.calls else False
            s.status = "playing" if resumed else s.status
            s.paused = False if resumed else s.paused
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "resume", "resumed": resumed, "state": self.state(chat_id)}
        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "resume", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def _stop_backend(self, chat_id: int) -> bool:
        # Try the widest set of known method names first.
        if await self._call_any(self.calls, ["stop", "leave_current_group_call", "leave_group_call", "leave", "stop_stream"], chat_id):
            return True

        # Last resort: disconnect the client so the session definitely ends.
        if self.client:
            try:
                await self._maybe_await(self.client.disconnect())
                return True
            except Exception as exc:
                logger.debug("client disconnect fallback failed: %s", exc)
        return False

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)

        # Even if the backend is not ready, we still return a clean response.
        if not self.ready:
            s.status = "stopped"
            s.paused = False
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions.pop(chat_id, None)
            return {"ok": False, "action": "stop", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(chat_id)}

        try:
            async with self._lock:
                backend_stopped = await self._stop_backend(chat_id)
                s.status = "stopped"
                s.paused = False
                s.last_error = "" if backend_stopped else "backend_stop_noop"
                self._sessions.pop(chat_id, None)
                self._queues.pop(chat_id, None)
                return {"ok": True, "action": "stop", "stopped": backend_stopped, "state": self.state(chat_id)}
        except Exception as exc:
            # Never let /stop explode into a 500.
            s.status = "stopped"
            s.paused = False
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions.pop(chat_id, None)
            self._queues.pop(chat_id, None)
            return {"ok": False, "action": "stop", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def seek(self, chat_id: int, delta: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            return {"ok": False, "action": "seek", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(chat_id)}
        try:
            if not self.calls:
                return {"ok": False, "action": "seek", "error": "calls_not_ready", "state": self.state(chat_id)}
            fn = getattr(self.calls, "seek", None)
            if not callable(fn):
                return {"ok": False, "action": "seek", "error": "method_not_supported", "state": self.state(chat_id)}
            await self._maybe_await(fn(chat_id, delta))
            return {"ok": True, "action": "seek", "state": self.state(chat_id)}
        except Exception as exc:
            return {"ok": False, "action": "seek", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def enqueue(self, payload: QueueAddRequest) -> dict[str, Any]:
        item = QueueItem(
            chat_id=payload.chat_id,
            source_type=payload.source_type,
            source_id=payload.source_id,
            title=payload.title,
            duration=payload.duration,
            requested_by=payload.requested_by,
            auto_start=payload.auto_start,
        )
        q = self._queue(payload.chat_id)
        q.append(item)
        self._sessions[payload.chat_id] = self._touch(self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id))
        if payload.auto_start and self.state(payload.chat_id)["session"]["status"] in {"idle", "stopped"}:
            start_res = await self.start(StartRequest(
                chat_id=payload.chat_id,
                source_type=payload.source_type,
                source_id=payload.source_id,
                title=payload.title,
                duration=payload.duration,
                offset=0,
            ))
            return {"ok": True, "action": "queue_add", "auto_started": True, "start_result": start_res, "state": self.state(payload.chat_id)}
        return {"ok": True, "action": "queue_add", "queued": True, "queue_length": len(q), "state": self.state(payload.chat_id)}

    async def queue_list(self, chat_id: int) -> dict[str, Any]:
        q = self._queue(chat_id)
        return {"ok": True, "action": "queue_list", "queue": [item.to_dict() for item in q], "state": self.state(chat_id)}

    async def queue_clear(self, chat_id: int) -> dict[str, Any]:
        self._queues.pop(chat_id, None)
        return {"ok": True, "action": "queue_clear", "state": self.state(chat_id)}

    async def skip(self, chat_id: int) -> dict[str, Any]:
        q = self._queue(chat_id)
        if q:
            q.popleft()
        if not q:
            return await self.stop(chat_id)
        next_item = q[0]
        return await self.start(StartRequest(
            chat_id=chat_id,
            source_type=next_item.source_type,
            source_id=next_item.source_id,
            title=next_item.title,
            duration=next_item.duration,
            offset=0,
        ))