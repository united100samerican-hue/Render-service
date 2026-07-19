from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from yt_dlp import YoutubeDL

from config import settings
from session import sessions

log = logging.getLogger("tiktok.receiver")

cfg = settings()


@dataclass
class CacheItem:
    url: str
    video_url: str
    audio_url: str
    title: str
    uploader: str
    thumbnail: str
    expires: float


class TikTokReceiver:

    def __init__(self) -> None:

        self.cache: dict[str, CacheItem] = {}

        self.lock = asyncio.Lock()

        self.ydl = YoutubeDL(
            {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "cachedir": False,
                "retries": 5,
                "socket_timeout": 15,
                "extract_flat": False,
                "http_headers": {
                    "User-Agent": cfg.USER_AGENT
                },
            }
        )

    async def resolve(
        self,
        *,
        chat_id: int,
        url: str,
        force: bool = False,
    ) -> dict[str, Any]:

        async with self.lock:

            now = time.time()

            if not force:

                cached = self.cache.get(url)

                if cached and cached.expires > now:

                    state = sessions.get(chat_id)

                    state.stream.source_url = url

                    state.stream.video_url = cached.video_url

                    state.stream.audio_url = cached.audio_url

                    state.title = cached.title

                    state.touch()

                    return {
                        "ok": True,
                        "video_url": cached.video_url,
                        "audio_url": cached.audio_url,
                        "title": cached.title,
                        "uploader": cached.uploader,
                        "thumbnail": cached.thumbnail,
                        "cached": True,
                    }

            return await self._extract(chat_id, url)
        async def _extract(
        self,
        chat_id: int,
        url: str,
    ) -> dict[str, Any]:

        for attempt in range(1, 6):

            try:

                info = await asyncio.to_thread(
                    self.ydl.extract_info,
                    url,
                    False,
                )

                if not info:
                    raise RuntimeError("empty_info")

                video_url = ""
                audio_url = ""

                formats = info.get("formats") or []

                best_video = None
                best_audio = None

                for fmt in formats:

                    vcodec = str(fmt.get("vcodec") or "none")
                    acodec = str(fmt.get("acodec") or "none")

                    if vcodec != "none":

                        if (
                            best_video is None
                            or int(fmt.get("height") or 0)
                            > int(best_video.get("height") or 0)
                        ):
                            best_video = fmt

                    if acodec != "none":

                        if (
                            best_audio is None
                            or int(fmt.get("abr") or 0)
                            > int(best_audio.get("abr") or 0)
                        ):
                            best_audio = fmt

                if best_video:
                    video_url = (
                        best_video.get("url")
                        or best_video.get("manifest_url")
                        or ""
                    )

                if best_audio:
                    audio_url = (
                        best_audio.get("url")
                        or best_audio.get("manifest_url")
                        or ""
                    )

                if not video_url:
                    video_url = info.get("url") or ""

                if not audio_url:
                    audio_url = video_url

                if not video_url:
                    raise RuntimeError("video_not_found")

                title = (
                    info.get("title")
                    or "TikTok Live"
                )

                uploader = (
                    info.get("uploader")
                    or info.get("creator")
                    or ""
                )

                thumbnail = (
                    info.get("thumbnail")
                    or ""
                )

                self.cache[url] = CacheItem(

                    url=url,

                    video_url=video_url,

                    audio_url=audio_url,

                    title=title,

                    uploader=uploader,

                    thumbnail=thumbnail,

                    expires=time.time() + 300,

                )

                state = sessions.get(chat_id)

                state.stream.source_url = url
                state.stream.video_url = video_url
                state.stream.audio_url = audio_url

                state.title = title

                state.touch()

                log.info(
                    "Resolved TikTok stream chat=%s",
                    chat_id,
                )

                return {

                    "ok": True,

                    "video_url": video_url,

                    "audio_url": audio_url,

                    "title": title,

                    "uploader": uploader,

                    "thumbnail": thumbnail,

                    "cached": False,

                }

            except Exception as e:

                log.warning(
                    "yt-dlp attempt %s failed: %s",
                    attempt,
                    e,
                )

                if attempt == 5:

                    return {

                        "ok": False,

                        "error": str(e),

                    }

                await asyncio.sleep(2)
        async def refresh(
        self,
        *,
        chat_id:int,
        url:str,
    )->dict[str,Any]:

        self.cache.pop(url,None)

        return await self.resolve(
            chat_id=chat_id,
            url=url,
            force=True,
        )

    async def metadata(
        self,
        url:str,
    )->dict[str,Any]:

        item=self.cache.get(url)

        if item:

            return{

                "ok":True,

                "title":item.title,

                "uploader":item.uploader,

                "thumbnail":item.thumbnail,

                "cached":True,

            }

        try:

            info=await asyncio.to_thread(
                self.ydl.extract_info,
                url,
                False,
            )

            if not info:

                return{
                    "ok":False,
                    "error":"metadata_not_found",
                }

            return{

                "ok":True,

                "title":info.get("title") or "TikTok Live",

                "uploader":info.get("uploader") or info.get("creator") or "",

                "thumbnail":info.get("thumbnail") or "",

                "cached":False,

            }

        except Exception as e:

            return{

                "ok":False,

                "error":str(e),

            }

    def invalidate(
        self,
        url:str,
    )->None:

        self.cache.pop(url,None)

    def clear_cache(self)->None:

        self.cache.clear()

    def is_cached(
        self,
        url:str,
    )->bool:

        item=self.cache.get(url)

        if not item:

            return False

        return item.expires>time.time()

    async def close(self)->None:

        self.clear_cache()


receiver=TikTokReceiver()

