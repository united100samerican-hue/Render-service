
from __future__ import annotations

import asyncio
import logging
import os
import time
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Deque, Optional
from collections import deque

import httpx
from telethon import TelegramClient, functions
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
    "telegram_audio",
    "telegram_video",
    "telegram",
}

AUDIO_EXTS = {".mp3", ".ogg", ".oga", ".wav", ".m4a", ".aac", ".flac", ".opus"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}


@dataclass
class Track:
    id: str
    chat_id: int
    source_type: str
    source_id: str
    title: str = ""
    duration: int = 0
    local_path: str = ""
    original_path: str = ""
    offset: int = 0
    created_at: float = field(default_factory=time.time)
    video: bool = False
    requested_by: str = ""


@dataclass
class SessionState:
    chat_id: int
    status: str = "idle"  # idle | starting | playing | paused | stopped | error
    current: Track | None = None
    queue: list[Track] = field(default_factory=list)
    gc: Any = None
    ended_hooked: bool = False
    started_at: float = 0.0
    pause_started_at: float = 0.0
    paused_seconds: float = 0.0
    paused: bool = False
    last_update_at: float = field(default_factory=time.time)
    last_error: str = ""
    temp_path: str = ""


class AudioService:
    """
    Telegram-only audio/video service.

    Supported:
    - Telegram file_id / media downloaded from Telegram
    - queue
    - play / pause / resume / seek / stop
    - raw Telethon leave-group-call fallback for reliable stop
    """

    def __init__(self) -> None:
        self.api_id = int(os.getenv("API_ID", "0") or "0")
        self.api_hash = os.getenv("API_HASH", "").strip()
        self.session_string = os.getenv("SESSION_STRING", "").strip()
        self.bot_token = os.getenv("BOT_TOKEN", "").strip()
        self.ready = False
        self.backend_error = ""

        self.client: TelegramClient | None = None
        self.calls: Any = None
        self.calls_started = False

        self.lock = asyncio.Lock()
        self.boot_lock = asyncio.Lock()

        self.sessions: dict[int, SessionState] = {}
        self._download_dir = Path(tempfile.gettempdir()) / "render_audio_service_media"
        self._download_dir.mkdir(parents=True, exist_ok=True)

    # -------------------- helpers --------------------
    async def _maybe(self, v: Any) -> Any:
        return await v if asyncio.iscoroutine(v) else v

    def _sess(self, chat_id: int) -> SessionState:
        s = self.sessions.get(chat_id)
        if s is None:
            s = SessionState(chat_id=chat_id)
            self.sessions[chat_id] = s
        return s

    def _touch(self, s: SessionState) -> None:
        s.last_update_at = time.time()

    def _is_url(self, s: str) -> bool:
        s = str(s or "").strip().lower()
        return s.startswith(("http://", "https://")) or "youtu.be/" in s or "youtube.com/" in s or "music.youtube.com/" in s

    def _looks_like_file_id(self, s: str) -> bool:
        s = str(s or "").strip()
        if not s or self._is_url(s):
            return False
        if " " in s:
            return False
        if len(s) < 10:
            return False
        # Telegram file_id / media id patterns are opaque; keep loose but not too loose
        return True

    def _normalize_source_type(self, source_type: str, source_id: str) -> str:
        st = str(source_type or "").strip().lower()
        sid = str(source_id or "").strip()
        if st not in ALLOWED_SOURCE_TYPES:
            if self._looks_like_file_id(sid):
                return "file_id"
            raise RuntimeError("unsupported_source_type")
        if st == "telegram":
            # Telegram-only mode: treat plain "telegram" as file_id if it looks like one
            if self._looks_like_file_id(sid):
                return "file_id"
            return "telegram_file_id"
        return st

    def _detect_video(self, source_type: str, file_name: str) -> bool:
        st = str(source_type or "").strip().lower()
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

    async def _download_telegram_file(self, file_id: str, chat_id: int, source_type: str, title: str = "") -> tuple[str, dict[str, Any]]:
        if not self.bot_token:
            raise RuntimeError("missing_bot_token")

        async with httpx.AsyncClient(timeout=120) as client:
            info = await client.get(
                f"https://api.telegram.org/bot{self.bot_token}/getFile",
                params={"file_id": file_id},
            )
            info.raise_for_status()
            data = info.json()
            if not data.get("ok"):
                raise RuntimeError(f"telegram_getFile_failed:{data}")

            file_path = data["result"]["file_path"]
            file_name = Path(file_path).name or (title.strip() or file_id)
            video = self._detect_video(source_type, file_name)
            ext = Path(file_path).suffix.lower() or (".mp4" if video else ".ogg")

            safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", file_id)[:120]
            local_path = self._download_dir / f"{chat_id}_{safe_name}{ext}"

            file_bytes = await client.get(
                f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
            )
            file_bytes.raise_for_status()
            local_path.write_bytes(file_bytes.content)

            meta = {
                "title": title or file_name,
                "duration": 0,
                "video": video,
                "file_name": file_name,
                "telegram_path": file_path,
            }
            return str(local_path), meta

    async def _invoke(self, name: str, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self.calls, name, None)
        if not fn:
            raise RuntimeError(f"missing_method:{name}")
        res = fn(*args, **kwargs)
        return await res if asyncio.iscoroutine(res) else res

    async def _play_media(self, chat_id: int, media: str) -> None:
        # Match the old player's behavior: positional first, keyword second.
        try:
            await self._invoke("play", int(chat_id), media)
            return
        except TypeError:
            await self._invoke("play", chat_id=int(chat_id), media=media)

    async def _pause_backend(self, chat_id: int) -> bool:
        if self.calls is None:
            return False
        for name in ("pause", "pause_stream"):
            try:
                await self._invoke(name, int(chat_id))
                return True
            except TypeError:
                try:
                    await self._invoke(name)
                    return True
                except Exception:
                    pass
            except Exception as e:
                logger.debug("pause backend failed on %s: %s", name, e)
        return False

    async def _resume_backend(self, chat_id: int) -> bool:
        if self.calls is None:
            return False
        for name in ("resume", "resume_stream"):
            try:
                await self._invoke(name, int(chat_id))
                return True
            except TypeError:
                try:
                    await self._invoke(name)
                    return True
                except Exception:
                    pass
            except Exception as e:
                logger.debug("resume backend failed on %s: %s", name, e)
        return False

    async def _seek_backend(self, chat_id: int, delta: int) -> bool:
        if self.calls is None:
            return False
        for name in ("seek", "shift", "move"):
            try:
                await self._invoke(name, int(chat_id), int(delta))
                return True
            except TypeError:
                try:
                    await self._invoke(name, int(delta))
                    return True
                except Exception:
                    pass
            except Exception as e:
                logger.debug("seek backend failed on %s: %s", name, e)
        return False

    async def _stop_backend(self, chat_id: int) -> tuple[bool, list[str]]:
        """
        Old working semantics:
        1) Try raw Telethon LeaveGroupCallRequest using full_chat.call
        2) Fall back to backend stop/leave variants
        3) Disconnect client as final fallback
        """
        errors: list[str] = []
        done = False

        if self.client is not None:
            try:
                entity = await self.client.get_entity(chat_id)
                full = await self.client(functions.channels.GetFullChannelRequest(channel=entity))
                call = getattr(getattr(full, "full_chat", None), "call", None)
                if call:
                    try:
                        res = self.client(functions.phone.LeaveGroupCallRequest(call=call, source=0))
                    except TypeError:
                        res = self.client(functions.phone.LeaveGroupCallRequest(call=call))
                    if asyncio.iscoroutine(res):
                        await res
                    done = True
            except Exception as e:
                errors.append(f"raw_leave:{e}")

        if not done and self.calls is not None:
            targets = (
                self.calls,
                getattr(self.calls, "group_call", None),
                getattr(self.calls, "mtproto", None),
                getattr(self.calls, "_group_call", None),
                getattr(self.calls, "_call", None),
            )
            for obj in targets:
                if not obj:
                    continue
                for name in ("stop", "leave_current_group_call", "leave_group_call", "hangup", "close"):
                    fn = getattr(obj, name, None)
                    if not callable(fn):
                        continue
                    for args in ((int(chat_id),), ()):
                        try:
                            res = fn(*args)
                            if asyncio.iscoroutine(res):
                                await res
                            done = True
                            break
                        except TypeError:
                            continue
                        except Exception as e:
                            errors.append(f"{name}:{e}")
                            break
                    if done:
                        break
                if done:
                    break

        if not done and self.client is not None:
            try:
                await self.client.disconnect()
                done = True
            except Exception as e:
                errors.append(f"disconnect:{e}")

        return done, errors

    async def _cleanup_temp(self, s: SessionState) -> None:
        if s.temp_path:
            try:
                Path(s.temp_path).unlink(missing_ok=True)
            except Exception:
                pass
            s.temp_path = ""
        if s.current and s.current.local_path:
            try:
                Path(s.current.local_path).unlink(missing_ok=True)
            except Exception:
                pass

    def _state(self, chat_id: int) -> dict[str, Any]:
        s = self._sess(chat_id)
        return {
            "ok": True,
            "chat_id": chat_id,
            "ready": self.ready,
            "backend_error": self.backend_error,
            "session": {
                "chat_id": s.chat_id,
                "status": s.status,
                "paused": s.paused,
                "last_error": s.last_error,
                "started_at": s.started_at,
                "pause_started_at": s.pause_started_at,
                "paused_seconds": s.paused_seconds,
                "last_update_at": s.last_update_at,
                "current": None if not s.current else {
                    "id": s.current.id,
                    "source_type": s.current.source_type,
                    "source_id": s.current.source_id,
                    "title": s.current.title,
                    "duration": s.current.duration,
                    "local_path": s.current.local_path,
                    "original_path": s.current.original_path,
                    "offset": s.current.offset,
                    "video": s.current.video,
                    "requested_by": s.current.requested_by,
                    "created_at": s.current.created_at,
                },
                "queue": [
                    {
                        "id": t.id,
                        "source_type": t.source_type,
                        "source_id": t.source_id,
                        "title": t.title,
                        "duration": t.duration,
                        "local_path": t.local_path,
                        "original_path": t.original_path,
                        "offset": t.offset,
                        "video": t.video,
                        "requested_by": t.requested_by,
                        "created_at": t.created_at,
                    } for t in s.queue
                ],
            },
        }

    # -------------------- lifecycle --------------------
    async def boot(self) -> None:
        if self.ready and self.calls_started:
            return

        async with self.boot_lock:
            if self.ready and self.calls_started:
                return

            if not self.api_id or not self.api_hash or not self.session_string:
                self.ready = False
                self.backend_error = "missing_env: API_ID/API_HASH/SESSION_STRING"
                return

            self.client = self.client or TelegramClient(StringSession(self.session_string), self.api_id, self.api_hash)
            try:
                if not self.client.is_connected():
                    await self.client.start()

                if self.calls is None:
                    if PyTgCalls is None:
                        raise RuntimeError("pytgcalls_import_failed")
                    self.calls = PyTgCalls(self.client)

                res = self.calls.start()
                if asyncio.iscoroutine(res):
                    await res

                self.calls_started = True
                self.ready = True
                self.backend_error = ""
            except Exception as e:
                self.ready = False
                self.backend_error = f"{type(e).__name__}: {e}"
                raise

    async def ensure_ready(self) -> None:
        if not self.ready or not self.calls_started:
            await self.boot()

    async def close(self) -> None:
        try:
            for s in self.sessions.values():
                await self._cleanup_temp(s)
        finally:
            try:
                if self.client is not None:
                    try:
                        if self.calls is not None:
                            stop = getattr(self.calls, "stop", None)
                            if callable(stop):
                                res = stop()
                                if asyncio.iscoroutine(res):
                                    await res
                    except Exception:
                        pass
                    await self.client.disconnect()
            except Exception:
                pass

    # -------------------- public API --------------------
    async def meta(self, chat_id: int, source_type: str, source_id: str, title: str = "", duration: int = 0) -> dict[str, Any]:
        source_type = self._normalize_source_type(source_type, source_id)
        s = self._sess(int(chat_id))
        s.current = s.current or Track(
            id=uuid.uuid4().hex,
            chat_id=int(chat_id),
            source_type=source_type,
            source_id=str(source_id),
            title=str(title or ""),
            duration=int(duration or 0),
        )
        s.current.source_type = source_type
        s.current.source_id = str(source_id)
        s.current.title = str(title or "")
        s.current.duration = int(duration or 0)
        s.last_update_at = time.time()
        return {"ok": True, "action": "meta", **self._state(chat_id)}

    async def start(self, chat_id: int, source_type: str, source_id: str, title: str = "", duration: int = 0, offset: int = 0) -> dict[str, Any]:
        await self.ensure_ready()

        chat_id = int(chat_id)
        source_type = self._normalize_source_type(source_type, source_id)
        source_id = str(source_id or "").strip()
        title = str(title or "").strip()
        duration = int(duration or 0)
        offset = max(0, int(offset or 0))

        if not source_id:
            raise RuntimeError("empty_source")

        if self._is_url(source_id):
            # Telegram-only mode: refuse remote URLs here.
            raise RuntimeError("urls_not_supported_in_this_service")

        async with self.lock:
            s = self._sess(chat_id)

            # preserve queue support; start should stop existing session cleanly first
            await self._stop_backend(chat_id)
            await self._cleanup_temp(s)

            # only Telegram file_id/media
            if source_type in {"file_id", "telegram_file_id", "telegram_audio", "telegram_video"} or self._looks_like_file_id(source_id):
                local_path, meta = await self._download_telegram_file(source_id, chat_id, source_type, title)
                video = bool(meta.get("video", False))
                resolved_title = title or str(meta.get("title") or source_id)
                resolved_duration = int(duration or meta.get("duration") or 0)

                track = Track(
                    id=uuid.uuid4().hex,
                    chat_id=chat_id,
                    source_type=source_type,
                    source_id=source_id,
                    title=resolved_title,
                    duration=resolved_duration,
                    local_path=local_path,
                    original_path=local_path,
                    offset=offset,
                    video=video,
                )
                s.current = track
                s.status = "starting"
                s.paused = False
                s.last_error = ""
                s.started_at = time.time()
                s.pause_started_at = 0.0
                s.paused_seconds = 0.0
                s.last_update_at = time.time()

                # old player: start backend using the local media file
                last_error: Exception | None = None
                for _ in range(2):
                    try:
                        print("before_play", chat_id, local_path)
                        await self._play_media(chat_id, local_path)
                        print("after_play", chat_id)
                        s.status = "playing"
                        s.last_update_at = time.time()
                        return {"ok": True, "action": "start", **self._state(chat_id)}
                    except Exception as e:
                        last_error = e
                        msg = str(e).lower()
                        print("play_error", chat_id, msg)
                        if "already running" in msg:
                            self.calls_started = True
                            self.ready = True
                            continue
                        if "no method" in msg or "missing_method" in msg or "method_not_supported" in msg:
                            break
                        await asyncio.sleep(1)

                s.status = "error"
                s.last_error = f"{type(last_error).__name__}: {last_error}"
                return {"ok": False, "error": f"play_failed:{last_error}", **self._state(chat_id)}

            raise RuntimeError("unsupported_source_type")

    async def pause(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        chat_id = int(chat_id)

        async with self.lock:
            s = self._sess(chat_id)
            if s.status != "playing":
                return {"ok": True, "action": "pause", **self._state(chat_id)}

            try:
                await self._pause_backend(chat_id)
            except Exception as e:
                print("pause_error", chat_id, str(e))

            s.status = "paused"
            s.paused = True
            s.pause_started_at = time.time()
            s.last_update_at = time.time()
            return {"ok": True, "action": "pause", **self._state(chat_id)}

    async def resume(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        chat_id = int(chat_id)

        async with self.lock:
            s = self._sess(chat_id)
            try:
                await self._resume_backend(chat_id)
            except Exception as e:
                print("resume_error", chat_id, str(e))
            if s.paused and s.pause_started_at:
                s.paused_seconds += max(0.0, time.time() - s.pause_started_at)
                s.pause_started_at = 0.0
            s.status = "playing"
            s.paused = False
            s.last_update_at = time.time()
            return {"ok": True, "action": "resume", **self._state(chat_id)}

    async def seek(self, chat_id: int, delta: int = 0) -> dict[str, Any]:
        await self.ensure_ready()
        chat_id = int(chat_id)
        delta = int(delta or 0)

        async with self.lock:
            ok = await self._seek_backend(chat_id, delta)
            s = self._sess(chat_id)
            if s.current:
                s.current.offset = max(0, int(s.current.offset or 0) + delta)
            s.last_update_at = time.time()
            return {"ok": True, "action": "seek", "moved": ok, "delta": delta, **self._state(chat_id)}

    async def stop(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        chat_id = int(chat_id)

        async with self.lock:
            s = self._sess(chat_id)
            ok, errors = await self._stop_backend(chat_id)

            await self._cleanup_temp(s)
            s.gc = None
            s.current = None
            s.queue.clear()
            s.status = "idle"
            s.paused = False
            s.started_at = 0.0
            s.pause_started_at = 0.0
            s.paused_seconds = 0.0
            s.last_update_at = time.time()
            s.last_error = "" if ok else (errors[-1] if errors else "method_not_supported")

            if not ok:
                return {"ok": False, "error": f"backend_stop_failed: {s.last_error or 'method_not_supported'}", **self._state(chat_id)}

            return {"ok": True, "action": "stop", **self._state(chat_id)}

    async def enqueue(self, chat_id: int, source_type: str, source_id: str, title: str = "", duration: int = 0, requested_by: str = "", auto_start: bool = True) -> dict[str, Any]:
        await self.ensure_ready()
        chat_id = int(chat_id)
        source_type = self._normalize_source_type(source_type, source_id)
        source_id = str(source_id or "").strip()

        async with self.lock:
            s = self._sess(chat_id)
            t = Track(
                id=uuid.uuid4().hex,
                chat_id=chat_id,
                source_type=source_type,
                source_id=source_id,
                title=str(title or ""),
                duration=int(duration or 0),
                requested_by=str(requested_by or ""),
                video=source_type == "telegram_video",
            )
            s.queue.append(t)
            s.last_update_at = time.time()

            if auto_start and s.status in {"idle", "stopped", "error"}:
                nxt = s.queue.pop(0)
                result = await self.start(chat_id, nxt.source_type, nxt.source_id, title=nxt.title, duration=nxt.duration, offset=nxt.offset)
                return {"ok": True, "action": "enqueue", "auto_started": True, "result": result, **self._state(chat_id)}

            return {"ok": True, "action": "enqueue", **self._state(chat_id)}

    async def queue_list(self, chat_id: int) -> dict[str, Any]:
        chat_id = int(chat_id)
        s = self._sess(chat_id)
        return {
            "ok": True,
            "action": "queue_list",
            "queue": [
                {
                    "id": t.id,
                    "source_type": t.source_type,
                    "source_id": t.source_id,
                    "title": t.title,
                    "duration": t.duration,
                    "requested_by": t.requested_by,
                    "offset": t.offset,
                    "video": t.video,
                }
                for t in s.queue
            ],
            **self._state(chat_id),
        }

    async def queue_clear(self, chat_id: int) -> dict[str, Any]:
        chat_id = int(chat_id)
        async with self.lock:
            s = self._sess(chat_id)
            s.queue.clear()
            s.last_update_at = time.time()
            return {"ok": True, "action": "queue_clear", **self._state(chat_id)}

    async def skip(self, chat_id: int) -> dict[str, Any]:
        chat_id = int(chat_id)
        async with self.lock:
            s = self._sess(chat_id)
            if not s.queue:
                return await self.stop(chat_id)

            nxt = s.queue.pop(0)
            # stop current then start next
            await self._stop_backend(chat_id)
            await self._cleanup_temp(s)
            s.status = "starting"
            s.current = None
            s.last_update_at = time.time()

            return await self.start(chat_id, nxt.source_type, nxt.source_id, title=nxt.title, duration=nxt.duration, offset=nxt.offset)


service = AudioService()