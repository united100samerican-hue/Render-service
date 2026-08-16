from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    from pytgcalls import PyTgCalls
except Exception:  # pragma: no cover
    PyTgCalls = None  # type: ignore[assignment]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_service")

# This service intentionally handles Telegram media only.
# URL/YouTube playback is deliberately excluded and can be added later as a separate service.
ALLOWED_SOURCE_TYPES = {"telegram_file_id", "telegram_audio", "telegram_video", "telegram_message"}
AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".aac", ".flac", ".opus"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}


@dataclass
class AudioSession:
    chat_id: int
    status: str = "idle"
    title: str = ""
    source_type: str = ""
    source_id: str = ""
    source_chat_id: str = ""
    source_message_id: str = ""
    duration: int = 0
    position: int = 0
    paused: bool = False
    last_error: str = ""
    local_path: str = ""
    video: bool = False
    updated_at: float = 0.0


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
        self._sessions: dict[int, AudioSession] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._download_dir = Path(tempfile.gettempdir()) / "render_audio_service_media"
        self._download_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    async def _maybe_await(value: Any) -> Any:
        if asyncio.iscoroutine(value):
            return await value
        return value

    def _now(self) -> float:
        return time.time()

    def _touch(self, session: AudioSession) -> AudioSession:
        session.updated_at = self._now()
        return session

    def _lock_for(self, chat_id: int) -> asyncio.Lock:
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[chat_id] = lock
        return lock

    def _normalize_source_type(self, source_type: str) -> str:
        raw = (source_type or "").strip().lower().replace("-", "_")
        aliases = {
            "telegram": "telegram_file_id",
            "tg": "telegram_file_id",
            "telegram_file": "telegram_file_id",
            "telegram_file_id": "telegram_file_id",
            "telegram_media": "telegram_file_id",
            "telegram_document": "telegram_file_id",
            "file": "telegram_file_id",
            "document": "telegram_file_id",
            "media": "telegram_file_id",
            "audio": "telegram_audio",
            "voice": "telegram_audio",
            "song": "telegram_audio",
            "music": "telegram_audio",
            "telegram_audio": "telegram_audio",
            "video": "telegram_video",
            "clip": "telegram_video",
            "movie": "telegram_video",
            "telegram_video": "telegram_video",
            "telegram_message": "telegram_message",
        }
        normalized = aliases.get(raw, raw or "telegram_file_id")
        if normalized not in ALLOWED_SOURCE_TYPES:
            raise ValueError("unsupported_source_type")
        return normalized

    @staticmethod
    def _infer_video(source_type: str, file_name: str = "", mime_type: str = "") -> bool:
        st = source_type.strip().lower()
        suffix = Path(file_name).suffix.lower()
        if st == "telegram_video":
            return True
        if st == "telegram_audio":
            return False
        if mime_type.startswith("video/"):
            return True
        if mime_type.startswith("audio/"):
            return False
        if suffix in VIDEO_EXTS:
            return True
        if suffix in AUDIO_EXTS:
            return False
        return False

    async def _http_get_json(self, url: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise RuntimeError("invalid_json_response")
            return data

    async def _http_get_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(timeout=300) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.content

    async def _download_telegram_file(
        self,
        file_id: str,
        source_type: str,
        title: str = "",
    ) -> tuple[Path, bool]:
        if not self.bot_token:
            raise RuntimeError("missing_env: BOT_TOKEN")
        if not file_id.strip():
            raise RuntimeError("missing_source_id")

        info = await self._http_get_json(
            f"https://api.telegram.org/bot{self.bot_token}/getFile",
            params={"file_id": file_id},
        )
        if not info.get("ok"):
            raise RuntimeError(f"telegram_getFile_failed: {info}")

        file_path = str(info["result"]["file_path"])
        original_name = Path(file_path).name or (title.strip() or file_id)
        video = self._infer_video(source_type, original_name)
        ext = Path(file_path).suffix.lower() or (".mp4" if video else ".ogg")
        unique = re.sub(r"[^A-Za-z0-9._-]+", "_", file_id)[:80]
        local_path = self._download_dir / f"{unique}_{int(time.time() * 1000)}{ext}"
        content = await self._http_get_bytes(
            f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        )
        local_path.write_bytes(content)
        return local_path, video

    async def _download_telegram_message(
        self,
        source_chat_id: int,
        source_message_id: int,
        source_type: str,
        title: str = "",
    ) -> tuple[Path, bool]:
        if self._client is None:
            raise RuntimeError("missing_telegram_client")
        if not source_chat_id or not source_message_id:
            raise RuntimeError("missing_source_message_reference")

        message = await self._client.get_messages(int(source_chat_id), ids=int(source_message_id))
        if not message or not getattr(message, "media", None):
            raise RuntimeError("telegram_message_not_found")

        mime_type = str(getattr(getattr(message, "file", None), "mime_type", "") or "").lower()
        file_name = str(getattr(getattr(message, "file", None), "name", "") or "")
        video = bool(getattr(message, "video", None)) or self._infer_video(source_type, file_name, mime_type)
        stem = re.sub(
            r"[^A-Za-z0-9._-]+",
            "_",
            title or file_name or f"{source_chat_id}_{source_message_id}",
        )[:80]
        local_hint = self._download_dir / f"{stem}_{int(time.time() * 1000)}"
        local_file = await self._client.download_media(message, file=str(local_hint))
        if not local_file:
            raise RuntimeError("telegram_download_failed")
        return Path(local_file), video

    async def _probe_duration(self, path: Path) -> int:
        """Read the real media duration using ffprobe when available."""
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "default=noprint_wrappers=1:nokey=1",
                    str(path),
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if proc.returncode != 0:
                return 0
            value = float((proc.stdout or "").strip() or 0)
            return max(0, int(round(value)))
        except Exception as exc:
            logger.debug("duration probe failed for %s: %s", path, exc)
            return 0

    async def _cleanup_file(self, path: str) -> None:
        if not path:
            return
        try:
            await asyncio.to_thread(Path(path).unlink, missing_ok=True)
        except Exception:
            pass

    async def _cleanup_download_dir(self) -> None:
        try:
            for path in self._download_dir.iterdir():
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    shutil.rmtree(path, ignore_errors=True)
        except Exception:
            logger.exception("download_dir_cleanup_failed")

    async def _call_any(self, obj: Any, method_names: list[str], *args: Any, **kwargs: Any) -> bool:
        if obj is None:
            return False
        for name in method_names:
            fn = getattr(obj, name, None)
            if not callable(fn):
                continue
            try:
                result = fn(*args, **kwargs)
                await self._maybe_await(result)
                return True
            except TypeError:
                continue
        return False

    async def ensure_ready(self) -> None:
        if self.ready:
            return
        if not self.api_id or not self.api_hash or not self.session_string:
            self.ready = False
            self.backend_error = "missing_env: API_ID/API_HASH/SESSION_STRING"
            return
        if PyTgCalls is None:
            self.ready = False
            self.backend_error = "pytgcalls_import_failed"
            return

        if self._client is None:
            self._client = TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)

        try:
            await self._cleanup_download_dir()
            if not self._client.is_connected():
                await self._client.connect()
            if self.calls is None:
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
            for session in list(self._sessions.values()):
                await self._cleanup_file(session.local_path)
            self._sessions.clear()
        finally:
            try:
                if self.calls is not None:
                    stop = getattr(self.calls, "stop", None)
                    if callable(stop):
                        await self._maybe_await(stop())
            except Exception:
                logger.exception("pytgcalls_stop_failed")
            finally:
                if self._client is not None:
                    try:
                        await self._client.disconnect()
                    except Exception:
                        pass
                self.calls = None
                self._client = None
                self.ready = False

    def active_sessions_count(self) -> int:
        return sum(1 for s in self._sessions.values() if s.status in {"playing", "paused"})

    def state(self, chat_id: int) -> dict[str, Any]:
        session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        return {
            "ok": True,
            "chat_id": chat_id,
            "ready": self.ready,
            "backend_error": self.backend_error,
            "session": {
                "chat_id": session.chat_id,
                "status": session.status,
                "title": session.title,
                "source_type": session.source_type,
                "source_id": session.source_id,
                "source_chat_id": session.source_chat_id,
                "source_message_id": session.source_message_id,
                "duration": session.duration,
                "position": session.position,
                "paused": session.paused,
                "last_error": session.last_error,
                "video": session.video,
                "updated_at": session.updated_at,
            },
        }

    async def _leave_call(self, chat_id: int) -> bool:
        """Explicitly leave the voice chat. Never call this during track switching."""
        if self.calls is None:
            return False
        if await self._call_any(self.calls, ["leave_call"], chat_id):
            return True
        return await self._call_any(self.calls, ["leave_current_group_call"])

    async def _play_file(self, chat_id: int, local_path: str) -> None:
        if self.calls is None:
            raise RuntimeError("pytgcalls_not_ready")
        # PyTgCalls' play() is intentionally used for both first play and switching.
        # It must be allowed to manage the existing group call without an explicit leave.
        if not await self._call_any(self.calls, ["play"], chat_id, local_path):
            raise RuntimeError("play_method_failed")

    async def _load_source(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        title: str,
        source_chat_id: int,
        source_message_id: int,
    ) -> tuple[Path, bool, int]:
        st = self._normalize_source_type(source_type)
        if st == "telegram_message":
            path, video = await self._download_telegram_message(
                source_chat_id,
                source_message_id,
                st,
                title,
            )
        else:
            path, video = await self._download_telegram_file(source_id, st, title)
        duration = await self._probe_duration(path)
        return path, video, duration

    async def meta(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        title: str = "",
        duration: int = 0,
        source_chat_id: int = 0,
        source_message_id: int = 0,
    ) -> dict[str, Any]:
        await self.ensure_ready()
        st = self._normalize_source_type(source_type)
        session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        session.title = title
        session.source_type = st
        session.source_id = source_id
        session.source_chat_id = str(source_chat_id or "")
        session.source_message_id = str(source_message_id or "")
        session.duration = max(0, int(duration or 0))
        session.video = st == "telegram_video"
        self._sessions[chat_id] = self._touch(session)
        return {"ok": True, "action": "meta", "state": self.state(chat_id)}

    async def start(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        title: str = "",
        duration: int = 0,
        offset: int = 0,
        source_chat_id: int = 0,
        source_message_id: int = 0,
    ) -> dict[str, Any]:
        """Start first playback or replace the current stream without leaving the call."""
        await self.ensure_ready()
        st = self._normalize_source_type(source_type)
        if not self.ready:
            session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            session.status = "error"
            session.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(session)
            return {
                "ok": False,
                "action": "start",
                "error": "service_not_ready",
                "detail": self.backend_error,
                "state": self.state(chat_id),
            }

        async with self._lock_for(chat_id):
            session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            previous_path = session.local_path
            local_path = ""
            try:
                path_obj, video, probed_duration = await self._load_source(
                    chat_id,
                    st,
                    source_id,
                    title,
                    source_chat_id,
                    source_message_id,
                )
                local_path = str(path_obj)
                await self._play_file(chat_id, local_path)

                real_duration = probed_duration or max(0, int(duration or 0))
                session.status = "playing"
                session.title = title
                session.source_type = st
                session.source_id = source_id
                session.source_chat_id = str(source_chat_id or session.source_chat_id or "")
                session.source_message_id = str(source_message_id or session.source_message_id or "")
                session.duration = real_duration
                session.position = max(0, int(offset or 0))
                session.paused = False
                session.last_error = ""
                session.local_path = local_path
                session.video = video
                self._sessions[chat_id] = self._touch(session)

                if previous_path and previous_path != local_path:
                    await self._cleanup_file(previous_path)

                logger.info(
                    "playback_started chat_id=%s title=%s duration=%s video=%s",
                    chat_id,
                    title,
                    real_duration,
                    video,
                )
                return {
                    "ok": True,
                    "action": "start",
                    "played": True,
                    "switched": bool(previous_path),
                    "state": self.state(chat_id),
                }
            except Exception as exc:
                session.status = "error"
                session.last_error = f"{type(exc).__name__}: {exc}"
                if local_path:
                    await self._cleanup_file(local_path)
                self._sessions[chat_id] = self._touch(session)
                logger.exception("audio start failed chat_id=%s", chat_id)
                return {
                    "ok": False,
                    "action": "start",
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "state": self.state(chat_id),
                }

    async def next(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        title: str = "",
        duration: int = 0,
        offset: int = 0,
        source_chat_id: int = 0,
        source_message_id: int = 0,
    ) -> dict[str, Any]:
        result = await self.start(
            chat_id,
            source_type,
            source_id,
            title=title,
            duration=duration,
            offset=offset,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
        )
        result["action"] = "next"
        return result

    async def skip(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        title: str = "",
        duration: int = 0,
        offset: int = 0,
        source_chat_id: int = 0,
        source_message_id: int = 0,
    ) -> dict[str, Any]:
        result = await self.next(
            chat_id,
            source_type,
            source_id,
            title=title,
            duration=duration,
            offset=offset,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
        )
        result["action"] = "skip"
        return result

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        if not self.ready:
            session.status = "error"
            session.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(session)
            return {"ok": False, "action": "pause", "error": "service_not_ready", "state": self.state(chat_id)}
        try:
            result = await self._call_any(self.calls, ["pause", "pause_stream"], chat_id)
            if not result:
                raise RuntimeError("pause_method_failed")
            session.status = "paused"
            session.paused = True
            self._sessions[chat_id] = self._touch(session)
            return {"ok": True, "action": "pause", "paused": True, "state": self.state(chat_id)}
        except Exception as exc:
            session.status = "error"
            session.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(session)
            return {"ok": False, "action": "pause", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def resume(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        if not self.ready:
            session.status = "error"
            session.last_error = self.backend_error or "service_not_ready"
            self._sessions[chat_id] = self._touch(session)
            return {"ok": False, "action": "resume", "error": "service_not_ready", "state": self.state(chat_id)}
        try:
            result = await self._call_any(self.calls, ["resume", "resume_stream"], chat_id)
            if not result:
                raise RuntimeError("resume_method_failed")
            session.status = "playing"
            session.paused = False
            self._sessions[chat_id] = self._touch(session)
            return {"ok": True, "action": "resume", "resumed": True, "state": self.state(chat_id)}
        except Exception as exc:
            session.status = "error"
            session.last_error = f"{type(exc).__name__}: {exc}"
            self._sessions[chat_id] = self._touch(session)
            return {"ok": False, "action": "resume", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def seek(self, chat_id: int, delta: int = 0) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.ready:
            return {"ok": False, "action": "seek", "error": "service_not_ready", "state": self.state(chat_id)}
        try:
            result = await self._call_any(self.calls, ["seek"], chat_id, int(delta))
            if not result:
                raise RuntimeError("seek_method_failed")
            session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            session.position = max(0, int(session.position or 0) + int(delta or 0))
            self._sessions[chat_id] = self._touch(session)
            return {"ok": True, "action": "seek", "moved": True, "state": self.state(chat_id)}
        except Exception as exc:
            return {"ok": False, "action": "seek", "error": type(exc).__name__, "detail": str(exc), "state": self.state(chat_id)}

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        async with self._lock_for(chat_id):
            session = self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            try:
                left = await self._leave_call(chat_id)
                await self._cleanup_file(session.local_path)
                self._sessions.pop(chat_id, None)
                self._locks.pop(chat_id, None)
                return {
                    "ok": left,
                    "action": "stop",
                    "stopped": left,
                    "state": self.state(chat_id),
                }
            except Exception as exc:
                logger.exception("audio stop failed chat_id=%s", chat_id)
                self._sessions.pop(chat_id, None)
                return {
                    "ok": False,
                    "action": "stop",
                    "error": type(exc).__name__,
                    "detail": str(exc),
                    "state": self.state(chat_id),
                }


service = AudioService()