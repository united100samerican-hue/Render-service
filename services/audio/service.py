from __future__ import annotations

import asyncio
import logging
import os
import tempfile
from collections import deque
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Deque

import httpx
from telethon import TelegramClient, functions
from telethon.sessions import StringSession

try:
    from pytgcalls import PyTgCalls
except Exception:  # pragma: no cover
    PyTgCalls = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_service")

ALLOWED_SOURCE_TYPES = {"telegram", "telegram_file_id", "telegram_audio", "telegram_video", "file_id"}
AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".aac", ".flac", ".opus"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}


@dataclass
class AudioSession:
    chat_id: int
    status: str = "idle"
    title: str = ""
    source_type: str = ""
    source_id: str = ""
    duration: int = 0
    offset: int = 0
    paused: bool = False
    last_error: str = ""
    local_path: str = ""
    video: bool = False
    updated_at: float = 0.0


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


class AudioService:
    def __init__(self) -> None:
        self.api_id = int(os.getenv("API_ID", "0") or "0")
        self.api_hash = os.getenv("API_HASH", "").strip()
        self.session_string = os.getenv("SESSION_STRING", "").strip()
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        self.ready = False
        self.backend_error = ""
        self._client: TelegramClient | None = None
        self.calls: Any = None
        self._lock = asyncio.Lock()
        self._sessions: dict[int, AudioSession] = {}
        self._queues: dict[int, Deque[QueueItem]] = {}
        self._download_dir = Path(tempfile.gettempdir()) / "render_audio_service_media"
        self._download_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if asyncio.iscoroutine(value):
            return await value
        return value

    def _now(self) -> float:
        try:
            return asyncio.get_event_loop().time()
        except Exception:
            import time
            return time.time()

    def _touch(self, s: AudioSession) -> AudioSession:
        s.updated_at = self._now()
        return s

    def _queue(self, chat_id: int) -> Deque[QueueItem]:
        q = self._queues.get(chat_id)
        if q is None:
            q = deque()
            self._queues[chat_id] = q
        return q

    def _normalize_source_type(self, source_type: str) -> str:
        st = (source_type or "").strip().lower() or "telegram_file_id"
        if st not in ALLOWED_SOURCE_TYPES:
            raise ValueError("unsupported_source_type")
        return st

    def _infer_video_from_name(self, source_type: str, file_name: str) -> bool:
        st = self._normalize_source_type(source_type)
        suffix = Path(file_name).suffix.lower()
        if st == "telegram_video":
            return True
        if st == "telegram_audio":
            return False
        if suffix in VIDEO_EXTS:
            return True
        if suffix in AUDIO_EXTS:
            return False
        return False

    async def _http_get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.get(url, params=params)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise RuntimeError("invalid_json_response")
            return data

    async def _http_get_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.get(url)
            r.raise_for_status()
            return r.content

    async def _download_telegram_file(self, file_id: str, source_type: str, title: str = "") -> tuple[Path, bool]:
        if not self.bot_token:
            raise RuntimeError("missing_env: BOT_TOKEN")
        info = await self._http_get_json(f"https://api.telegram.org/bot{self.bot_token}/getFile", params={"file_id": file_id})
        if not info.get("ok"):
            raise RuntimeError(f"telegram_getFile_failed: {info}")
        file_path = str(info["result"]["file_path"])
        file_name = Path(file_path).name or (title.strip() or file_id)
        video = self._infer_video_from_name(source_type, file_name)
        ext = Path(file_path).suffix.lower() or (".mp4" if video else ".ogg")
        local_path = self._download_dir / f"{file_id.replace('/', '_')}{ext}"
        local_path.write_bytes(await self._http_get_bytes(f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"))
        return local_path, video

    async def _call_any(self, obj: Any, method_names: list[str], *args: Any, **kwargs: Any) -> bool:
        if obj is None:
            return False
        for name in method_names:
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
        return False

    async def ensure_ready(self) -> None:
        if self.ready:
            return
        if not self.api_id or not self.api_hash or not self.session_string:
            self.ready = False
            self.backend_error = "missing_env: API_ID/API_HASH/SESSION_STRING"
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
            for s in list(self._sessions.values()):
                if s.local_path:
                    try:
                        Path(s.local_path).unlink(missing_ok=True)
                    except Exception:
                        pass
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

    async def _stop_backend(self, chat_id: int) -> bool:
        if self._client is not None:
            try:
                entity = await self._client.get_entity(chat_id)
                full = await self._client(functions.channels.GetFullChannelRequest(channel=entity))
                call = getattr(getattr(full, "full_chat", None), "call", None)
                if call:
                    try:
                        res = self._client(functions.phone.LeaveGroupCallRequest(call=call, source=0))
                    except TypeError:
                        res = self._client(functions.phone.LeaveGroupCallRequest(call=call))
                    if asyncio.iscoroutine(res):
                        await res
                    return True
            except Exception as exc:
                logger.debug("raw telethon leave failed: %s", exc)

        targets: list[Any] = []
        if self.calls is not None:
            targets.extend([
                self.calls,
                getattr(self.calls, "group_call", None),
                getattr(self.calls, "mtproto", None),
                getattr(self.calls, "_group_call", None),
                getattr(self.calls, "_call", None),
            ])
        for obj in targets:
            if not obj:
                continue
            for name in ("stop", "leave_current_group_call", "leave_group_call", "hangup", "close", "disconnect"):
                fn = getattr(obj, name, None)
                if not callable(fn):
                    continue
                for args in ((chat_id,), ()): 
                    try:
                        res = fn(*args)
                        if asyncio.iscoroutine(res):
                            await res
                        return True
                    except TypeError:
                        continue
                    except Exception as exc:
                        logger.debug("backend %s failed: %s", name, exc)
                        continue
        if self._client is not None:
            try:
                await self._client.disconnect()
                return True
            except Exception as exc:
                logger.debug("client disconnect fallback failed: %s", exc)
        return False

    async def meta(self, chat_id: int, source_type: str, source_id: str, title: str = "", duration: int = 0) -> dict[str, Any]:
        await self.ensure_ready()
        st = self._normalize_source_type(source_type)
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        s.title = title
        s.source_type = st
        s.source_id = source_id
        s.duration = int(duration or 0)
        s.video = st == "telegram_video"
        self._sessions[chat_id] = self._touch(s)
        return {"ok": True, "action": "meta", "state": self.state(chat_id)}

    async def start(self, chat_id: int, source_type: str, source_id: str, title: str = "", duration: int = 0, offset: int = 0) -> dict[str, Any]:
        await self.ensure_ready()
        st = self._normalize_source_type(source_type)
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "start", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(chat_id)}
        try:
            local_path, video = await self._download_telegram_file(source_id, st, title)
            try:
                await self._stop_backend(chat_id)
            except Exception:
                pass
            played = False
            if self.calls is not None:
                played = await self._call_any(self.calls, ["play", "start", "join", "join_group_call"], chat_id, str(local_path))
                if not played:
                    played = await self._call_any(self.calls, ["play", "start", "join_group_call"], chat_id, str(local_path), title=title)
            if not played:
                raise RuntimeError("method_not_supported: play/start/join")
            s.status = "playing"
            s.title = title
            s.source_type = st
            s.source_id = source_id
            s.duration = int(duration or 0)
            s.offset = int(offset or 0)
            s.paused = False
            s.last_error = ""
            s.local_path = str(local_path)
            s.video = video
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "start", "played": True, "state": self.state(chat_id)}
        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "start", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        if not self.ready:
            s.status = "error"
            s.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "pause", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(chat_id)}
        try:
            paused = False
            if self.calls is not None:
                paused = await self._call_any(self.calls, ["pause", "pause_stream"], chat_id)
            s.status = "paused"
            s.paused = True
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
            resumed = False
            if self.calls is not None:
                resumed = await self._call_any(self.calls, ["resume", "resume_stream"], chat_id)
            s.status = "playing"
            s.paused = False
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "resume", "resumed": resumed, "state": self.state(chat_id)}
        except Exception as exc:
            s.status = "error"
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(s)
            return {"ok": False, "action": "resume", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def seek(self, chat_id: int, delta: int = 0) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            return {"ok": False, "action": "seek", "error": "service_not_ready", "detail": self.backend_error, "state": self.state(chat_id)}
        try:
            moved = False
            if self.calls is not None:
                moved = await self._call_any(self.calls, ["seek"], chat_id, int(delta))
            s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            s.offset = max(0, int(s.offset or 0) + int(delta or 0))
            self._sessions[chat_id] = self._touch(s)
            return {"ok": True, "action": "seek", "moved": moved, "state": self.state(chat_id)}
        except Exception as exc:
            return {"ok": False, "action": "seek", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        s = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        try:
            async with self._lock:
                backend_stopped = await self._stop_backend(chat_id)
            s.status = "stopped"
            s.paused = False
            s.last_error = "" if backend_stopped else "backend_stop_noop"
            if s.local_path:
                try:
                    Path(s.local_path).unlink(missing_ok=True)
                except Exception:
                    pass
            self._sessions.pop(chat_id, None)
            self._queues.pop(chat_id, None)
            return {"ok": backend_stopped, "action": "stop", "stopped": backend_stopped, "state": self.state(chat_id)}
        except Exception as exc:
            logger.exception("audio backend stop failed")
            s.status = "stopped"
            s.paused = False
            s.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions.pop(chat_id, None)
            self._queues.pop(chat_id, None)
            return {"ok": False, "action": "stop", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def enqueue(self, chat_id: int, source_type: str, source_id: str, title: str = "", duration: int = 0, requested_by: str = "", auto_start: bool = True) -> dict[str, Any]:
        await self.ensure_ready()
        st = self._normalize_source_type(source_type)
        item = QueueItem(chat_id=chat_id, source_type=st, source_id=source_id, title=title, duration=int(duration or 0), requested_by=requested_by, auto_start=bool(auto_start), video=st == "telegram_video")
        q = self._queue(chat_id)
        q.append(item)
        self._sessions[chat_id] = self._touch(self._sessions.get(chat_id) or AudioSession(chat_id=chat_id))
        if auto_start and self._sessions.get(chat_id, AudioSession(chat_id)).status in {"idle", "stopped", "error"}:
            start_result = await self.start(chat_id, source_type, source_id, title=title, duration=duration, offset=0)
            return {"ok": True, "action": "enqueue", "queued": True, "auto_started": True, "start_result": start_result, "queue_length": len(q), "state": self.state(chat_id)}
        return {"ok": True, "action": "enqueue", "queued": True, "queue_length": len(q), "state": self.state(chat_id)}

    async def queue_list(self, chat_id: int) -> dict[str, Any]:
        q = self._queue(chat_id)
        return {"ok": True, "action": "queue_list", "queue": [item.to_dict() for item in q], "state": self.state(chat_id)}

    async def queue_clear(self, chat_id: int) -> dict[str, Any]:
        self._queues.pop(chat_id, None)
        if chat_id in self._sessions:
            self._sessions[chat_id] = self._touch(self._sessions[chat_id])
        return {"ok": True, "action": "queue_clear", "state": self.state(chat_id)}

    async def skip(self, chat_id: int) -> dict[str, Any]:
        q = self._queue(chat_id)
        if q:
            q.popleft()
        if not q:
            return await self.stop(chat_id)
        next_item = q[0]
        return await self.start(chat_id, next_item.source_type, next_item.source_id, title=next_item.title, duration=next_item.duration, offset=0)


service = AudioService()