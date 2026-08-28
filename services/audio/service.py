from __future__ import annotations

import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Any

from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError
from telethon.sessions import StringSession

from call.manager import CallManager
from config import API_HASH, API_ID, BOT_TOKEN, SESSION_STRING
from errors import AudioServiceError
from media.telegram import TelegramMedia
from media.url import UrlResolver
from state.models import AudioSession

log = logging.getLogger("audio_service")


class AudioService:
    def __init__(self):
        self.ready = False
        self.backend_error = ""
        self.client: TelegramClient | None = None
        self.calls: CallManager | None = None
        self.telegram_media: TelegramMedia | None = None
        self.urls = UrlResolver()
        self.sessions: dict[int, AudioSession] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.url_locks: dict[str, asyncio.Lock] = {}
        self.ready_lock = asyncio.Lock()
        self.root = Path(tempfile.gettempdir()) / "render_audio_media"
        self.root.mkdir(parents=True, exist_ok=True)
        self._clean_all()

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(int(chat_id), asyncio.Lock())

    def _url_lock(self, source: str) -> asyncio.Lock:
        key = str(source or "").strip()
        return self.url_locks.setdefault(key, asyncio.Lock())

    def _now(self) -> float:
        return time.time()

    async def ensure_ready(self):
        if self.ready:
            return
        async with self.ready_lock:
            if self.ready:
                return

            self._clean_all()

            if not API_ID or not API_HASH or not SESSION_STRING:
                self.backend_error = "missing_env: API_ID/API_HASH/AUDIO_SESSION_STRING"
                raise RuntimeError(self.backend_error)

            client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    self.backend_error = "session_not_authorized"
                    raise RuntimeError(self.backend_error)

                calls = CallManager(client)
                await calls.start()
                media = TelegramMedia(BOT_TOKEN, client, self.root)
            except AuthKeyDuplicatedError as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self.backend_error = "auth_key_duplicated"
                raise AudioServiceError("auth_key_duplicated", str(e)) from e
            except Exception as e:
                try:
                    await client.disconnect()
                except Exception:
                    pass
                self.backend_error = f"{type(e).__name__}: {e}"
                raise

            self.client = client
            self.calls = calls
            self.telegram_media = media
            self.ready = True
            self.backend_error = ""
            log.info("ready")

    async def close(self):
        for chat_id in list(self.sessions):
            try:
                if self.calls:
                    await self.calls.stop(chat_id)
            except Exception:
                pass

            session = self.sessions.pop(chat_id, None)
            if session:
                self._clean_file(session.local_path)

        if self.client:
            try:
                await self.client.disconnect()
            except Exception:
                pass

        self.client = None
        self.calls = None
        self.telegram_media = None
        self.ready = False
        self._clean_all()
        self.url_locks.clear()

    def _clean_all(self):
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            for path in self.root.iterdir():
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
                elif path.is_dir():
                    for child in path.rglob("*"):
                        if child.is_file() or child.is_symlink():
                            child.unlink(missing_ok=True)
                    for child in sorted(path.rglob("*"), reverse=True):
                        if child.is_dir():
                            child.rmdir()
                    path.rmdir()
        except Exception:
            pass
        try:
            import tempfile
            for path in Path(tempfile.gettempdir()).glob("youtube_cookies_*.txt"):
                path.unlink(missing_ok=True)
        except Exception:
            pass

    def _clean_file(self, path: str):
        if not path:
            return
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass

    def state(self, chat_id: int) -> dict[str, Any]:
        session = self.sessions.get(int(chat_id))
        if not session:
            return {
                "ok": True,
                "ready": self.ready,
                "active": False,
                "state": {"chat_id": int(chat_id), "status": "idle"},
            }

        state = session.to_dict()
        if session.status == "playing":
            position = max(0, int(self._now() - session.started_at))
            if session.duration > 0:
                position = min(position, session.duration)
            state["position"] = position
        elif session.status == "paused":
            state["position"] = max(0, int(session.position))

        state["updated_at"] = self._now()
        return {
            "ok": True,
            "ready": self.ready,
            "active": session.status in {"playing", "paused"},
            "state": state,
        }

    async def call_state(self, chat_id: int) -> dict[str, Any]:
        await self.ensure_ready()
        if not self.calls:
            raise RuntimeError("call_backend_not_ready")
        try:
            active = await self.calls.active(int(chat_id))
        except Exception as e:
            message = str(e)
            if "GROUPCALL_INVALID" in message or "NoActiveGroupCall" in message or "No active group call" in message:
                active = False
            else:
                raise
        return {"ok": True, "chat_id": int(chat_id), "active": bool(active)}

    async def _resolve_url(self, source_id: str) -> dict[str, Any]:
        source = str(source_id or "").strip()
        if not source:
            raise RuntimeError("url_missing")
        async with self._url_lock(source):
            return await self.urls.resolve(source)

    async def _telegram_message_metadata(
        self,
        chat_id: int,
        message_id: int,
        title: str = "",
        duration: int = 0,
    ) -> dict[str, Any]:
        result_title = str(title or "").strip()
        result_duration = max(0, int(duration or 0))
        video = False
        kind = "audio"

        if self.client and chat_id and message_id:
            message = await self.client.get_messages(int(chat_id), ids=int(message_id))
            if message and message.media:
                media = message.media
                document = getattr(media, "document", None)
                attributes = list(getattr(document, "attributes", []) or []) if document else []
                file_name = ""
                for attribute in attributes:
                    candidate = getattr(attribute, "file_name", "")
                    if candidate:
                        file_name = str(candidate)
                        break
                    if hasattr(attribute, "duration") and not result_duration:
                        result_duration = max(0, int(getattr(attribute, "duration", 0) or 0))
                mime = str(getattr(document, "mime_type", "") or "") if document else ""
                ext = Path(file_name).suffix.lower()
                video = mime.startswith("video/") or ext in {".mp4", ".mkv", ".mov", ".webm", ".m4v", ".avi"}
                kind = "video" if video else "audio"
                if not result_title:
                    result_title = Path(file_name).stem.strip() if file_name else ""

        return {
            "source_type": "telegram_message",
            "source_id": f"{int(message_id)}",
            "stream_url": "",
            "title": result_title or "غير معروف",
            "duration": result_duration,
            "webpage_url": "",
            "thumbnail": "",
            "video": video,
            "media_kind": kind,
            "local_path": "",
            "live": False,
        }

    async def _resolve(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        title: str = "",
        duration: int = 0,
        source_chat_id: int = 0,
        source_message_id: int = 0,
        metadata_only: bool = False,
    ) -> dict[str, Any]:
        st = str(source_type or "").strip().lower().replace("-", "_")

        if st in {"url", "link", "youtube", "yt"}:
            result = await self._resolve_url(source_id)
            result.update(source_type="url", source_id=source_id)
            return result

        if st == "telegram_message":
            if metadata_only:
                return await self._telegram_message_metadata(
                    source_chat_id or chat_id,
                    source_message_id,
                    title,
                    duration,
                )
            if not self.telegram_media:
                raise RuntimeError("telegram_media_not_ready")
            path, video, kind = await self.telegram_media.from_message(
                source_chat_id or chat_id,
                source_message_id,
                title,
            )
            return {
                "source_type": st,
                "source_id": source_id,
                "stream_url": str(path),
                "title": title or path.stem,
                "duration": duration,
                "webpage_url": "",
                "thumbnail": "",
                "video": video,
                "media_kind": kind,
                "local_path": str(path),
                "live": False,
            }

        if st in {"telegram_audio", "telegram_video", "telegram_file_id", "file_id"}:
            if metadata_only:
                return {
                    "source_type": st,
                    "source_id": source_id,
                    "stream_url": "",
                    "title": title or "غير معروف",
                    "duration": max(0, int(duration or 0)),
                    "webpage_url": "",
                    "thumbnail": "",
                    "video": st == "telegram_video",
                    "media_kind": "video" if st == "telegram_video" else "audio",
                    "local_path": "",
                    "live": False,
                }
            if not self.telegram_media:
                raise RuntimeError("telegram_media_not_ready")
            path, video, kind = await self.telegram_media.from_file_id(source_id, st, title)
            return {
                "source_type": st,
                "source_id": source_id,
                "stream_url": str(path),
                "title": title or path.stem,
                "duration": duration,
                "webpage_url": "",
                "thumbnail": "",
                "video": video,
                "media_kind": kind,
                "local_path": str(path),
                "live": False,
            }

        raise RuntimeError(f"unsupported_source_type: {source_type}")

    async def meta(self, chat_id: int, source_type: str, source_id: str, **kw) -> dict[str, Any]:
        await self.ensure_ready()
        async with self.lock(chat_id):
            result = await self._resolve(
                chat_id,
                source_type,
                source_id,
                title=str(kw.get("title") or ""),
                duration=int(kw.get("duration") or 0),
                source_chat_id=int(kw.get("source_chat_id") or 0),
                source_message_id=int(kw.get("source_message_id") or 0),
                metadata_only=True,
            )
            return {
                "ok": True,
                "action": "meta",
                "state": {
                    "chat_id": chat_id,
                    "source_type": str(result.get("source_type") or source_type),
                    "source_id": str(result.get("source_id") or source_id),
                    "title": str(result.get("title") or kw.get("title") or "غير معروف"),
                    "duration": int(result.get("duration") or kw.get("duration") or 0),
                    "video": bool(result.get("video")),
                    "media_kind": str(result.get("media_kind") or ("video" if result.get("video") else "audio")),
                    "webpage_url": str(result.get("webpage_url") or result.get("source_url") or source_id),
                    "source_url": str(result.get("source_url") or source_id),
                    "thumbnail": str(result.get("thumbnail") or ""),
                    "live": bool(result.get("live", False)),
                },
            }

    async def _start_locked(
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
        await self.ensure_ready()
        if not self.calls:
            raise RuntimeError("call_backend_not_ready")
        if not await self.calls.active(chat_id):
            raise AudioServiceError("no_active_call", "no_active_call")

        result = await self._resolve(
            chat_id,
            source_type,
            source_id,
            title,
            duration,
            source_chat_id,
            source_message_id,
            metadata_only=False,
        )
        new_path = str(result.get("local_path") or "")
        old = self.sessions.get(chat_id)
        if old and old.local_path and old.local_path != new_path:
            self._clean_file(old.local_path)

        stream = str(result.get("stream_url") or "")
        if not stream:
            self._clean_file(new_path)
            raise RuntimeError("stream_url_missing")

        try:
            await self.calls.play(chat_id,stream,bool(result.get("video")),int(offset or 0))
        except AuthKeyDuplicatedError as e:
            self._clean_file(new_path)
            self.backend_error = "auth_key_duplicated"
            raise AudioServiceError("auth_key_duplicated", str(e)) from e
        except Exception as e:
            message = str(e)
            is_url = str(source_type or "").strip().lower() in {"url","link","youtube","yt"}
            if is_url and not any(x in message for x in ("NoActiveGroupCall","No active group call","GROUPCALL_INVALID","AuthKeyDuplicated")):
                try:
                    self.urls.invalidate(source_id)
                    fresh = await self._resolve_url(source_id)
                    fresh_stream = str(fresh.get("stream_url") or "")
                    if not fresh_stream:raise RuntimeError("stream_url_missing")
                    await self.calls.play(chat_id,fresh_stream,bool(fresh.get("video")),int(offset or 0))
                    result = fresh
                except AuthKeyDuplicatedError as retry_error:
                    self._clean_file(new_path)
                    self.backend_error = "auth_key_duplicated"
                    raise AudioServiceError("auth_key_duplicated", str(retry_error)) from retry_error
                except Exception as retry_error:
                    self._clean_file(new_path)
                    retry_message = str(retry_error)
                    if "NoActiveGroupCall" in retry_message or "No active group call" in retry_message or "GROUPCALL_INVALID" in retry_message:
                        raise AudioServiceError("no_active_call", "no_active_call") from retry_error
                    raise
            else:
                self._clean_file(new_path)
                if "NoActiveGroupCall" in message or "No active group call" in message or "GROUPCALL_INVALID" in message:
                    raise AudioServiceError("no_active_call", "no_active_call") from e
                raise

        now = self._now()
        safe_offset = max(0, int(offset or 0))
        session = AudioSession(
            chat_id,
            status="playing",
            title=str(result.get("title") or title or source_id),
            source_type=str(result.get("source_type") or source_type),
            source_id=str(result.get("source_id") or source_id),
            source_chat_id=str(source_chat_id or ""),
            source_message_id=str(source_message_id or ""),
            source_url=str(result.get("source_url") or source_id if str(source_type) == "url" else ""),
            duration=int(result.get("duration") or duration or 0),
            position=safe_offset,
            started_at=now - safe_offset,
            video=bool(result.get("video")),
            media_kind=str(result.get("media_kind") or ("video" if result.get("video") else "audio")),
            live=bool(result.get("live", False)),
            thumbnail=str(result.get("thumbnail") or ""),
            webpage_url=str(result.get("webpage_url") or source_id),
            local_path=new_path,
            updated_at=now,
        )
        self.sessions[chat_id] = session
        return {"ok": True, "action": "start", "state": session.to_dict()}

    async def start(self, chat_id: int, source_type: str, source_id: str, **kw):
        async with self.lock(chat_id):
            return await self._start_locked(chat_id, source_type, source_id, **kw)

    async def stop(self, chat_id: int):
        await self.ensure_ready()
        async with self.lock(chat_id):
            session = self.sessions.get(chat_id)
            try:
                if self.calls:
                    await self.calls.stop(chat_id)
            except Exception as e:
                message = str(e)
                if not any(x in message for x in ("NoActiveGroupCall", "No active group call", "GROUPCALL_INVALID", "NotInCallError")):
                    raise
            finally:
                if session:
                    self._clean_file(session.local_path)
                self.sessions.pop(chat_id, None)
            return {"ok": True, "action": "stop", "state": self.state(chat_id)}

    async def pause(self, chat_id: int):
        await self.ensure_ready()
        async with self.lock(chat_id):
            session = self.sessions.get(chat_id)
            if not session or session.status != "playing":
                return {"ok": False, "action": "pause", "error": "no_active_audio", "state": self.state(chat_id)}
            await self.calls.pause(chat_id)
            now = self._now()
            session.position = max(0, int(now - session.started_at))
            session.status = "paused"
            session.paused_at = int(now)
            session.updated_at = now
            return {"ok": True, "action": "pause", "state": session.to_dict()}

    async def resume(self, chat_id: int):
        await self.ensure_ready()
        async with self.lock(chat_id):
            session = self.sessions.get(chat_id)
            if not session or session.status != "paused":
                return {"ok": False, "action": "resume", "error": "not_paused", "state": self.state(chat_id)}
            await self.calls.resume(chat_id)
            now = self._now()
            session.started_at = now - session.position
            session.status = "playing"
            session.paused_at = 0
            session.updated_at = now
            return {"ok": True, "action": "resume", "state": session.to_dict()}

    async def enqueue(self, chat_id, source_type, source_id, **kw):
        return {"ok": True, "action": "enqueue", "state": self.state(chat_id)}

    async def queue_list(self, chat_id):
        return {"ok": True, "action": "queue_list", "queue": [], "state": self.state(chat_id)}

    async def queue_clear(self, chat_id):
        return await self.stop(chat_id)

    async def skip(self, chat_id):
        return {"ok": False, "action": "skip", "error": "worker_owns_playlist", "state": self.state(chat_id)}


service = AudioService()
