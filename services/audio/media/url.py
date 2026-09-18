from __future__ import annotations

import asyncio
import base64
import logging
import os
import re
import shutil
import socket
import tempfile
import time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
import yt_dlp

from config import POT_PROVIDER_URL, YOUTUBE_COOKIES


log = logging.getLogger("audio_url")


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
        self._cookie_status: dict[str, Any] = {
            "configured": False,
            "loaded": False,
            "valid_format": False,
            "source": "none",
            "bytes": 0,
            "youtube_domains": 0,
            "cookie_rows": 0,
            "auth_cookies": 0,
            "expired_auth_cookies": 0,
        }
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

    @staticmethod
    def _looks_like_base64(value: str) -> bool:
        compact = re.sub(r"\s+", "", str(value or ""))
        if len(compact) < 64 or len(compact) % 4:
            return False
        return bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact))

    @staticmethod
    def _decode_cookie_text(raw: str) -> tuple[str, str]:
        value = str(raw or "").strip()
        if not value:
            return "", "none"

        if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
            value = value[1:-1].strip()

        # Render environment variables may contain escaped newlines/tabs.
        # Decode those before any filesystem probing so a whole cookie file is
        # never accidentally treated as one enormous filename.
        if "\\n" in value or "\\r" in value or "\\t" in value:
            value = value.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\r").replace("\\t", "\t")

        if value.startswith("base64:"):
            encoded = re.sub(r"\s+", "", value[7:])
            try:
                return base64.b64decode(encoded, validate=False).decode("utf-8-sig"), "base64"
            except Exception:
                return "", "base64_invalid"

        if value.startswith("data:text/plain;base64,"):
            encoded = re.sub(r"\s+", "", value.split(",", 1)[1])
            try:
                return base64.b64decode(encoded, validate=False).decode("utf-8-sig"), "base64"
            except Exception:
                return "", "base64_invalid"

        if "Netscape HTTP Cookie File" in value[:256] or "HTTP Cookie File" in value[:256] or "\n" in value or "\r" in value:
            return value, "text"

        if UrlResolver._looks_like_base64(value):
            try:
                decoded = base64.b64decode(re.sub(r"\s+", "", value), validate=False).decode("utf-8-sig")
                if "HTTP Cookie File" in decoded[:256] or "Netscape HTTP Cookie File" in decoded[:256]:
                    return decoded, "base64_auto"
            except Exception:
                pass

        # A real file path is the final possibility. Guard both length and
        # OSError because Linux rejects overlong path components with errno 36.
        if "\n" not in value and "\r" not in value and "\x00" not in value and len(value) <= 4096:
            try:
                candidate_path = Path(value).expanduser()
                if candidate_path.is_file():
                    try:
                        return candidate_path.read_text(encoding="utf-8"), "file"
                    except (OSError, UnicodeError):
                        pass
            except (OSError, ValueError):
                pass

        return value, "text_invalid"

    @staticmethod
    def _cookie_stats(text: str) -> dict[str, Any]:
        auth_names = {
            "SID", "HSID", "SSID", "APISID", "SAPISID",
            "LOGIN_INFO", "SIDCC", "__SECURE-1PSID",
            "__SECURE-3PSID", "__SECURE-1PSIDTS",
            "__SECURE-3PSIDTS",
        }
        now = int(time.time())
        lines = str(text or "").splitlines()
        first = next((line.lstrip("\ufeff").strip() for line in lines if line.strip()), "")
        valid_header = first in {"# Netscape HTTP Cookie File", "# HTTP Cookie File"}
        domains: set[str] = set()
        rows = 0
        auth = 0
        expired_auth = 0
        login_info = 0
        sapisid = 0

        for line in lines:
            if not line.strip() or line.startswith("#") and not line.startswith("#HttpOnly_"):
                continue
            normalized = line
            if normalized.startswith("#HttpOnly_"):
                normalized = normalized[len("#HttpOnly_"):]
            parts = normalized.split("\t")
            if len(parts) < 7:
                continue
            rows += 1
            domain = parts[0].strip().lower().lstrip(".")
            name = parts[5].strip().upper()
            if domain == "youtube.com" or domain.endswith(".youtube.com"):
                domains.add(domain)
                if name in auth_names:
                    auth += 1
                    if name == "LOGIN_INFO":
                        login_info += 1
                    if name in {"SAPISID", "__SECURE-3PAPISID", "__SECURE-1PAPISID"}:
                        sapisid += 1
                    try:
                        expires = int(parts[4].strip() or 0)
                    except Exception:
                        expires = 0
                    if expires and expires < now:
                        expired_auth += 1

        return {
            "valid_format": valid_header,
            "cookie_rows": rows,
            "youtube_domains": len(domains),
            "auth_cookies": auth,
            "expired_auth_cookies": expired_auth,
            "login_info": login_info > 0,
            "sapisid_cookies": sapisid,
        }

    def _prepare_cookies(self):
        self._cookie_file = ""
        raw = str(YOUTUBE_COOKIES or "").strip()
        if not raw:
            return

        text, source = self._decode_cookie_text(raw)
        stats = self._cookie_stats(text)
        self._cookie_status = {
            "configured": True,
            "loaded": False,
            "source": source,
            "bytes": len(text.encode("utf-8", "ignore")),
            **stats,
        }

        if not stats["valid_format"] or stats["cookie_rows"] <= 0:
            return

        fd, path = tempfile.mkstemp(prefix="youtube_cookies_", suffix=".txt")
        os.close(fd)
        try:
            Path(path).write_text(text.replace("\r\n", "\n").replace("\r", "\n"), encoding="utf-8")
            os.chmod(path, 0o600)

            jar = MozillaCookieJar(path)
            jar.load(ignore_discard=True, ignore_expires=True)
            self._cookie_file = path
        except Exception as exc:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass
            self._cookie_status["valid_format"] = False
            self._cookie_status["load_error"] = type(exc).__name__
            return

        self._cookie_status["loaded"] = True

        log.info(
                "youtube cookies configured source=%s valid=%s bytes=%d youtube_domains=%d auth=%d expired_auth=%d",
                source,
                bool(self._cookie_file),
                self._cookie_status.get("bytes", 0),
                self._cookie_status.get("youtube_domains", 0),
                self._cookie_status.get("auth_cookies", 0),
                self._cookie_status.get("expired_auth_cookies", 0),
            )

    def cookie_status(self) -> dict[str, Any]:
        return dict(self._cookie_status)

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

        ext = Path(
            urlsplit(raw).path
        ).suffix.lower()

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
        )

    def _provider_available(self) -> bool:
        now = time.time()
        if now - self._pot_checked_at < 10.0 and self._pot_available is not None:
            return bool(self._pot_available)

        try:
            host = urlsplit(POT_PROVIDER_URL).hostname or "127.0.0.1"
            port = int(urlsplit(POT_PROVIDER_URL).port or 4416)
            with socket.create_connection((host, port), timeout=0.25):
                self._pot_available = True
        except Exception:
            self._pot_available = False

        self._pot_checked_at = now
        return bool(self._pot_available)

    def _youtube_options(
        self,
        player_clients: list[str] | None = None,
        use_provider: bool = False,
        use_cookies: bool = True,
    ) -> dict[str, Any]:
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "skip_download": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "socket_timeout": 25,
            "retries": 5,
            "extractor_retries": 3,
            "fragment_retries": 5,
            "file_access_retries": 3,
            "concurrent_fragment_downloads": 3,
            "continuedl": True,
            "geo_bypass": True,
            # Keep the old working format strategy: prefer HLS when a supported
            # YouTube client exposes it, then fall back to progressive HTTP.
            # HLS is intentionally allowed here because PyTgCalls/FFmpeg can
            # play the returned remote manifest directly.
            "format": (
                "best[protocol^=m3u8][vcodec!=none][acodec!=none]"
                "/best[protocol^=m3u8][acodec!=none]"
                "/best[protocol^=http][vcodec!=none][acodec!=none]"
                "/best[protocol^=http][acodec!=none]"
                "/best[acodec!=none]"
                "/best"
            ),
        }

        youtube_args: dict[str, Any] = {}
        if player_clients:
            # This is deliberately a single yt-dlp extraction using the same
            # client combination that the old working service used. It lets
            # yt-dlp merge whichever client returns usable data instead of
            # forcing each client into isolated attempts.
            youtube_args["player_client"] = list(player_clients)
            # Keep formats requiring a PO token visible to yt-dlp. The old
            # working service explicitly used this with bgutil.
            youtube_args["formats"] = ["missing_pot"]

        extractor_args: dict[str, Any] = {}
        if youtube_args:
            extractor_args["youtube"] = youtube_args

        if use_provider and self._provider_available():
            extractor_args["youtubepot-bgutilhttp"] = {
                "base_url": [POT_PROVIDER_URL],
            }

        if extractor_args:
            options["extractor_args"] = extractor_args

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

        if use_cookies and self._cookie_file:
            options["cookiefile"] = self._cookie_file

        # Match the previously working Render service: allow yt-dlp to fetch
        # updated EJS challenge scripts when the bundled package is not enough.
        options["remote_components"] = ["ejs:github"]

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
            "concurrent_fragment_downloads": 4,
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

    def _extract(self, source: str) -> dict[str, Any]:
        is_youtube = (
            self._is_youtube_search(source)
            or self._is_youtube_url(source)
        )

        attempts: list[tuple[str, dict[str, Any]]] = []
        provider = self._provider_available() if is_youtube else False

        if is_youtube:
            # Reproduce the extraction strategy from the old service that was
            # known to work: use one combined client request with mweb,
            # web_safari and android, while keeping missing-POT formats enabled.
            if self._cookie_file:
                attempts.append((
                    "legacy-mweb-web_safari-android",
                    self._youtube_options(
                        player_clients=["mweb", "web_safari", "android"],
                        use_provider=provider,
                        use_cookies=True,
                    ),
                ))
                attempts.append((
                    "web_safari",
                    self._youtube_options(
                        player_clients=["web_safari"],
                        use_provider=False,
                        use_cookies=True,
                    ),
                ))
                attempts.append((
                    "mweb+bgutil",
                    self._youtube_options(
                        player_clients=["mweb"],
                        use_provider=provider,
                        use_cookies=True,
                    ),
                ))
                attempts.append((
                    "android-guest",
                    self._youtube_options(
                        player_clients=["android"],
                        use_provider=False,
                        use_cookies=False,
                    ),
                ))
            else:
                attempts.append((
                    "legacy-mweb-web_safari-android",
                    self._youtube_options(
                        player_clients=["mweb", "web_safari", "android"],
                        use_provider=provider,
                        use_cookies=False,
                    ),
                ))
                if provider:
                    attempts.append((
                        "mweb+bgutil",
                        self._youtube_options(
                            player_clients=["mweb"],
                            use_provider=True,
                            use_cookies=False,
                        ),
                    ))
                attempts.append((
                    "web_safari",
                    self._youtube_options(
                        player_clients=["web_safari"],
                        use_provider=False,
                        use_cookies=False,
                    ),
                ))
                attempts.append((
                    "android-vr-guest",
                    self._youtube_options(
                        player_clients=["android_vr"],
                        use_provider=False,
                        use_cookies=False,
                    ),
                ))
        else:
            attempts = [("generic", self._generic_options())]

        def run(opts: dict[str, Any]) -> dict[str, Any]:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source, download=False)

                if info and info.get("entries"):
                    info = next((entry for entry in info["entries"] if entry), None)

                if not info:
                    raise RuntimeError("url_metadata_empty")

                return info

        last_exc: Exception | None = None
        for index, (label, options) in enumerate(attempts):
            try:
                info = run(options)
                if is_youtube:
                    log.info(
                        "youtube extraction succeeded attempt=%d/%d client=%s yt_dlp=%s provider=%s cookies=%s",
                        index + 1,
                        len(attempts),
                        label,
                        getattr(yt_dlp.version, "__version__", "unknown"),
                        provider,
                        bool(self._cookie_file),
                    )
                break
            except yt_dlp.utils.DownloadError as exc:
                last_exc = exc
                message = str(exc).lower()
                retryable = (
                    self._is_bot_check_error(exc)
                    or "page needs to be reloaded" in message
                    or "no formats" in message
                    or "unable to extract" in message
                    or "failed to extract" in message
                    or "player response" in message
                )
                log.warning(
                    "youtube extraction failed attempt=%d/%d client=%s retryable=%s error=%s",
                    index + 1,
                    len(attempts),
                    label,
                    retryable,
                    str(exc).splitlines()[0][:300],
                )
                if not is_youtube or index >= len(attempts) - 1 or not retryable:
                    raise
                time.sleep(0.35)
        else:
            if last_exc is not None:
                if is_youtube and self._cookie_file and self._is_bot_check_error(last_exc):
                    if not self._cookie_status.get("login_info"):
                        raise RuntimeError("youtube_cookies_missing_login_info") from last_exc
                    raise RuntimeError("youtube_egress_or_session_rejected") from last_exc
                raise last_exc

        stream = str(info.get("url") or "")

        if not stream:
            formats = [
                item for item in (info.get("formats") or [])
                if item.get("url") and item.get("protocol") not in {"mhtml"}
            ]

            if is_youtube:
                formats = [
                    item
                    for item in formats
                    if item.get("vcodec") not in (None, "none")
                    and item.get("acodec") not in (None, "none")
                ] or [
                    item
                    for item in formats
                    if item.get("acodec") not in (None, "none")
                ] or formats

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

            chosen = formats[0]
            stream = str(chosen["url"])
            raw_headers = chosen.get("http_headers") or info.get("http_headers") or {}
        else:
            raw_headers = info.get("http_headers") or {}

        allowed_headers = {}
        for key, value in dict(raw_headers or {}).items():
            if str(key).lower() in {"user-agent", "referer", "origin", "accept", "accept-language"}:
                allowed_headers[str(key)] = str(value)

        webpage = str(info.get("webpage_url") or info.get("original_url") or "").strip()
        if not webpage and not self._is_youtube_search(source):
            webpage = source
        if not webpage:
            raise RuntimeError("search_result_url_missing")

        title = str(info.get("title") or "").strip() or "غير معروف"
        video_codec = str(info.get("vcodec") or "")
        audio_codec = str(info.get("acodec") or "")
        video = bool(video_codec and video_codec != "none" and audio_codec and audio_codec != "none")

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
            "http_headers": allowed_headers,
            # YouTube URLs are remote FFmpeg/PyTgCalls inputs. Do not fetch
            # an HLS manifest as if it were a local media file.
            "remote_stream": bool(is_youtube and stream),
        }

    async def resolve(self, url: str) -> dict[str, Any]:
        source = str(url or "").strip()

        if not source:
            raise RuntimeError("url_missing")

        if (
            self._is_youtube_search(source)
            or self._is_youtube_url(source)
        ):
            cached = self._cache_get(source)

            if cached:
                return cached

            result = await asyncio.to_thread(
                self._extract,
                source,
            )

            self._cache_set(source, result)
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
                "title": Path(name).stem.strip()
                if name
                else "Audio",
                "duration": 0,
                "webpage_url": source,
                "thumbnail": "",
                "video": kind == "video",
                "media_kind": kind,
                "live": live,
            }

            self._cache_set(source, result)
            return result

        cached = self._cache_get(source)

        if cached:
            return cached

        result = await asyncio.to_thread(
            self._extract,
            source,
        )

        self._cache_set(source, result)
        return result