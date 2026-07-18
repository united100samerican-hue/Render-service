from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import pytgcalls

log = logging.getLogger("tiktok.bridge")


@dataclass
class BridgeState:
    chat_id: int
    title: str = ""
    rtmp_url: str = ""
    mode: str = "bridge_audio"
    status: str = "idle"
    started_at: float = 0.0
    last_seen_at: float = 0.0
    frames_in: int = 0
    bytes_in: int = 0
    ffmpeg_pid: int = 0
    last_error: str = ""
    active: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def public(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "rtmp_url": self.rtmp_url,
            "mode": self.mode,
            "status": self.status,
            "started_at": self.started_at,
            "last_seen_at": self.last_seen_at,
            "frames_in": self.frames_in,
            "bytes_in": self.bytes_in,
            "ffmpeg_pid": self.ffmpeg_pid,
            "last_error": self.last_error,
            "active": self.active,
            "metadata": self.metadata,
        }


class TelegramToTikTokBridge:
    """
    Telegram voice chat -> TikTok RTMP bridge.

    Design:
    - Join Telegram group call using pytgcalls GroupCallRaw.
    - Receive PCM 16-bit audio in on_recorded_data.
    - Feed raw PCM to ffmpeg through stdin.
    - ffmpeg outputs black video + captured audio to TikTok RTMP.

    Required env or payload:
    - TIKTOK_RTMP_URL (preferred)
    """

    def __init__(
        self,
        client,
        *,
        ffmpeg_bin: str = "ffmpeg",
        video_size: str = "1280x720",
        fps: int = 30,
        audio_bitrate_kbps: int = 128,
        output_sample_rate: int = 48000,
        output_channels: int = 2,
    ) -> None:
        self.client = client
        self.ffmpeg_bin = ffmpeg_bin
        self.video_size = video_size
        self.fps = int(fps)
        self.audio_bitrate_kbps = int(audio_bitrate_kbps)
        self.output_sample_rate = int(output_sample_rate)
        self.output_channels = int(output_channels)

        self._factory = pytgcalls.GroupCallFactory(
            client,
            pytgcalls.GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON,
        )

        self._lock = asyncio.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[bytes] | None = None
        self._writer_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._group_call = None
        self._ffmpeg = None
        self._state: dict[str, BridgeState] = {}

    def _get_state(self, chat_id: int) -> BridgeState:
        key = str(chat_id)
        if key not in self._state:
            self._state[key] = BridgeState(chat_id=chat_id)
        return self._state[key]

    def state(self, chat_id: int) -> dict[str, Any]:
        return self._get_state(chat_id).public()

    def _enqueue_audio(self, chat_id: int, frame: bytes, length: int) -> None:
        st = self._get_state(chat_id)
        payload = (frame or b"")[: max(0, int(length or 0))]
        if not payload:
            return

        st.frames_in += 1
        st.bytes_in += len(payload)
        st.last_seen_at = time.time()

        if not self._queue:
            return

        try:
            self._queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                _ = self._queue.get_nowait()
            except Exception:
                pass
            try:
                self._queue.put_nowait(payload)
            except Exception:
                pass

    async def _spawn_ffmpeg(self, rtmp_url: str) -> None:
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={self.video_size}:r={self.fps}",
            "-f",
            "s16le",
            "-ar",
            str(self.output_sample_rate),
            "-ac",
            str(self.output_channels),
            "-i",
            "pipe:0",
            "-shortest",
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
            f"{self.audio_bitrate_kbps}k",
            "-ar",
            str(self.output_sample_rate),
            "-ac",
            str(self.output_channels),
            "-f",
            "flv",
            rtmp_url,
        ]
        self._ffmpeg = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )

    async def _pump_ffmpeg(self, chat_id: int) -> None:
        if not self._ffmpeg or not self._ffmpeg.stdin or not self._queue:
            return
        try:
            while True:
                chunk = await self._queue.get()
                if chunk is None:
                    break
                self._ffmpeg.stdin.write(chunk)
                await self._ffmpeg.stdin.drain()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st = self._get_state(chat_id)
            st.last_error = f"ffmpeg_pump_error: {type(e).__name__}: {e}"
            st.status = "error"

    async def _drain_ffmpeg_stderr(self, chat_id: int) -> None:
        if not self._ffmpeg or not self._ffmpeg.stderr:
            return
        try:
            while True:
                line = await self._ffmpeg.stderr.readline()
                if not line:
                    break
                txt = line.decode("utf-8", "ignore").strip()
                if txt:
                    log.warning("[FFMPEG][%s] %s", chat_id, txt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ffmpeg stderr drain failed: %s", e)

    async def start(
        self,
        *,
        chat_id: int,
        rtmp_url: str,
        title: str = "",
        join_as: Any = None,
        invite_hash: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            st = self._get_state(chat_id)

            if st.active:
                return {"ok": True, "state": st.public()}

            if not rtmp_url or not str(rtmp_url).startswith("rtmp"):
                st.status = "error"
                st.last_error = "missing_tiktok_rtmp_url"
                return {"ok": False, "error": "missing_tiktok_rtmp_url"}

            self._loop = asyncio.get_running_loop()
            self._queue = asyncio.Queue(maxsize=1000)
            st.rtmp_url = rtmp_url
            st.title = title or "TikTok Live"
            st.mode = "bridge_audio"
            st.status = "starting"
            st.last_error = ""
            st.started_at = time.time()
            st.last_seen_at = st.started_at
            st.active = True

            def on_played_data(_call, length: int) -> bytes:
                return b"\x00" * max(0, int(length or 0))

            def on_recorded_data(_call, frame: bytes, length: int) -> None:
                if not self._loop:
                    return
                try:
                    self._loop.call_soon_threadsafe(self._enqueue_audio, chat_id, frame, length)
                except Exception:
                    pass

            self._group_call = self._factory.get_raw_group_call(
                on_played_data=on_played_data,
                on_recorded_data=on_recorded_data,
                enable_logs_to_console=False,
                outgoing_audio_bitrate_kbit=self.audio_bitrate_kbps,
            )

            try:
                await self._spawn_ffmpeg(rtmp_url)
                st.ffmpeg_pid = int(getattr(self._ffmpeg, "pid", 0) or 0)

                self._writer_task = asyncio.create_task(self._pump_ffmpeg(chat_id))
                self._stderr_task = asyncio.create_task(self._drain_ffmpeg_stderr(chat_id))

                await self._group_call.start(
                    chat_id,
                    join_as=join_as,
                    invite_hash=invite_hash,
                    enable_action=False,
                )

                st.status = "playing"
                st.last_seen_at = time.time()
                return {"ok": True, "state": st.public()}

            except Exception as e:
                st.status = "error"
                st.last_error = f"{type(e).__name__}: {e}"
                await self.stop(chat_id)
                return {"ok": False, "error": st.last_error}

    async def stop(self, chat_id: int) -> dict[str, Any]:
        async with self._lock:
            st = self._get_state(chat_id)
            st.active = False
            st.status = "stopping"

            if self._writer_task:
                self._writer_task.cancel()
                self._writer_task = None

            if self._stderr_task:
                self._stderr_task.cancel()
                self._stderr_task = None

            if self._queue:
                try:
                    self._queue.put_nowait(b"")
                except Exception:
                    pass
                self._queue = None

            try:
                if self._group_call:
                    stop_fn = getattr(self._group_call, "stop", None)
                    if callable(stop_fn):
                        await stop_fn()
                    else:
                        leave_fn = getattr(self._group_call, "leave_current_group_call", None)
                        if callable(leave_fn):
                            await leave_fn()
            except Exception as e:
                st.last_error = f"group_call_stop_error: {type(e).__name__}: {e}"

            self._group_call = None

            if self._ffmpeg:
                try:
                    if self._ffmpeg.stdin:
                        self._ffmpeg.stdin.close()
                except Exception:
                    pass
                try:
                    await asyncio.wait_for(self._ffmpeg.wait(), timeout=10)
                except Exception:
                    try:
                        self._ffmpeg.kill()
                    except Exception:
                        pass
                self._ffmpeg = None

            st.status = "idle"
            st.ffmpeg_pid = 0
            return {"ok": True, "state": st.public()}