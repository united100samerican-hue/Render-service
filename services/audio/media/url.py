from __future__ import annotations

import asyncio
import base64
import os
import shutil
import socket
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yt_dlp

from config import POT_PROVIDER_URL, YOUTUBE_COOKIES


VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".m4v",
    ".avi",
    ".m3u8",
    ".mpd",
    ".ts",
    ".m2ts",
}

AUDIO_EXTS = {
    ".mp3",
    ".ogg",
    ".oga",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".opus",
}

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtu.be",
    "www.youtu.be",
}

SEARCH_PREFIXES = (
    "ytsearch:",
    "ytsearch1:",
    "ytsearch2:",
    "ytsearch3:",
)


class UrlResolver:
    def __init__(self):
        self._timeout = 20
        self._cache_ttl = 90
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._cookie_file = ""
        self._pot_available = None
        self._pot_checked_at = 0.0
        self._prepare_cookies()

    def _cache_get(self, source: str) -> dict[str, Any] | None:
        item = self._cache.get(source)

        if not item:
            return None

        ts, value = item

        if time.time() - ts > self._cache_ttl:
            self._cache.pop(source, None)
            return None

        return dict(value)

    def invalidate(self, source: str):
        self._cache.pop(str(source or ""), None)

    def _cache_set(self, source: str, value: dict[str, Any]):
        self._cache[source] = (time.time(), dict(value))

        if len(self._cache) > 128:
            oldest = min(
                self._cache.items(),
                key=lambda item: item[1][0],
            )[0]
            self._cache.pop(oldest, None)

    def _prepare_cookies(self):
        raw = str(YOUTUBE_COOKIES or "").strip()

        if not raw:
            return

        text = raw

        if raw.startswith("base64:"):
            try:
                text = base64.b64decode(raw[7:].strip()).decode("utf-8")
            except Exception:
                return

        if "Netscape HTTP Cookie File" not in text and not (
            "\n" in text or "\r" in text
        ):
            return

        try:
            fd, path = tempfile.mkstemp(
                prefix="youtube_cookies_",
                suffix=".txt",
            )
            os.close(fd)

            Path(path).write_text(
                text,
                encoding="utf-8",
            )

            self._cookie_file = path
        except Exception:
            self._cookie_file = ""

    @staticmethod
    def _is_youtube_url(value: str) -> bool:
        raw = str(value or "").strip()

        if not raw.lower().startswith(("http://", "https://")):
            return False

        try:
            host = (
                str(urlsplit(raw).hostname or "")
                .lower()
                .rstrip(".")
            )
        except Exception:
            return False

        return host in YOUTUBE_HOSTS

    @staticmethod
    def _is_youtube_search(value: str) -> bool:
        return str(value or "").strip().lower().startswith(SEARCH_PREFIXES)

    @staticmethod
    def _is_direct(url: str, content_type: str = "") -> tuple[bool, str, bool]:
        raw = str(url or "").strip().lower()

        content = (
            content_type.lower()
            .split(";", 1)[0]
            .strip()
        )

        ext = Path(urlsplit(raw).path).suffix.lower()

        if raw.startswith(("rtmp://", "rtmps://", "rtsp://")):
            return True, "video", True

        if content.startswith("video/") or ext in VIDEO_EXTS:
            return True, "video", ext in {".m3u8", ".mpd"}

        if content.startswith("audio/") or ext in AUDIO_EXTS:
            return True, "audio", False

        if "mpegurl" in content or "dash+xml" in content:
            return True, "video", True

        return False, "audio", False

    async def _head(self, url: str) -> tuple[str, str]:
        if not str(url).lower().startswith(("http://", "https://")):
            return "", url

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
            ) as client:
                response = await client.head(
                    url,
                    headers={"User-Agent": "Mozilla/5.0"},
                )

                return (
                    str(response.headers.get("content-type", "")),
                    str(response.url),
                )
        except Exception:
            return "", url

    @staticmethod
    def _is_bot_check_error(exc: Exception) -> bool:
        message = str(exc).lower()

        return (
            "sign in to confirm" in message
            or "not a bot" in message
            or "bot check" in message
            or "confirm you’re not a bot" in message
            or "confirm you're not a bot" in message
            or "login_required" in message
        )

    def _provider_available(self) -> bool:
        now = time.time()

        if now - self._pot_checked_at < 10.0 and self._pot_available is not None:
            return bool(self._pot_available)

        try:
            base = str(POT_PROVIDER_URL or "").rstrip("/")
            if not base:
                self._pot_available = False
            else:
                response = httpx.get(
                    f"{base}/ping",
                    timeout=0.8,
                )
                self._pot_available = response.is_success
        except Exception:
            self._pot_available = False

        self._pot_checked_at = now
        return bool(self._pot_available)

    def _youtube_options(
        self,
        embedded: bool = False,
        use_provider: bool | None = None,
        player_client: str | None = None,
        flat_search: bool = False,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "socket_timeout": 20,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 2,
            "continuedl": True,
            "geo_bypass": True,
            "http_headers": {
                "User-Agent": (
                    "Mozilla/5.0 "
                    "(X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "(KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"
                )
            },
        }

        if flat_search:
            options["extract_flat"] = "in_playlist"
            options["playlistend"] = 1
        else:
            options["format"] = (
                "best[height<=720][ext=mp4][vcodec!=none][acodec!=none]"
                "/best[height<=720][vcodec!=none][acodec!=none]"
                "/best[acodec!=none]"
                "/best"
            )

        if use_provider is None:
            use_provider = self._provider_available()

        if player_client:
            options["extractor_args"] = {
                "youtube": {
                    "player_client": [player_client],
                }
            }
            if player_client == "mweb" and use_provider:
                options["extractor_args"]["youtubepot-bgutilhttp"] = {
                    "base_url": [POT_PROVIDER_URL],
                }
        elif use_provider and not embedded and not flat_search:
            options["extractor_args"] = {
                "youtube": {
                    "player_client": ["mweb"],
                },
                "youtubepot-bgutilhttp": {
                    "base_url": [POT_PROVIDER_URL],
                },
            }
        elif embedded:
            options["extractor_args"] = {
                "youtube": {
                    "player_client": ["web_embedded"],
                }
            }

        deno = shutil.which("deno")

        if not deno:
            candidate = "/usr/local/bin/deno"

            if Path(candidate).is_file():
                deno = candidate

        if deno:
            options["js_runtimes"] = {
                "deno": {
                    "path": deno,
                }
            }

        if self._cookie_file:
            options["cookiefile"] = self._cookie_file

        return options

    @staticmethod
    def _generic_options() -> dict[str, Any]:
        return {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "socket_timeout": 20,
            "retries": 3,
            "fragment_retries": 3,
            "concurrent_fragment_downloads": 2,
            "continuedl": True,
            "geo_bypass": True,
            "http_headers": {
                "User-Agent": "Mozilla/5.0",
            },
            "format": (
                "best[ext=mp4][vcodec!=none][acodec!=none]"
                "/best[vcodec!=none][acodec!=none]"
                "/best[acodec!=none]"
                "/best"
            ),
        }

    @staticmethod
    def _first_entry(info: dict[str, Any] | None) -> dict[str, Any] | None:
        if not info:
            return None

        entries = info.get("entries")
        if not entries:
            return info

        return next(
            (
                entry
                for entry in entries
                if entry
            ),
            None,
        )

    def _run(self, source: str, options: dict[str, Any]) -> dict[str, Any]:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(
                source,
                download=False,
            )

        info = self._first_entry(info)

        if not info:
            raise RuntimeError("url_metadata_empty")

        return info

    def _search_result_url(self, source: str) -> str:
        options = self._youtube_options(
            use_provider=False,
            flat_search=True,
        )

        info = self._run(source, options)

        webpage = str(
            info.get("webpage_url")
            or info.get("original_url")
            or ""
        ).strip()

        if webpage:
            return webpage

        video_id = str(info.get("id") or "").strip()

        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

        raise RuntimeError("search_result_url_missing")

    def _extract_direct(self, source: str) -> dict[str, Any]:
        provider = self._provider_available()
        errors: list[Exception] = []

        variants: list[dict[str, Any]] = []

        # When cookies are present, the authenticated request is tried first.
        # On Render/datacenter IPs this is materially more reliable for
        # YouTube's "Sign in to confirm you're not a bot" challenge.
        if self._cookie_file:
            variants.append(
                self._youtube_options(
                    use_provider=provider,
                )
            )

        # Keep the no-forced-client/default extractor available. It can work
        # on videos that do not require the mweb client.
        variants.append(
            self._youtube_options(
                use_provider=False,
            )
        )

        # mweb + bgutil remains the recommended PO-token path when available.
        if provider:
            variants.append(
                self._youtube_options(
                    use_provider=True,
                    player_client="mweb",
                )
            )

        # Embedded/tv clients do not require the bgutil POT path. They are
        # fallback clients for public videos that are embeddable/available
        # through those clients.
        variants.append(
            self._youtube_options(
                use_provider=False,
                player_client="web_embedded",
            )
        )
        variants.append(
            self._youtube_options(
                use_provider=False,
                player_client="tv",
            )
        )

        seen: set[str] = set()

        for options in variants:
            key = repr(
                options.get("extractor_args", {})
            )
            if key in seen:
                continue
            seen.add(key)

            try:
                return self._run(source, options)
            except yt_dlp.utils.DownloadError as exc:
                errors.append(exc)
                continue

        last = errors[-1] if errors else RuntimeError("youtube_extract_failed")

        if not self._cookie_file and any(
            self._is_bot_check_error(exc)
            for exc in errors
        ):
            raise RuntimeError(
                "youtube_login_required: YouTube is challenging the Render "
                "egress IP. Set YOUTUBE_COOKIES to a fresh Netscape cookie "
                "file from a dedicated YouTube browser session."
            ) from last

        raise last

    def _extract(self, source: str) -> dict[str, Any]:
        is_youtube = (
            self._is_youtube_search(source)
            or self._is_youtube_url(source)
        )

        if is_youtube and self._is_youtube_search(source):
            source = self._search_result_url(source)

        if is_youtube:
            info = self._extract_direct(source)
        else:
            info = self._run(
                source,
                self._generic_options(),
            )

        stream = str(info.get("url") or "")
        selected_headers = dict(
            info.get("http_headers")
            or {}
        )

        if not stream:
            formats = [
                item
                for item in (info.get("formats") or [])
                if item.get("url")
                and item.get("protocol") not in {"mhtml"}
            ]

            if is_youtube:
                progressive = [
                    item
                    for item in formats
                    if item.get("vcodec") not in (None, "none")
                    and item.get("acodec") not in (None, "none")
                    and (
                        not item.get("height")
                        or int(item.get("height") or 0) <= 720
                    )
                ]

                audio_only = [
                    item
                    for item in formats
                    if item.get("acodec") not in (None, "none")
                ]

                formats = progressive or audio_only or formats

            if not formats:
                raise RuntimeError("url_stream_not_found")

            formats.sort(
                key=lambda item: (
                    min(int(item.get("height") or 0), 720),
                    item.get("tbr") or 0,
                    item.get("abr") or 0,
                ),
                reverse=True,
            )

            selected = formats[0]
            stream = str(selected["url"])
            selected_headers = dict(
                selected.get("http_headers")
                or {}
            )

        webpage = str(
            info.get("webpage_url")
            or info.get("original_url")
            or ""
        ).strip()

        if not webpage and not self._is_youtube_search(source):
            webpage = source

        if not webpage:
            raise RuntimeError("search_result_url_missing")

        title = (
            str(info.get("title") or "").strip()
            or "غير معروف"
        )

        video_codec = str(info.get("vcodec") or "")
        audio_codec = str(info.get("acodec") or "")

        video = bool(
            video_codec
            and video_codec != "none"
            and audio_codec
            and audio_codec != "none"
        )

        return {
            "source_url": webpage,
            "stream_url": stream,
            "title": title,
            "duration": int(info.get("duration") or 0),
            "webpage_url": webpage,
            "thumbnail": str(info.get("thumbnail") or ""),
            "video": video,
            "media_kind": "video" if video else "audio",
            "live": bool(info.get("is_live")),
            "video_id": str(info.get("id") or ""),
            "http_headers": selected_headers,
        }

    async def resolve(self, url: str) -> dict[str, Any]:
        source = str(url or "").strip()

        if not source:
            raise RuntimeError("url_missing")

        cache_key = source

        cached = self._cache_get(cache_key)
        if cached:
            return cached

        if (
            self._is_youtube_search(source)
            or self._is_youtube_url(source)
        ):
            result = await asyncio.to_thread(
                self._extract,
                source,
            )

            self._cache_set(cache_key, result)

            # Search results should also be cached under the resolved page URL
            # to reduce repeated YouTube player requests for the same selection.
            resolved_url = str(
                result.get("webpage_url")
                or ""
            ).strip()
            if resolved_url and resolved_url != cache_key:
                self._cache_set(resolved_url, result)

            return result

        direct, kind, live = self._is_direct(source)
        final = source

        if not direct:
            content_type, final = await self._head(source)
            direct, kind, live = self._is_direct(
                final,
                content_type,
            )

        if direct:
            cached = self._cache_get(source)

            if cached:
                return cached

            name = (
                Path(urlsplit(final or source).path).name
                or Path(urlsplit(source).path).name
            )

            result = {
                "source_url": source,
                "stream_url": final or source,
                "title": (
                    Path(name).stem.strip()
                    if name
                    else "Audio"
                ),
                "duration": 0,
                "webpage_url": source,
                "thumbnail": "",
                "video": kind == "video",
                "media_kind": kind,
                "live": live,
                "http_headers": {
                    "User-Agent": "Mozilla/5.0",
                },
            }

            self._cache_set(source, result)
            return result

        result = await asyncio.to_thread(
            self._run,
            source,
            self._generic_options(),
        )

        stream = str(result.get("url") or "")
        headers = dict(
            result.get("http_headers")
            or {}
        )

        if not stream:
            formats = [
                item
                for item in (result.get("formats") or [])
                if item.get("url")
                and item.get("protocol") not in {"mhtml"}
            ]

            if not formats:
                raise RuntimeError("url_stream_not_found")

            formats.sort(
                key=lambda item: (
                    item.get("height") or 0,
                    item.get("tbr") or 0,
                    item.get("abr") or 0,
                ),
                reverse=True,
            )
            selected = formats[0]
            stream = str(selected["url"])
            headers = dict(
                selected.get("http_headers")
                or {}
            )

        webpage = str(
            result.get("webpage_url")
            or result.get("original_url")
            or source
        ).strip()

        video_codec = str(result.get("vcodec") or "")
        audio_codec = str(result.get("acodec") or "")
        video = bool(
            video_codec
            and video_codec != "none"
            and audio_codec
            and audio_codec != "none"
        )

        output = {
            "source_url": webpage,
            "stream_url": stream,
            "title": str(
                result.get("title")
                or "غير معروف"
            ),
            "duration": int(
                result.get("duration")
                or 0
            ),
            "webpage_url": webpage,
            "thumbnail": str(
                result.get("thumbnail")
                or ""
            ),
            "video": video,
            "media_kind": (
                "video"
                if video
                else "audio"
            ),
            "live": bool(result.get("is_live")),
            "video_id": str(
                result.get("id")
                or ""
            ),
            "http_headers": headers,
        }

        self._cache_set(source, output)
        return output
