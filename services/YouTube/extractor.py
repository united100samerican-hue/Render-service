from __future__ import annotations

import asyncio
import os
from pathlib import Path
import shutil
import time
from typing import Any

import yt_dlp

from .config import Settings
from .errors import SocialMediaError
from .models import MediaInfo


class Extractor:
    def __init__(self, settings: Settings) -> None:
        self.s = settings
        self._cookie_runtime_file = self.s.media_dir / ".cookies.txt"

    async def _prepare_cookie_file(self) -> str:
        source = self.s.cookies_file
        if not source:
            return ""
        if not source.is_file():
            raise SocialMediaError("cookies_file_missing", str(source))

        def copy_cookie() -> None:
            data = source.read_bytes()
            first = data.splitlines()[0].decode("utf-8-sig", "ignore").strip() if data.splitlines() else ""
            if first not in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}:
                raise SocialMediaError("invalid_cookies_file", "cookies must be in Netscape/Mozilla format")
            self._cookie_runtime_file.write_bytes(data)

        await asyncio.to_thread(copy_cookie)
        return str(self._cookie_runtime_file)

    async def _base_opts(self) -> dict[str, Any]:
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "socket_timeout": self.s.request_timeout,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 4,
            "continuedl": True,
            "http_headers": {
                "User-Agent": os.getenv(
                    "SOCIAL_MEDIA_USER_AGENT",
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36",
                ),
            },
            "js_runtimes": {"node": {"path": shutil.which("node") or "/usr/bin/node"}},
        }
        cookiefile = await self._prepare_cookie_file()
        if cookiefile:
            opts["cookiefile"] = cookiefile
        return opts

    async def _info(self, url: str) -> dict[str, Any]:
        opts = await self._base_opts()
        try:
            return await asyncio.to_thread(self._info_sync, url, opts)
        except yt_dlp.utils.DownloadError as exc:
            raise SocialMediaError("extract_failed", str(exc)) from exc

    @staticmethod
    def _info_sync(url: str, opts: dict[str, Any]) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            raise SocialMediaError("empty_info")
        if info.get("_type") == "playlist":
            raise SocialMediaError("playlist_not_allowed")
        return info

    @staticmethod
    def _formats(info: dict[str, Any]) -> list[dict[str, Any]]:
        return [f for f in info.get("formats") or [] if f.get("url")]

    def _best_combined(self, info: dict[str, Any]) -> dict[str, Any] | None:
        formats = [
            f for f in self._formats(info)
            if f.get("vcodec") not in (None, "none") and f.get("acodec") not in (None, "none")
        ]
        if not formats:
            return None
        formats.sort(
            key=lambda f: (
                min(int(f.get("height") or 0), self.s.max_height),
                float(f.get("tbr") or 0),
                float(f.get("fps") or 0),
            ),
            reverse=True,
        )
        return formats[0]

    @staticmethod
    def _best_audio(info: dict[str, Any]) -> dict[str, Any] | None:
        formats = [f for f in info.get("formats") or [] if f.get("url") and f.get("acodec") not in (None, "none")]
        return max(formats, key=lambda f: float(f.get("abr") or f.get("tbr") or 0)) if formats else None

    async def inspect(self, url: str) -> MediaInfo:
        url = str(url or "").strip()
        if not url.lower().startswith(("http://", "https://")):
            raise SocialMediaError("invalid_url", "URL must start with http:// or https://")
        info = await self._info(url)
        combined = self._best_combined(info)
        audio = self._best_audio(info)
        all_formats = self._formats(info)
        has_any_video = any(f.get("vcodec") not in (None, "none") for f in all_formats)
        duration = max(0, int(float(info.get("duration") or 0)))
        if self.s.max_duration and duration > self.s.max_duration:
            raise SocialMediaError("duration_limit", "media duration exceeds the configured limit")
        direct = combined or audio
        return MediaInfo(
            url=url,
            webpage_url=str(info.get("webpage_url") or url),
            title=str(info.get("title") or "وسائط غير معروفة").strip(),
            duration=duration,
            thumbnail=str(info.get("thumbnail") or "").strip(),
            extractor=str(info.get("extractor_key") or info.get("extractor") or "").strip(),
            is_live=bool(info.get("is_live")),
            has_video=bool(combined) or (bool(has_any_video) and not bool(info.get("is_live"))),
            has_audio=bool(audio or combined),
            direct_url=str(direct.get("url") if direct else "").strip(),
            direct_ext=str(direct.get("ext") if direct else "").strip(),
            source_id=str(info.get("id") or "").strip(),
        )

    async def download_vod(self, info: MediaInfo) -> str:
        if info.is_live:
            raise SocialMediaError("live_download_not_supported")
        opts = await self._base_opts()
        opts.update(
            {
                "format": (
                    f"bv*[height<={self.s.max_height}]+ba/"
                    f"b[height<={self.s.max_height}]/b"
                ),
                "merge_output_format": "mp4",
                "outtmpl": str(self.s.media_dir / "%(extractor_key)s_%(id)s_%(epoch)s.%(ext)s"),
                "overwrites": False,
            }
        )
        try:
            data = await asyncio.to_thread(self._download_sync, info.url, opts)
            return data
        except SocialMediaError:
            raise
        except yt_dlp.utils.DownloadError as exc:
            raise SocialMediaError("download_failed", str(exc)) from exc

    def _download_sync(self, url: str, opts: dict[str, Any]) -> str:
        with yt_dlp.YoutubeDL(opts) as ydl:
            data = ydl.extract_info(url, download=True)
            if not data:
                raise SocialMediaError("download_empty")
            prepared = Path(ydl.prepare_filename(data))
            candidates = [prepared, prepared.with_suffix(".mp4")]
            if not prepared.exists():
                candidates.extend(
                    sorted(
                        self.s.media_dir.glob(
                            f"{data.get('extractor_key','unknown')}_{data.get('id','unknown')}_*.mp4"
                        ),
                        key=lambda p: p.stat().st_mtime,
                        reverse=True,
                    )
                )
            path = next((p for p in candidates if p.exists()), None)
            if path is None:
                raise SocialMediaError("download_file_missing")
            if path.stat().st_size > self.s.max_media_bytes:
                path.unlink(missing_ok=True)
                raise SocialMediaError("media_size_limit")
            return str(path)
