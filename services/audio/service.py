from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from collections import deque
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Deque, Optional

import httpx
from pydantic import BaseModel, Field
from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    from pytgcalls import PyTgCalls
except Exception:  # pragma: no cover
    PyTgCalls = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_service")

ALLOWED_SOURCE_TYPES = {
    "file_id",
    "telegram_file_id",
    "telegram",
    "telegram_audio",
    "telegram_video",
}

MEDIA_EXT_AUDIO = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".aac", ".flac", ".opus"}
MEDIA_EXT_VIDEO = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}


class StartRequest(BaseModel):
    chat_id: int = Field(..., alias="chatId")
    source_type: str = Field("telegram_file_id", alias="sourceType")
    source_id: str = Field(..., alias="sourceId")
    title: str = Field("", alias="title")
    duration: int = Field(0, alias="duration")
    offset: int = Field(0, alias="offset")


class MetaRequest(BaseModel):
    chat_id: int = Field(..., alias="chatId")
    source_type: str = Field("telegram_file_id", alias="sourceType")
    source_id: str = Field(..., alias="sourceId")
    title: str = Field("", alias="title")
    duration: int = Field(0, alias="duration")


class ControlRequest(BaseModel):
    chat_id: int = Field(..., alias="chatId")


class SeekRequest(BaseModel):
    chat_id: int = Field(..., alias="chatId")
    delta: int = Field(0, alias="delta")


class QueueAddRequest(BaseModel):
    chat_id: int = Field(..., alias="chatId")
    source_type: str = Field("telegram_file_id", alias="sourceType")
    source_id: str = Field(..., alias="sourceId")
    title: str = Field("", alias="title")
    duration: int = Field(0, alias="duration")
    requested_by: str = Field("", alias="requestedBy")
    auto_start: bool = Field(True, alias="autoStart")


class QueueListRequest(BaseModel):
    chat_id: int = Field(..., alias="chatId")


class QueueClearRequest(BaseModel):
    chat_id: int = Field(..., alias="chatId")


@dataclass
class AudioSession:
    chat_id: int
    status: str = "idle"  # idle | playing | paused | stopped | error
    title: str = ""
    source_type: str = ""
    source_id: str = ""
    duration: int = 0
    offset: int = 0
    paused: bool = False
    last_error: str = ""
    updated_at: float = field(default_factory=lambda: asyncio.get_event_loop().time() if asyncio.get_event_loop().is_running() else 0.0)
    local_path: str = ""
    video: bool = False


@dataclass
class QueueItem:
    chat_id: int
    source_type: str
    source_id: str
    title: str = ""
    duration: int = 0
    requested_by: str = ""
    auto_start: bool = True
    video: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _now() -> float:
    loop = asyncio.get_event_loop()
    try:
        return loop.time()
    except Exception:
        import time
        return time.time()


class AudioService:
    def __init__(self) -> None:
        self.api_id = int(os.getenv("API_ID", "0") or "0")
        self.api_hash = os.getenv("API_HASH", "").strip()
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        self.session_string = os.getenv("SESSION_STRING", "").strip()
        self.backend_error = ""
        self.ready = False

        self._lock = asyncio.Lock()
        self._sessions: dict[int, AudioSession] = {}
        self._queues: dict[int, Deque[QueueItem]] = {}
        self._download_dir = Path(tempfile.gettempdir()) / "render_audio_service_media"
        self._download_dir.mkdir(parents=True, exist_ok=True)
        self._client: TelegramClient | None = None
        self.calls: Any = None

    # ---------- lifecycle ----------
    async def ensure_ready(self) -> None:
        if self.ready:
            return

        if not self.api_id or not self.api_hash or not self.session_string:
            self.backend_error = "missing_env: API_ID/API_HASH/SESSION_STRING"
            self.ready = False
            return

        if self._client is None:
            self._client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

        try:
            if not self._client.is_connected():
                await self._client.connect()

            if self.calls is None:
                if PyTgCalls is None:
                    raise RuntimeError("pytgcalls_import_failed")
                self.calls = PyTgCalls(self._client)

            # Different versions expose start either sync or async.
            await self._maybe_await(self.calls.start())
            self.ready = True
            self.backend_error = ""
            logger.info("audio service ready")
        except Exception as exc:
            self.ready = False
            self.backend_error = f"{type(exc).__name__}: {exc}"
            logger.exception("audio service init failed")

    async def close(self) -> None:
        try:
            await self._cleanup_all_temp_files()
        finally:
            try:
                if self.calls is not None:
                    stop = getattr(self.calls, "stop", None)
                    if callable(stop):
                        try:
                            await self._maybe_await(stop())
                        except Exception:
                            pass
            finally:
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass

    # ---------- helpers ----------
    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if asyncio.iscoroutine(value):
            return await value
        return value

    def _touch(self, s: AudioSession) -> AudioSession:
        s.updated_at = _now()
        return s

    def _queue(self, chat_id: int) -> Deque[QueueItem]:
        q = self._queues.get(chat_id)
        if q is None:
            q = deque()
            self._queues[chat_id] = q
        return q

    async def _cleanup_file(self, path: str) -> None:
        if not path:
            return
        p = Path(path)
        try:
            if p.exists():
                p.unlink()
        except Exception:
            logger.debug("failed to cleanup temp file: %s", path)

    async def _cleanup_all_temp_files(self) -> None:
        for s in list(self._sessions.values()):
            await self._cleanup_file(s.local_path)
        for q in self._queues.values():
            for item in q:
                pass

    def _allowed_source(self, source_type: str) -> tuple[bool, bool]:
        st = (source_type or "").strip().lower()
        if st not in ALLOWED_SOURCE_TYPES:
            return False, False
        return True, st == "telegram_video"

    def _infer_video(self, source_type: str, file_name: str) -> bool:
        st = (source_type or "").strip().lower()
        if st == "telegram_video":
            return True
        if st == "telegram_audio":
            return False
        suffix = Path(file_name).suffix.lower()
        if suffix in MEDIA_EXT_VIDEO:
            return True
        if suffix in MEDIA_EXT_AUDIO:
            return False
        return False

    async def _resolve_telegram_file(self, file_id: str, *, source_type: str, title: str = "") -> tuple[Path, bool, str]:
        """
        Download a Telegram file_id through the Bot API and return:
        local_path, video_flag, original_filename
        """
        if not self.bot_token:
            raise RuntimeError("missing_env: BOT_TOKEN")

        async with httpx.AsyncClient(timeout=120) as client:
            info = await client.get(
                f"https://api.telegram.org/bot{self.bot_token}/getFile",
                params={"file_id": file_id},
            )
            info.raise_for_status()
            data = info.json()
            if not data.get("ok"):
                raise RuntimeError(f"telegram_getFile_failed: {data}")

            file_path = data["result"]["file_path"]
            suffix = Path(file_path).suffix.lower()
            guessed_name = Path(file_path).name or (title.strip() or file_id)
            is_video = self._infer_video(source_type, guessed_name)

            if suffix:
                ext = suffix
            else:
                ext = ".mp4" if is_video else ".ogg"

            local_name = f"{file_id.replace('/', '_')}{ext}"
            local_path = self._download_dir / local_name

            dl = await client.get(
                f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}",
            )
            dl.raise_for_status()
            local_path.write_bytes(dl.content)

            return local_path, is_video, guessed_name

    async def _call_any(self, obj: Any, methods: list[str], *args: Any, **kwargs: Any) -> bool:
        if obj is None:
            return False
        for name in methods:
            fn = getattr(obj, name, None)
            if not callable(fn):
                continue
            try:
                res = fn(*args, **kwargs)
                await self._maybe_await(res)
                return True
            except TypeError:
                continue
            except Exception as exc:
                logger.debug("method %s failed: %s", name, exc)
                continue
        return False

    async def _stop_backend(self, chat_id: int) -> bool:
        """
        Prefer explicit leave semantics to match the old working service.
        """
        if self.calls is None:
            return False

        # Explicit leave first.
        if await self._call_any(self.calls, [
            "leave_current_group_call",
            "leave_group_call",
            "leave",
        ], chat_id):
            return True

        # Variants without chat_id.
        if await self._call_any(self.calls, [
            "leave_current_group_call",
            "leave_group_call",
            "leave",
        ]):
            return True

        # Generic stop as fallback.
        if await self._call_any(self.calls, [
            "stop",
            "stop_stream",
        ], chat_id):
            return True

        if await self._call_any(self.calls, [
            "stop",
            "stop_stream",
        ]):
            return True

        # Last resort: disconnect the client.
        if self._client is not None:
            try:
                await self._maybe_await(self._client.disconnect())
                return True
            except Exception as exc:
                logger.debug("client disconnect fallback failed: %s", exc)
        return False

    # ---------- state ----------
    def state(self, chat_id: int) -> dict[str, Any]:
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        q = self._queue(chat_id)
        return {
            "ok": True,
            "chat_id": chat_id,
            "ready": self.ready,
            "backend_error": self.backend_error,
            "session": {
                "chat_id": s.chat_id,
                "status": s.status,
                "title": s.title,
                "source_type": s.source_type,
                "source_id": s.source_id,
                "duration": s.duration,
                "offset": s.offset,
                "paused": s.paused,
                "last_error": s.last_error,
                "local_path": s.local_path,
                "video": s.video,
                "updated_at": s.updated_at,
            },
            "queue_length": len(q),
            "queue": [item.to_dict() for item in q],
        }

    def active_sessions_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status in {"playing", "paused"})

    def queues_count(self) -> int:
        return sum(len(q) for q in self._queues.values())

    # ---------- operations ----------
    async def meta(self, payload: MetaRequest) -> dict[str, Any]:
        await self.ensure_ready()
        ok, video = self._allowed_source(payload.source_type)
        if not ok:
            return {
                "ok": False,
                "action": "meta",
                "error": "unsupported_source_type",
                "detail": payload.source_type,
                "state": self.state(payload.chat_id),
            }

        s = self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id)
        s.title = payload.title
        s.source_type = payload.source_type
        s.source_id = payload.source_id
        s.duration = payload.duration
        s.video = video
        self._sessions[payload.chat_id] = self._touch(s)
        return {"ok": True, "action": "meta", "state": self.state(payload.chat_id)}

    async def start(self, payload: StartRequest) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id)

        ok, video_hint = self._allowed_source(payload.source_type)
        if not ok:
            s.status = "error"
            s.last_error = f"unsupported_source_type: {payload.source_type}"
            self._sessions[payload.chat_id] = self._touch(s)
            return {
                "ok": False,
                "action": "start",
                "error": "unsupported_source_type",
                "detail": payload.source_type,
                "state": self.state(payload.chat_id),
            }

        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[payload.chat_id] = self._touch(s)
            return {
                "ok": False,
                "action": "start",
                "error": "service_not_ready",
                "detail": self.backend_error,
                "state": self.state(payload.chat_id),
            }

        local_path = ""
        video = video_hint
        try:
            local_path_obj, video_by_file, _ = await self._resolve_telegram_file(
                payload.source_id,
                source_type=payload.source_type,
                title=payload.title,
            )
            local_path = str(local_path_obj)
            video = video_by_file or video_hint

            # Stop any previous playback first.
            try:
                await self._stop_backend(payload.chat_id)
            except Exception:
                pass

            played = False
            if self.calls is not None:
                played = await self._call_any(
                    self.calls,
                    ["play", "start", "join", "join_group_call", "create"],
                    payload.chat_id,
                    local_path,
                    title=payload.title,
                    duration=payload.duration,
                    offset=payload.offset,
                )
                if not played:
                    played = await self._call_any(
                        self.calls,
                        ["play", "start", "join", "join_group_call"],
                        payload.chat_id,
                        local_path,
                    )

            if not played:
                raise RuntimeError("method_not_supported: play/start/join")

            s.status = "playing"
            s.paused = False
            s.last_error = ""
            s.title = payload.title
            s.source_type = payload.source_type
            s.source_id = payload.source_id
            s.duration = payload.duration
            s.offset = payload.offset
            s.local_path = local_path
            s.video = video
            self._sessions[payload.chat_id] = self._touch(s)
            return {"ok": True, "action": "start", "played": True, "state": self.state(payload.chat_id)}

        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            s.local_path = local_path or s.local_path
            s.video = video
            self._sessions[payload.chat_id] = self._touch(s)
            return {
                "ok": False,
                "action": "start",
                "error": type(exc).__name__,
                "detail": str(exc),
                "state": self.state(payload.chat_id),
            }

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)

        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(s)
            return {
                "ok": False,
                "action": "pause",
                "error": "service_not_ready",
                "detail": self.backend_error,
                "state": self.state(chat_id),
            }

        try:
            paused = await self._call_any(self.calls, ["pause", "pause_stream"], chat_id) if self.calls else False
            s.status = "paused" if paused else s.status
            s.paused = True
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "pause", "paused": paused, "state": self.state(chat_id)}
        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(s)
            return {
                "ok": False,
                "action": "pause",
                "error": type(exc).__name__,
                "detail": str(exc),
                "state": self.state(chat_id),
            }

    async def resume(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)

        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(s)
            return {
                "ok": False,
                "action": "resume",
                "error": "service_not_ready",
                "detail": self.backend_error,
                "state": self.state(chat_id),
            }

        try:
            resumed = await self._call_any(self.calls, ["resume", "resume_stream"], chat_id) if self.calls else False
            s.status = "playing" if resumed else s.status
            s.paused = False
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "resume", "resumed": resumed, "state": self.state(chat_id)}
        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(s)
            return {
                "ok": False,
                "action": "resume",
                "error": type(exc).__name__,
                "detail": str(exc),
                "state": self.state(chat_id),
            }

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)

        if not self.ready:
            s.status = "stopped"
            s.paused = False
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions.pop(chat_id, None)
            self._queues.pop(chat_id, None)
            await self._cleanup_file(s.local_path)
            return {
                "ok": False,
                "action": "stop",
                "error": "service_not_ready",
                "detail": self.backend_error,
                "state": self.state(chat_id),
            }

        try:
            async with self._lock:
                backend_stopped = await self._stop_backend(chat_id)

            s.status = "stopped"
            s.paused = False
            s.last_error = "" if backend_stopped else "backend_stop_noop"
            await self._cleanup_file(s.local_path)
            self._sessions.pop(chat_id, None)
            self._queues.pop(chat_id, None)
            return {
                "ok": True if backend_stopped else False,
                "action": "stop",
                "stopped": backend_stopped,
                "state": self.state(chat_id),
            }
        except Exception as exc:
            logger.exception("audio backend stop failed")
            s.status = "stopped"
            s.paused = False
            s.last_error = f"{type(exc).__name__}: {exc}"
            await self._cleanup_file(s.local_path)
            self._sessions.pop(chat_id, None)
            self._queues.pop(chat_id, None)
            return {
                "ok": False,
                "action": "stop",
                "error": type(exc).__name__,
                "detail": str(exc),
                "state": self.state(chat_id),
            }

    async def seek(self, chat_id: int, delta: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            return {
                "ok": False,
                "action": "seek",
                "error": "service_not_ready",
                "detail": self.backend_error,
                "state": self.state(chat_id),
            }

        try:
            if not self.calls:
                return {"ok": False, "action": "seek", "error": "calls_not_ready", "state": self.state(chat_id)}
            fn = getattr(self.calls, "seek", None)
            if not callable(fn):
                return {"ok": False, "action": "seek", "error": "method_not_supported", "state": self.state(chat_id)}
            await self._maybe_await(fn(chat_id, delta))
            s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            s.offset = max(0, s.offset + int(delta))
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "seek", "state": self.state(chat_id)}
        except Exception as exc:
            return {
                "ok": False,
                "action": "seek",
                "error": type(exc).__name__,
                "detail": str(exc),
                "state": self.state(chat_id),
            }

    async def enqueue(self, payload: QueueAddRequest) -> dict[str, Any]:
        await self.ensure_ready()
        ok, video = self._allowed_source(payload.source_type)
        if not ok:
            return {
                "ok": False,
                "action": "queue_add",
                "error": "unsupported_source_type",
                "detail": payload.source_type,
                "state": self.state(payload.chat_id),
            }

        item = QueueItem(
            chat_id=payload.chat_id,
            source_type=payload.source_type,
            source_id=payload.source_id,
            title=payload.title,
            duration=payload.duration,
            requested_by=payload.requested_by,
            auto_start=payload.auto_start,
            video=video,
        )
        q = self._queue(payload.chat_id)
        q.append(item)
        self._sessions[payload.chat_id] = self._touch(self._sessions.get(payload.chat_id) or AudioSession(chat_id=payload.chat_id))

        if payload.auto_start and self.state(payload.chat_id)["session"]["status"] in {"idle", "stopped"}:
            start_res = await self.start(StartRequest(
                chatId=payload.chat_id,
                sourceType=payload.source_type,
                sourceId=payload.source_id,
                title=payload.title,
                duration=payload.duration,
                offset=0,
            ))
            return {
                "ok": True,
                "action": "queue_add",
                "auto_started": True,
                "start_result": start_res,
                "state": self.state(payload.chat_id),
            }

        return {
            "ok": True,
            "action": "queue_add",
            "queued": True,
            "queue_length": len(q),
            "state": self.state(payload.chat_id),
        }

    async def queue_list(self, chat_id: int) -> dict[str, Any]:
        q = self._queue(chat_id)
        return {
            "ok": True,
            "action": "queue_list",
            "queue": [item.to_dict() for item in q],
            "state": self.state(chat_id),
        }

    async def queue_clear(self, chat_id: int) -> dict[str, Any]:
        q = self._queues.pop(chat_id, None)
        if q:
            for item in q:
                pass
        s = self._sessions.get(chat_id)
        if s:
            self._sessions[chat_id] = self._touch(s)
        return {"ok": True, "action": "queue_clear", "state": self.state(chat_id)}

    async def skip(self, chat_id: int) -> dict[str, Any]:
        q = self._queue(chat_id)
        if q:
            q.popleft()
        if not q:
            return await self.stop(chat_id)

        next_item = q[0]
        return await self.start(StartRequest(
            chatId=chat_id,
            sourceType=next_item.source_type,
            sourceId=next_item.source_id,
            title=next_item.title,
            duration=next_item.duration,
            offset=0,
        ))


service = AudioService()