from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from telethon import TelegramClient
from telethon.sessions import StringSession

try:
    from pytgcalls.group_call_factory import GroupCallFactory
    from pytgcalls.mtproto_client_type import MTProtoClientType
except Exception:
    try:
        from pytgcalls import GroupCallFactory, MTProtoClientType
    except Exception:
        GroupCallFactory = None
        MTProtoClientType = None

logger = logging.getLogger("tiktok_bridge")


@dataclass
class BridgeResult:
    ok: bool
    error: str = ""
    state: dict[str, Any] | None = None


class TikTokBridge:
    def __init__(self) -> None:
        self.active = False
        self._state: dict[str, Any] = {"status": "idle", "bridge": False}
        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._client: Optional[TelegramClient] = None
        self._group_call = None
        self._ffmpeg = None
        self._writer_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=512)
        self._chat_id = 0
        self._source_url = ""
        self._title = ""

    def _telegram_env_ok(self) -> bool:
        return bool(
            os.getenv("SESSION_STRING", "").strip()
            and int(os.getenv("API_ID", "0") or 0)
            and os.getenv("API_HASH", "").strip()
        )

    def _rtmp_target(self) -> str:
        full = os.getenv("TIKTOK_RTMP_URL", "").strip()
        if full:
            return full
        server = os.getenv("TIKTOK_RTMP_SERVER", "").strip().rstrip("/")
        key = os.getenv("TIKTOK_STREAM_KEY", "").strip()
        if server and key:
            return f"{server}/{key}"
        return ""

    def _video_size(self) -> tuple[int, int]:
        w = max(320, int(os.getenv("BRIDGE_VIDEO_WIDTH", "720") or 720))
        h = max(240, int(os.getenv("BRIDGE_VIDEO_HEIGHT", "1280") or 1280))
        return w, h

    def _video_fps(self) -> int:
        return max(1, int(os.getenv("BRIDGE_VIDEO_FPS", "30") or 30))

    def _audio_rate(self) -> int:
        return max(8000, int(os.getenv("BRIDGE_AUDIO_RATE", "48000") or 48000))

    def _audio_channels(self) -> int:
        return max(1, int(os.getenv("BRIDGE_AUDIO_CHANNELS", "2") or 2))

    def _audio_bitrate(self) -> str:
        kbps = max(32, int(os.getenv("BRIDGE_AUDIO_BITRATE_KBPS", "128") or 128))
        return f"{kbps}k"

    async def _maybe(self, value: Any) -> Any:
        return await value if asyncio.iscoroutine(value) or asyncio.isfuture(value) else value

    async def _ensure_client(self) -> TelegramClient:
        if self._client is not None:
            return self._client
        api_id = int(os.getenv("API_ID", "0") or 0)
        api_hash = os.getenv("API_HASH", "").strip()
        session = os.getenv("SESSION_STRING", "").strip()
        if not api_id or not api_hash or not session:
            raise RuntimeError("missing_telegram_env")
        self._client = TelegramClient(StringSession(session), api_id, api_hash)
        await self._maybe(self._client.start())
        return self._client

    def _build_group_call(self):
        if GroupCallFactory is None or MTProtoClientType is None:
            raise RuntimeError("pytgcalls_missing")
        factory = GroupCallFactory(
            self._client,
            mtproto_backend=MTProtoClientType.TELETHON,
            enable_logs_to_console=False,
        )
        return factory.get_raw_group_call(
            on_played_data=self._on_played_data,
            on_recorded_data=self._on_recorded_data,
        )

    def _enqueue_frame(self, frame: bytes) -> None:
        if not self.active or not frame:
            return
        try:
            self._audio_queue.put_nowait(frame)
        except asyncio.QueueFull:
            try:
                _ = self._audio_queue.get_nowait()
            except Exception:
                pass
            try:
                self._audio_queue.put_nowait(frame)
            except Exception:
                pass

    def _on_recorded_data(self, _call, frame: bytes, length: int) -> None:
        if not frame:
            return
        data = bytes(frame[:length] if length else frame)
        if self._loop and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._enqueue_frame, data)

    def _on_played_data(self, _call, length: int) -> bytes:
        return b"\0" * max(0, int(length or 0))

    async def _spawn_ffmpeg(self, rtmp_url: str):
        w, h = self._video_size()
        fps = self._video_fps()
        rate = self._audio_rate()
        channels = self._audio_channels()
        audio_bitrate = self._audio_bitrate()
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={w}x{h}:r={fps}",
            "-f",
            "s16le",
            "-ar",
            str(rate),
            "-ac",
            str(channels),
            "-i",
            "pipe:0",
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            audio_bitrate,
            "-f",
            "flv",
            rtmp_url,
        ]
        return await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _read_ffmpeg_stderr(self) -> None:
        try:
            if not self._ffmpeg or not self._ffmpeg.stderr:
                return
            while self.active:
                line = await self._ffmpeg.stderr.readline()
                if not line:
                    break
                msg = line.decode(errors="ignore").strip()
                if msg:
                    logger.info("ffmpeg: %s", msg)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("ffmpeg_stderr_reader_failed: %s", e)

    async def _writer_loop(self) -> None:
        try:
            while self.active:
                frame = await self._audio_queue.get()
                if frame is None:
                    continue
                if not self._ffmpeg or not self._ffmpeg.stdin:
                    continue
                self._ffmpeg.stdin.write(frame)
                try:
                    await self._ffmpeg.stdin.drain()
                except Exception:
                    break
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.warning("bridge_writer_failed: %s", e)

    async def _hard_stop(self) -> None:
        self.active = False
        self._state["bridge"] = False
        self._state["status"] = "stopped"

        if self._writer_task and not self._writer_task.done():
            self._writer_task.cancel()
            try:
                await self._writer_task
            except Exception:
                pass
        self._writer_task = None

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except Exception:
                pass
        self._stderr_task = None

        if self._group_call is not None:
            try:
                stop_fn = getattr(self._group_call, "stop", None)
                if callable(stop_fn):
                    await self._maybe(stop_fn())
                else:
                    leave_fn = getattr(self._group_call, "leave_current_group_call", None)
                    if callable(leave_fn):
                        await self._maybe(leave_fn())
            except Exception as e:
                logger.warning("group_call_stop_failed: %s", e)
        self._group_call = None

        if self._ffmpeg is not None:
            try:
                if self._ffmpeg.stdin:
                    try:
                        self._ffmpeg.stdin.close()
                    except Exception:
                        pass
                if self._ffmpeg.returncode is None:
                    self._ffmpeg.terminate()
                    try:
                        await asyncio.wait_for(self._ffmpeg.wait(), timeout=8)
                    except Exception:
                        self._ffmpeg.kill()
                        try:
                            await self._ffmpeg.wait()
                        except Exception:
                            pass
            except Exception as e:
                logger.warning("ffmpeg_stop_failed: %s", e)
        self._ffmpeg = None

        if self._client is not None:
            try:
                await self._maybe(self._client.disconnect())
            except Exception as e:
                logger.warning("telegram_client_disconnect_failed: %s", e)
        self._client = None

        while not self._audio_queue.empty():
            try:
                _ = self._audio_queue.get_nowait()
            except Exception:
                break

    async def start(self, chat_id: int, source_url: str, title: str = "") -> BridgeResult:
        async with self._lock:
            try:
                if self.active:
                    await self._hard_stop()

                if not self._telegram_env_ok():
                    return BridgeResult(ok=False, error="missing_telegram_env", state=self._state.copy())

                rtmp_url = self._rtmp_target()
                if not rtmp_url:
                    return BridgeResult(
                        ok=False,
                        error="missing_tiktok_rtmp",
                        state=self._state.copy(),
                    )

                self._loop = asyncio.get_running_loop()
                await self._ensure_client()
                self._group_call = self._build_group_call()
                self._ffmpeg = await self._spawn_ffmpeg(rtmp_url)

                self._chat_id = int(chat_id)
                self._source_url = str(source_url or "").strip().rstrip("/")
                self._title = str(title or "").strip()

                await self._group_call.start(self._chat_id, enable_action=False)

                self.active = True
                self._state = {
                    "status": "playing",
                    "bridge": True,
                    "chat_id": self._chat_id,
                    "source_url": self._source_url,
                    "title": self._title,
                    "rtmp_ready": True,
                    "error": "",
                }

                self._writer_task = asyncio.create_task(self._writer_loop())
                self._stderr_task = asyncio.create_task(self._read_ffmpeg_stderr())

                return BridgeResult(ok=True, state=self._state.copy())
            except Exception as e:
                await self._hard_stop()
                logger.exception("bridge_start_failed")
                self._state = {
                    "status": "error",
                    "bridge": False,
                    "chat_id": int(chat_id),
                    "source_url": str(source_url or "").strip(),
                    "title": str(title or "").strip(),
                    "error": f"{type(e).__name__}: {e}",
                }
                return BridgeResult(ok=False, error=f"{type(e).__name__}: {e}", state=self._state.copy())

    async def stop(self, chat_id: int | None = None) -> BridgeResult:
        async with self._lock:
            try:
                await self._hard_stop()
                self._state = {
                    "status": "stopped",
                    "bridge": False,
                    "chat_id": int(chat_id or self._chat_id or 0),
                    "error": "",
                }
                return BridgeResult(ok=True, state=self._state.copy())
            except Exception as e:
                logger.exception("bridge_stop_failed")
                self._state = {
                    "status": "error",
                    "bridge": False,
                    "chat_id": int(chat_id or self._chat_id or 0),
                    "error": f"{type(e).__name__}: {e}",
                }
                return BridgeResult(ok=False, error=f"{type(e).__name__}: {e}", state=self._state.copy())

    async def enable_bridge(self, chat_id: int, source_url: str, title: str = "") -> BridgeResult:
        return await self.start(chat_id=chat_id, source_url=source_url, title=title)

    async def disable_bridge(self, chat_id: int | None = None) -> BridgeResult:
        return await self.stop(chat_id=chat_id)

    async def state(self) -> dict[str, Any]:
        async with self._lock:
            if self._ffmpeg and self._ffmpeg.returncode is not None and self.active:
                self.active = False
                self._state["status"] = "stopped"
                self._state["bridge"] = False
            snap = self._state.copy()
            snap["active"] = bool(self.active)
            return snap

    async def close(self) -> None:
        await self._hard_stop()