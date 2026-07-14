from __future__ import annotations

import asyncio
import inspect
import logging
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field
from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    from pytgcalls import PyTgCalls
    PYTGCALLS_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover
    PyTgCalls = None
    PYTGCALLS_IMPORT_ERROR = str(exc)

logger = logging.getLogger("audio_service")

SESSION_STRING = os.getenv("SESSION_STRING", "").strip()
API_ID = int(os.getenv("API_ID", "0") or 0)
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()


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


class ControlRequest(BaseModel):
    chat_id: int


class SeekRequest(BaseModel):
    chat_id: int
    delta: int = 0


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AudioService:
    def __init__(self) -> None:
        self.ready: bool = False
        self.backend_error: str = ""
        self.client: Optional[TelegramClient] = None
        self.calls: Any = None
        self._lock = asyncio.Lock()
        self._sessions: dict[int, AudioSession] = {}

    def active_sessions_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status in {"playing", "paused"})

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

    async def _call_method(self, names: list[str], *args: Any, **kwargs: Any) -> Any:
        if not self.calls:
            raise RuntimeError("calls_not_ready")
        last_exc: Exception | None = None
        for name in names:
            fn = getattr(self.calls, name, None)
            if not callable(fn):
                continue
            try:
                return await self._maybe_await(fn(*args, **kwargs))
            except TypeError as exc:
                last_exc = exc
                continue
        if last_exc:
            raise last_exc
        raise RuntimeError(f"method_not_supported: {','.join(names)}")

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

    async def ensure_ready(self) -> None:
        async with self._lock:
            if self.ready:
                return

            env_ok, reason = self._env_ok()
            if not env_ok:
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

    def _state(self, chat_id: int) -> dict[str, Any]:
        s = self._sessions.get(chat_id)
        if not s:
            return {
                "ok": True,
                "ready": self.ready,
                "playing": False,
                "paused": False,
                "chat_id": chat_id,
                "source_type": "",
                "source_id": "",
                "title": "",
                "duration": 0,
                "started_at": 0,
                "updated_at": 0,
                "last_error": "",
            }
        return {
            "ok": True,
            "ready": self.ready,
            "playing": s.status == "playing",
            "paused": s.status == "paused",
            "chat_id": s.chat_id,
            "source_type": s.source_type,
            "source_id": s.source_id,
            "title": s.title,
            "duration": s.duration,
            "started_at": s.started_at,
            "updated_at": s.updated_at,
            "last_error": s.last_error,
        }

    async def meta(self, payload: MetaRequest) -> dict[str, Any]:
        async with self._lock:
            s = self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id)
            s.source_type = payload.source_type
            s.source_id = payload.source_id
            s.title = payload.title
            s.duration = int(payload.duration or 0)
            s.updated_at = time.time()
            self._sessions[payload.chat_id] = s
            return self._state(payload.chat_id)

    async def start(self, payload: StartRequest) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        source = self._normalize_source(payload.source_type, payload.source_id)

        async with self._lock:
            prev = self._sessions.get(payload.chat_id)
            if prev and prev.status == "playing" and prev.source_id == source:
                return self._state(payload.chat_id)

            if prev and prev.status in {"playing", "paused"}:
                await self._stop_locked(payload.chat_id, keep_state=False)

            try:
                await self._call_method(["play", "start"], int(payload.chat_id), source)
            except Exception as exc:
                s = self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id)
                s.status = "error"
                s.last_error = str(exc)
                s.updated_at = time.time()
                self._sessions[payload.chat_id] = s
                raise

            s = self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id)
            s.source_type = payload.source_type
            s.source_id = source
            s.title = payload.title
            s.duration = int(payload.duration or 0)
            s.status = "playing"
            s.paused = False
            s.started_at = s.started_at or time.time()
            s.updated_at = time.time()
            s.last_error = ""
            self._sessions[payload.chat_id] = s
            return self._state(payload.chat_id)

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            await self._call_method(["pause", "pause_playout"], int(chat_id))
            s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            s.status = "paused"
            s.paused = True
            s.updated_at = time.time()
            self._sessions[chat_id] = s
            return self._state(chat_id)

    async def resume(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            await self._call_method(["resume", "resume_playout"], int(chat_id))
            s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            s.status = "playing"
            s.paused = False
            s.updated_at = time.time()
            self._sessions[chat_id] = s
            return self._state(chat_id)

    async def _stop_locked(self, chat_id: int, keep_state: bool = False) -> dict[str, Any]:
        try:
            await self._call_method(["stop", "leave", "disconnect"], int(chat_id))
        except Exception as exc:
            s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            s.last_error = str(exc)
            s.status = "error"
            s.updated_at = time.time()
            self._sessions[chat_id] = s
            if not keep_state:
                raise
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        s.status = "stopped"
        s.paused = False
        s.updated_at = time.time()
        if keep_state:
            self._sessions[chat_id] = s
        else:
            self._sessions.pop(chat_id, None)
        return self._state(chat_id)

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            return await self._stop_locked(chat_id, keep_state=False)

    async def seek(self, chat_id: int, delta: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            raise RuntimeError(f"service_not_ready: {self.backend_error or 'missing_env'}")

        async with self._lock:
            fn = getattr(self.calls, "seek", None) if self.calls else None
            if not callable(fn):
                return {
                    "ok": False,
                    "error": "seek_not_supported_by_installed_backend",
                    **self._state(chat_id),
                }
            result = fn(int(chat_id), int(delta))
            await self._maybe_await(result)
            s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            s.updated_at = time.time()
            self._sessions[chat_id] = s
            return self._state(chat_id)
