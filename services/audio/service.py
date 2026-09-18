from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError
from telethon.sessions import StringSession

from call.manager import CallManager
from config import (
    API_HASH,
    API_ID,
    AUDIO_CACHE_ENABLED,
    BOT_TOKEN,
    SESSION_STRING,
)
from errors import AudioServiceError
from media.cache import R2AudioCache
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
        self.cache = R2AudioCache()

        self.sessions: dict[int, AudioSession] = {}
        self.locks: dict[int, asyncio.Lock] = {}
        self.url_locks: dict[str, asyncio.Lock] = {}
        self.ready_lock = asyncio.Lock()

        self.root = (
            Path(tempfile.gettempdir())
            / "render_audio_media"
        )
        self.root.mkdir(
            parents=True,
            exist_ok=True,
        )

        self._clean_all()

    def youtube_status(self) -> dict[str, Any]:
        return self.urls.cookie_status()

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self.locks.setdefault(
            int(chat_id),
            asyncio.Lock(),
        )

    def _url_lock(self, source: str) -> asyncio.Lock:
        key = str(source or "").strip()
        return self.url_locks.setdefault(
            key,
            asyncio.Lock(),
        )

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
                self.backend_error = (
                    "missing_env: "
                    "API_ID/API_HASH/AUDIO_SESSION_STRING"
                )
                raise RuntimeError(self.backend_error)

            client = TelegramClient(
                StringSession(SESSION_STRING),
                API_ID,
                API_HASH,
            )

            try:
                await client.connect()

                if not await client.is_user_authorized():
                    await client.disconnect()
                    self.backend_error = "session_not_authorized"
                    raise RuntimeError(
                        self.backend_error
                    )

                calls = CallManager(client)
                await calls.start()

                media = TelegramMedia(
                    BOT_TOKEN,
                    client,
                    self.root,
                )

            except AuthKeyDuplicatedError as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass

                self.backend_error = "auth_key_duplicated"

                raise AudioServiceError(
                    "auth_key_duplicated",
                    str(exc),
                ) from exc

            except Exception as exc:
                try:
                    await client.disconnect()
                except Exception:
                    pass

                self.backend_error = (
                    f"{type(exc).__name__}: {exc}"
                )

                raise

            self.client = client
            self.calls = calls
            self.telegram_media = media

            self.ready = True
            self.backend_error = ""

            log.info(
                "ready r2_cache=%s",
                bool(AUDIO_CACHE_ENABLED),
            )

    async def close(self):
        for chat_id in list(self.sessions):
            try:
                if self.calls:
                    await self.calls.stop(chat_id)
            except Exception:
                pass

            session = self.sessions.pop(
                chat_id,
                None,
            )

            if session:
                self._clean_file(
                    session.local_path,
                )

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
            self.root.mkdir(
                parents=True,
                exist_ok=True,
            )

            for path in self.root.iterdir():
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)

                elif path.is_dir():
                    for child in path.rglob("*"):
                        if child.is_file() or child.is_symlink():
                            child.unlink(
                                missing_ok=True,
                            )

                    for child in sorted(
                        path.rglob("*"),
                        reverse=True,
                    ):
                        if child.is_dir():
                            child.rmdir()

                    path.rmdir()

        except Exception:
            pass

        try:
            for path in Path(
                tempfile.gettempdir()
            ).glob("youtube_cookies_*.txt"):
                path.unlink(missing_ok=True)
        except Exception:
            pass

    def _clean_file(self, path: str):
        if not path:
            return

        try:
            Path(path).unlink(
                missing_ok=True,
            )
        except Exception:
            pass

    def _probe_duration(self, path: str) -> int:
        if not path or not shutil.which("ffprobe"):
            return 0

        try:
            result = subprocess.run(
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
                timeout=20,
            )

            if result.returncode != 0:
                return 0

            return max(
                0,
                int(
                    float(
                        (result.stdout or "").strip()
                        or 0
                    )
                ),
            )

        except Exception:
            return 0

    def state(self, chat_id: int) -> dict[str, Any]:
        session = self.sessions.get(
            int(chat_id),
        )

        if not session:
            return {
                "ok": True,
                "ready": self.ready,
                "active": False,
                "state": {
                    "chat_id": int(chat_id),
                    "status": "idle",
                },
            }

        state = session.to_dict()

        if session.status == "playing":
            position = max(
                0,
                int(
                    self._now()
                    - session.started_at
                ),
            )

            if session.duration > 0:
                position = min(
                    position,
                    session.duration,
                )

            state["position"] = position

        elif session.status == "paused":
            state["position"] = max(
                0,
                int(session.position),
            )

        state["updated_at"] = self._now()

        return {
            "ok": True,
            "ready": self.ready,
            "active": session.status
            in {"playing", "paused"},
            "state": state,
        }

    async def call_state(
        self,
        chat_id: int,
    ) -> dict[str, Any]:
        await self.ensure_ready()

        if not self.calls:
            raise RuntimeError(
                "call_backend_not_ready"
            )

        try:
            active = await self.calls.active(
                int(chat_id),
            )

        except Exception as exc:
            message = str(exc)

            if any(
                token in message
                for token in (
                    "GROUPCALL_INVALID",
                    "NoActiveGroupCall",
                    "No active group call",
                )
            ):
                active = False
            else:
                raise

        return {
            "ok": True,
            "chat_id": int(chat_id),
            "active": bool(active),
        }

    async def _resolve_url(
        self,
        source_id: str,
    ) -> dict[str, Any]:
        source = str(
            source_id or "",
        ).strip()

        if not source:
            raise RuntimeError(
                "url_missing",
            )

        async with self._url_lock(source):
            return await self.urls.resolve(
                source,
            )

    async def _telegram_message_metadata(
        self,
        chat_id: int,
        message_id: int,
        title: str = "",
        duration: int = 0,
    ) -> dict[str, Any]:
        result_title = str(
            title or "",
        ).strip()

        result_duration = max(
            0,
            int(duration or 0),
        )

        video = False
        kind = "audio"
        file_name = ""

        if self.client and chat_id and message_id:
            message = await self.client.get_messages(
                int(chat_id),
                ids=int(message_id),
            )

            if message and message.media:
                document = getattr(
                    message.media,
                    "document",
                    None,
                )

                attributes = list(
                    getattr(
                        document,
                        "attributes",
                        [],
                    )
                    or []
                ) if document else []

                for attribute in attributes:
                    candidate = getattr(
                        attribute,
                        "file_name",
                        "",
                    )

                    if candidate:
                        file_name = str(
                            candidate,
                        )
                        break

                    if (
                        hasattr(attribute, "duration")
                        and not result_duration
                    ):
                        result_duration = max(
                            0,
                            int(
                                getattr(
                                    attribute,
                                    "duration",
                                    0,
                                )
                                or 0
                            ),
                        )

                mime = str(
                    getattr(
                        document,
                        "mime_type",
                        "",
                    )
                    or ""
                ) if document else ""

                ext = Path(
                    file_name,
                ).suffix.lower()

                video = (
                    mime.startswith("video/")
                    or ext in {
                        ".mp4",
                        ".mkv",
                        ".mov",
                        ".webm",
                        ".m4v",
                        ".avi",
                    }
                )

                kind = (
                    "video"
                    if video
                    else "audio"
                )

                if not result_title:
                    result_title = (
                        Path(file_name)
                        .stem
                        .strip()
                        if file_name
                        else ""
                    )

        return {
            "source_type": "telegram_message",
            "source_id": str(int(message_id)),
            "stream_url": "",
            "title": (
                result_title
                or "غير معروف"
            ),
            "duration": result_duration,
            "webpage_url": "",
            "thumbnail": "",
            "video": video,
            "media_kind": kind,
            "local_path": "",
            "cache_key": "",
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

        st = str(
            source_type or "",
        ).lower().strip()

        if st in {
            "url",
            "link",
            "youtube",
            "yt",
        }:
            result = await self._resolve_url(
                source_id,
            )

            if metadata_only:
                return result

            # Preserve the old working YouTube behavior: play the extracted
            # remote stream URL directly through PyTgCalls/FFmpeg. This is
            # especially important for web_safari HLS URLs; downloading the
            # .m3u8 text with httpx would not produce a playable media file.
            if result.get("remote_stream"):
                return result

            return await self._materialize_url(
                result,
            )

        if st in {
            "telegram_audio",
            "telegram_video",
            "telegram_file_id",
        }:
            if not self.telegram_media:
                raise RuntimeError(
                    "telegram_media_not_ready",
                )

            cached_key = self.cache.key_for(
                "telegram",
                source_id,
                st,
                suffix=".mp4"
                if st == "telegram_video"
                else ".ogg",
            )

            local_name = (
                f"cache_{abs(hash(cached_key))}"
            )
            local_path = self.root / local_name

            if (
                not metadata_only
                and await self.cache.download(
                    cached_key,
                    local_path,
                )
            ):
                video = st == "telegram_video"

                return {
                    "source_type": st,
                    "source_id": str(
                        source_id,
                    ),
                    "source_url": "",
                    "stream_url": str(
                        local_path,
                    ),
                    "title": (
                        title
                        or "غير معروف"
                    ),
                    "duration": int(
                        duration or 0
                    ),
                    "webpage_url": "",
                    "thumbnail": "",
                    "video": video,
                    "media_kind": (
                        "video"
                        if video
                        else "audio"
                    ),
                    "live": False,
                    "local_path": str(
                        local_path,
                    ),
                    "cache_key": cached_key,
                }

            if metadata_only:
                return {
                    "source_type": st,
                    "source_id": str(
                        source_id,
                    ),
                    "source_url": "",
                    "stream_url": "",
                    "title": (
                        title
                        or "غير معروف"
                    ),
                    "duration": int(
                        duration or 0
                    ),
                    "webpage_url": "",
                    "thumbnail": "",
                    "video": st
                    == "telegram_video",
                    "media_kind": (
                        "video"
                        if st
                        == "telegram_video"
                        else "audio"
                    ),
                    "live": False,
                    "local_path": "",
                    "cache_key": cached_key,
                }

            path, video, kind = (
                await self.telegram_media.from_file_id(
                    str(source_id),
                    st,
                    title=title,
                )
            )

            actual_duration = (
                int(duration or 0)
                or self._probe_duration(
                    str(path),
                )
            )

            await self.cache.upload(
                path,
                cached_key,
            )

            return {
                "source_type": st,
                "source_id": str(
                    source_id,
                ),
                "source_url": "",
                "stream_url": str(path),
                "title": (
                    title
                    or path.stem
                    or "غير معروف"
                ),
                "duration": actual_duration,
                "webpage_url": "",
                "thumbnail": "",
                "video": video,
                "media_kind": kind,
                "live": False,
                "local_path": str(path),
                "cache_key": cached_key,
            }

        if st == "telegram_message":
            meta = (
                await self._telegram_message_metadata(
                    source_chat_id
                    or chat_id,
                    source_message_id
                    or int(source_id or 0),
                    title=title,
                    duration=duration,
                )
            )

            if metadata_only:
                return meta

            if not self.telegram_media:
                raise RuntimeError(
                    "telegram_media_not_ready",
                )

            cache_key = self.cache.key_for(
                "telegram_message",
                source_chat_id
                or chat_id,
                source_message_id
                or int(source_id or 0),
                suffix=".mp4"
                if meta["video"]
                else ".ogg",
            )

            local_path = self.root / (
                f"msg_{abs(hash(cache_key))}"
                + (
                    ".mp4"
                    if meta["video"]
                    else ".ogg"
                )
            )

            if await self.cache.download(
                cache_key,
                local_path,
            ):
                result = dict(meta)
                result["stream_url"] = str(
                    local_path,
                )
                result["local_path"] = str(
                    local_path,
                )
                result["cache_key"] = cache_key

                if not result["duration"]:
                    result["duration"] = (
                        self._probe_duration(
                            str(local_path),
                        )
                    )

                return result

            path, video, kind = (
                await self.telegram_media.from_message(
                    source_chat_id
                    or chat_id,
                    source_message_id
                    or int(source_id or 0),
                    title=title,
                )
            )

            actual_duration = (
                int(meta["duration"] or 0)
                or self._probe_duration(
                    str(path),
                )
            )

            await self.cache.upload(
                path,
                cache_key,
            )

            result = dict(meta)
            result["video"] = video
            result["media_kind"] = kind
            result["duration"] = (
                actual_duration
            )
            result["stream_url"] = str(
                path,
            )
            result["local_path"] = str(
                path,
            )
            result["cache_key"] = cache_key

            return result

        raise RuntimeError(
            f"unsupported_source_type: {source_type}"
        )

    async def _materialize_url(
        self,
        result: dict[str, Any],
        allow_refresh: bool = True,
    ) -> dict[str, Any]:
        stream_url = str(result.get("stream_url") or "").strip()
        if not stream_url:
            raise RuntimeError("stream_url_missing")

        cache_key = self.cache.key_for(
            "url",
            result.get("webpage_url")
            or result.get("source_url")
            or stream_url,
            suffix=".mp4" if result.get("video") else ".ogg",
        )

        local_path = self.root / (
            f"url_{abs(hash(cache_key))}"
            + (".mp4" if result.get("video") else ".ogg")
        )

        if await self.cache.download(cache_key, local_path):
            output = dict(result)
            output["stream_url"] = str(local_path)
            output["local_path"] = str(local_path)
            output["cache_key"] = cache_key
            if not output.get("duration"):
                output["duration"] = self._probe_duration(str(local_path))
            return output

        headers = {"User-Agent": "Mozilla/5.0"}
        for key, value in dict(result.get("http_headers") or {}).items():
            if str(key).lower() in {"user-agent", "referer", "origin", "accept", "accept-language"} and value:
                headers[str(key)] = str(value)

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=30.0),
                follow_redirects=True,
            ) as client:
                async with client.stream(
                    "GET",
                    stream_url,
                    headers=headers,
                ) as response:
                    if response.status_code in {401, 403, 410} and allow_refresh:
                        raise httpx.HTTPStatusError(
                            f"media_http_{response.status_code}",
                            request=response.request,
                            response=response,
                        )
                    response.raise_for_status()
                    with local_path.open("wb") as target:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            if chunk:
                                target.write(chunk)
        except httpx.HTTPStatusError as exc:
            self._clean_file(str(local_path))
            if (
                allow_refresh
                and exc.response is not None
                and exc.response.status_code in {401, 403, 410}
            ):
                source = str(result.get("webpage_url") or result.get("source_url") or "").strip()
                if source:
                    self.urls.invalidate(source)
                    fresh = await self._resolve_url(source)
                    return await self._materialize_url(fresh, allow_refresh=False)
            raise
        except Exception:
            self._clean_file(str(local_path))
            raise

        actual_duration = (
            int(result.get("duration") or 0)
            or self._probe_duration(str(local_path))
        )
        await self.cache.upload(local_path, cache_key)

        output = dict(result)
        output["stream_url"] = str(local_path)
        output["local_path"] = str(local_path)
        output["cache_key"] = cache_key
        output["duration"] = actual_duration
        return output

    async def meta(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        **kw,
    ):
        await self.ensure_ready()

        result = await self._resolve(
            chat_id,
            source_type,
            source_id,
            title=str(
                kw.get("title") or ""
            ),
            duration=int(
                kw.get("duration") or 0
            ),
            source_chat_id=int(
                kw.get("source_chat_id")
                or 0
            ),
            source_message_id=int(
                kw.get("source_message_id")
                or 0
            ),
            metadata_only=True,
        )

        return {
            "ok": True,
            "action": "meta",
            "state": {
                "chat_id": chat_id,
                "source_type": str(
                    result.get(
                        "source_type"
                    )
                    or source_type
                ),
                "source_id": str(
                    result.get(
                        "source_id"
                    )
                    or source_id
                ),
                "title": str(
                    result.get(
                        "title"
                    )
                    or kw.get("title")
                    or "غير معروف"
                ),
                "duration": int(
                    result.get(
                        "duration"
                    )
                    or kw.get("duration")
                    or 0
                ),
                "video": bool(
                    result.get("video")
                ),
                "media_kind": str(
                    result.get(
                        "media_kind"
                    )
                    or (
                        "video"
                        if result.get(
                            "video"
                        )
                        else "audio"
                    )
                ),
                "webpage_url": str(
                    result.get(
                        "webpage_url"
                    )
                    or result.get(
                        "source_url"
                    )
                    or source_id
                ),
                "source_url": str(
                    result.get(
                        "source_url"
                    )
                    or source_id
                ),
                "thumbnail": str(
                    result.get(
                        "thumbnail"
                    )
                    or ""
                ),
                "live": bool(
                    result.get(
                        "live",
                        False,
                    )
                ),
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
    ):
        await self.ensure_ready()

        if not self.calls:
            raise RuntimeError(
                "call_backend_not_ready"
            )

        if not await self.calls.active(
            chat_id,
        ):
            raise AudioServiceError(
                "no_active_call",
                "no_active_call",
            )

        result = await self._resolve(
            chat_id,
            source_type,
            source_id,
            title=title,
            duration=duration,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            metadata_only=False,
        )

        new_path = str(
            result.get("local_path") or ""
        )

        old = self.sessions.get(chat_id)

        if (
            old
            and old.local_path
            and old.local_path != new_path
        ):
            self._clean_file(
                old.local_path,
            )

        stream = str(
            result.get("stream_url") or ""
        )

        if not stream:
            self._clean_file(new_path)
            raise RuntimeError(
                "stream_url_missing"
            )

        safe_offset = max(
            0,
            int(offset or 0),
        )

        try:
            await self.calls.play(
                chat_id,
                stream,
                bool(result.get("video")),
                safe_offset,
            )

        except AuthKeyDuplicatedError as exc:
            self._clean_file(new_path)
            self.backend_error = (
                "auth_key_duplicated"
            )

            raise AudioServiceError(
                "auth_key_duplicated",
                str(exc),
            ) from exc

        except Exception as exc:
            message = str(exc)

            is_url = str(
                source_type or ""
            ).strip().lower() in {
                "url",
                "link",
                "youtube",
                "yt",
            }

            if (
                is_url
                and not any(
                    token in message
                    for token in (
                        "NoActiveGroupCall",
                        "No active group call",
                        "GROUPCALL_INVALID",
                        "AuthKeyDuplicated",
                    )
                )
            ):
                try:
                    self.urls.invalidate(
                        source_id,
                    )

                    fresh = await self._resolve(
                        chat_id,
                        source_type,
                        source_id,
                        title=title,
                        duration=duration,
                        source_chat_id=source_chat_id,
                        source_message_id=source_message_id,
                        metadata_only=False,
                    )

                    fresh_stream = str(
                        fresh.get(
                            "stream_url"
                        )
                        or ""
                    )

                    if not fresh_stream:
                        raise RuntimeError(
                            "stream_url_missing"
                        )

                    await self.calls.play(
                        chat_id,
                        fresh_stream,
                        bool(
                            fresh.get(
                                "video"
                            )
                        ),
                        safe_offset,
                    )

                    result = fresh

                except AuthKeyDuplicatedError as retry_error:
                    self._clean_file(new_path)
                    self.backend_error = (
                        "auth_key_duplicated"
                    )

                    raise AudioServiceError(
                        "auth_key_duplicated",
                        str(retry_error),
                    ) from retry_error

                except Exception as retry_error:
                    self._clean_file(new_path)

                    retry_message = str(
                        retry_error
                    )

                    if any(
                        token in retry_message
                        for token in (
                            "NoActiveGroupCall",
                            "No active group call",
                            "GROUPCALL_INVALID",
                        )
                    ):
                        raise AudioServiceError(
                            "no_active_call",
                            "no_active_call",
                        ) from retry_error

                    raise

            else:
                self._clean_file(new_path)

                if any(
                    token in message
                    for token in (
                        "NoActiveGroupCall",
                        "No active group call",
                        "GROUPCALL_INVALID",
                    )
                ):
                    raise AudioServiceError(
                        "no_active_call",
                        "no_active_call",
                    ) from exc

                raise

        now = self._now()

        session = AudioSession(
            chat_id,
            status="playing",
            title=str(
                result.get("title")
                or title
                or source_id
            ),
            source_type=str(
                result.get(
                    "source_type"
                )
                or source_type
            ),
            source_id=str(
                result.get(
                    "source_id"
                )
                or source_id
            ),
            source_chat_id=str(
                source_chat_id or ""
            ),
            source_message_id=str(
                source_message_id or ""
            ),
            source_url=(
                str(
                    result.get(
                        "source_url"
                    )
                    or source_id
                )
                if str(
                    source_type
                ).lower()
                in {"url", "link", "youtube", "yt"}
                else ""
            ),
            duration=int(
                result.get(
                    "duration"
                )
                or duration
                or 0
            ),
            position=safe_offset,
            started_at=(
                now - safe_offset
            ),
            video=bool(
                result.get("video")
            ),
            media_kind=str(
                result.get(
                    "media_kind"
                )
                or (
                    "video"
                    if result.get("video")
                    else "audio"
                )
            ),
            live=bool(
                result.get(
                    "live",
                    False,
                )
            ),
            thumbnail=str(
                result.get(
                    "thumbnail"
                )
                or ""
            ),
            webpage_url=str(
                result.get(
                    "webpage_url"
                )
                or source_id
            ),
            local_path=new_path,
            updated_at=now,
        )

        self.sessions[chat_id] = session

        return {
            "ok": True,
            "action": "start",
            "state": session.to_dict(),
        }

    async def start(
        self,
        chat_id: int,
        source_type: str,
        source_id: str,
        **kw,
    ):
        async with self.lock(chat_id):
            return await self._start_locked(
                chat_id,
                source_type,
                source_id,
                **kw,
            )

    async def stop(
        self,
        chat_id: int,
    ):
        await self.ensure_ready()

        async with self.lock(chat_id):
            session = self.sessions.get(chat_id)

            try:
                if self.calls:
                    await self.calls.stop(
                        chat_id,
                    )
            except Exception as exc:
                message = str(exc)

                if not any(
                    token in message
                    for token in (
                        "NoActiveGroupCall",
                        "No active group call",
                        "GROUPCALL_INVALID",
                        "NotInCallError",
                    )
                ):
                    raise

            finally:
                if session:
                    self._clean_file(
                        session.local_path,
                    )

                self.sessions.pop(
                    chat_id,
                    None,
                )

            return {
                "ok": True,
                "action": "stop",
                "state": self.state(
                    chat_id,
                ),
            }

    async def pause(
        self,
        chat_id: int,
    ):
        await self.ensure_ready()

        async with self.lock(chat_id):
            session = self.sessions.get(
                chat_id,
            )

            if (
                not session
                or session.status != "playing"
            ):
                return {
                    "ok": False,
                    "action": "pause",
                    "error": "no_active_audio",
                    "state": self.state(
                        chat_id,
                    ),
                }

            await self.calls.pause(
                chat_id,
            )

            now = self._now()

            session.position = max(
                0,
                int(
                    now - session.started_at
                ),
            )
            session.status = "paused"
            session.paused_at = int(now)
            session.updated_at = now

            return {
                "ok": True,
                "action": "pause",
                "state": session.to_dict(),
            }

    async def resume(
        self,
        chat_id: int,
    ):
        await self.ensure_ready()

        async with self.lock(chat_id):
            session = self.sessions.get(
                chat_id,
            )

            if (
                not session
                or session.status != "paused"
            ):
                return {
                    "ok": False,
                    "action": "resume",
                    "error": "not_paused",
                    "state": self.state(
                        chat_id,
                    ),
                }

            await self.calls.resume(
                chat_id,
            )

            now = self._now()

            session.started_at = (
                now - session.position
            )
            session.status = "playing"
            session.paused_at = 0
            session.updated_at = now

            return {
                "ok": True,
                "action": "resume",
                "state": session.to_dict(),
            }

    async def enqueue(
        self,
        chat_id,
        source_type,
        source_id,
        **kw,
    ):
        return {
            "ok": True,
            "action": "enqueue",
            "state": self.state(
                chat_id,
            ),
        }

    async def queue_list(
        self,
        chat_id,
    ):
        return {
            "ok": True,
            "action": "queue_list",
            "queue": [],
            "state": self.state(
                chat_id,
            ),
        }

    async def queue_clear(
        self,
        chat_id,
    ):
        return {
            "ok": True,
            "action": "queue_clear",
            "state": self.state(
                chat_id,
            ),
        }

    async def skip(
        self,
        chat_id,
    ):
        return await self.stop(
            chat_id,
        )


service = AudioService()