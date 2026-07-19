from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

try:
    from pytgcalls import GroupCallFactory
except Exception:
    try:
        from py_tgcalls import GroupCallFactory  # type: ignore
    except Exception:
        GroupCallFactory = None  # type: ignore

log = logging.getLogger("tiktok.bridge")


def _s(v: Any) -> str:
    return str(v or "").strip()


@dataclass
class TikTokSessionState:
    chat_id: int
    title: str = "TikTok Live"
    source_url: str = ""
    rtmp_url: str = ""
    mode: str = "live"
    status: str = "idle"
    last_error: str = ""
    active: bool = False
    bridge_enabled: bool = False
    started_at: float = 0.0
    last_seen_at: float = 0.0
    ffmpeg_pid: int = 0
    audio_frames: int = 0
    join_as: Any = None
    invite_hash: str | None = None

    def public(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "title": self.title,
            "source_url": self.source_url,
            "rtmp_url": self.rtmp_url,
            "mode": self.mode,
            "status": self.status,
            "last_error": self.last_error,
            "active": self.active,
            "bridge_enabled": self.bridge_enabled,
            "started_at": int(self.started_at),
            "last_seen_at": int(self.last_seen_at),
            "ffmpeg_pid": self.ffmpeg_pid,
            "viewers": 0,
            "duration": max(0, int(time.time() - self.started_at)) if self.started_at else 0,
        }


class TelegramToTikTokBridge:
    def __init__(
        self,
        client: Any,
        *,
        ffmpeg_bin: str = "ffmpeg",
        video_size: str = "1280x720",
        fps: int = 30,
        audio_bitrate_kbps: int = 128,
        output_sample_rate: int = 48000,
        output_channels: int = 2,
    ) -> None:
        if GroupCallFactory is None:
            raise RuntimeError("pytgcalls_unavailable")

        self.client = client
        self.ffmpeg_bin = ffmpeg_bin
        self.video_size = video_size
        self.fps = fps
        self.audio_bitrate_kbps = audio_bitrate_kbps
        self.output_sample_rate = output_sample_rate
        self.output_channels = output_channels

        self._factory = GroupCallFactory(client)
        self._states: dict[int, TikTokSessionState] = {}
        self._locks = defaultdict(asyncio.Lock)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: dict[int, asyncio.Queue[bytes]] = {}
        self._ffmpeg: dict[int, subprocess.Popen | None] = {}
        self._group_call: dict[int, Any] = {}
        self._writer_task: dict[int, asyncio.Task | None] = {}
        self._stderr_task: dict[int, asyncio.Task | None] = {}
        self._silence_task: dict[int, asyncio.Task | None] = {}

    def _get_state(self, chat_id: int) -> TikTokSessionState:
        chat_id = int(chat_id)
        if chat_id not in self._states:
            self._states[chat_id] = TikTokSessionState(chat_id=chat_id)
        return self._states[chat_id]

    def state(self, chat_id: int) -> dict[str, Any]:
        return self._get_state(chat_id).public()

    def _make_group_call(self, chat_id: int):
        state = self._get_state(chat_id)

        def on_played_data(_call, length: int) -> bytes:
            return b"\x00" * max(0, int(length or 0))

        def on_recorded_data(_call, frame: bytes, length: int) -> None:
            if not self._loop:
                return
            if not state.active or not state.bridge_enabled or state.mode != "bridge_audio":
                return
            try:
                self._loop.call_soon_threadsafe(self._enqueue_audio, chat_id, frame)
            except Exception:
                pass

        return self._factory.get_raw_group_call(
            on_played_data=on_played_data,
            on_recorded_data=on_recorded_data,
            enable_logs_to_console=False,
            outgoing_audio_bitrate_kbit=self.audio_bitrate_kbps,
        )

    def _enqueue_audio(self, chat_id: int, frame: bytes) -> None:
        q = self._queues.get(chat_id)
        if not q:
            return
        try:
            q.put_nowait(frame)
            st = self._get_state(chat_id)
            st.audio_frames += 1
            st.last_seen_at = time.time()
        except asyncio.QueueFull:
            pass

    def _silence_frame(self) -> bytes:
        bytes_per_second = self.output_sample_rate * self.output_channels * 2
        return b"\x00" * max(1, bytes_per_second // 10)

    async def _feed_silence(self, chat_id: int) -> None:
        st = self._get_state(chat_id)
        q = self._queues[chat_id]
        silence = self._silence_frame()
        while st.active:
            try:
                if q.qsize() < 2:
                    await q.put(silence)
            except Exception:
                pass
            await asyncio.sleep(0.1)

    async def _spawn_ffmpeg(self, chat_id: int, rtmp_url: str) -> None:
        cmd = [
            self.ffmpeg_bin,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-nostdin",
            "-f",
            "s16le",
            "-ar",
            str(self.output_sample_rate),
            "-ac",
            str(self.output_channels),
            "-i",
            "pipe:0",
            "-vn",
            "-c:a",
            "aac",
            "-b:a",
            f"{self.audio_bitrate_kbps}k",
            "-f",
            "flv",
            rtmp_url,
        ]
        log.info("[TT] spawn ffmpeg chat_id=%s cmd=%s", chat_id, cmd)
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self._ffmpeg[chat_id] = proc

    async def _pump_ffmpeg(self, chat_id: int) -> None:
        proc = self._ffmpeg.get(chat_id)
        q = self._queues.get(chat_id)
        if not proc or not proc.stdin or not q:
            return

        try:
            while True:
                st = self._get_state(chat_id)
                if not st.active:
                    break
                chunk = await q.get()
                if not chunk:
                    continue
                try:
                    proc.stdin.write(chunk)
                    proc.stdin.flush()
                except Exception as e:
                    st.last_error = f"{type(e).__name__}: {e}"
                    break
        except asyncio.CancelledError:
            raise
        except Exception as e:
            st = self._get_state(chat_id)
            st.last_error = f"{type(e).__name__}: {e}"
        finally:
            try:
                if proc and proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass

    async def _drain_ffmpeg_stderr(self, chat_id: int) -> None:
        proc = self._ffmpeg.get(chat_id)
        if not proc or not proc.stderr:
            return
        try:
            while True:
                line = await asyncio.to_thread(proc.stderr.readline)
                if not line:
                    break
                txt = line.decode("utf-8", "ignore").strip()
                if txt:
                    log.warning("[FFMPEG][%s] %s", chat_id, txt)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("ffmpeg stderr drain failed: %s", e)

    async def _start_session(
        self,
        *,
        chat_id: int,
        rtmp_url: str,
        title: str = "",
        join_as: Any = None,
        invite_hash: str | None = None,
        mode: str = "live",
        bridge_enabled: bool = False,
    ) -> dict[str, Any]:
        async with self._locks[int(chat_id)]:
            st = self._get_state(chat_id)

            if st.active:
                if mode == "bridge_audio":
                    st.mode = "bridge_audio"
                    st.bridge_enabled = True
                return {"ok": True, "state": st.public()}

            if not rtmp_url or not str(rtmp_url).startswith("rtmp"):
                st.status = "error"
                st.last_error = "missing_tiktok_rtmp_url"
                return {"ok": False, "error": "missing_tiktok_rtmp_url"}

            self._loop = asyncio.get_running_loop()
            self._queues[chat_id] = asyncio.Queue(maxsize=2000)
            st.title = title or "TikTok Live"
            st.source_url = ""
            st.rtmp_url = rtmp_url
            st.mode = mode
            st.status = "starting"
            st.last_error = ""
            st.active = True
            st.bridge_enabled = bridge_enabled
            st.started_at = time.time()
            st.last_seen_at = st.started_at
            st.join_as = join_as
            st.invite_hash = invite_hash

            try:
                self._group_call[chat_id] = self._make_group_call(chat_id)
                await self._spawn_ffmpeg(chat_id, rtmp_url)
                st.ffmpeg_pid = int(getattr(self._ffmpeg.get(chat_id), "pid", 0) or 0)

                self._writer_task[chat_id] = asyncio.create_task(self._pump_ffmpeg(chat_id))
                self._stderr_task[chat_id] = asyncio.create_task(self._drain_ffmpeg_stderr(chat_id))
                self._silence_task[chat_id] = asyncio.create_task(self._feed_silence(chat_id))

                await self._group_call[chat_id].start(
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

    async def start_live(
        self,
        *,
        chat_id: int,
        rtmp_url: str,
        title: str = "",
        join_as: Any = None,
        invite_hash: str | None = None,
    ) -> dict[str, Any]:
        return await self._start_session(
            chat_id=chat_id,
            rtmp_url=rtmp_url,
            title=title,
            join_as=join_as,
            invite_hash=invite_hash,
            mode="live",
            bridge_enabled=False,
        )

    async def enable_bridge(
        self,
        *,
        chat_id: int,
        rtmp_url: str,
        title: str = "",
        join_as: Any = None,
        invite_hash: str | None = None,
    ) -> dict[str, Any]:
        async with self._locks[int(chat_id)]:
            st = self._get_state(chat_id)
            if not st.active:
                return await self._start_session(
                    chat_id=chat_id,
                    rtmp_url=rtmp_url,
                    title=title,
                    join_as=join_as,
                    invite_hash=invite_hash,
                    mode="bridge_audio",
                    bridge_enabled=True,
                )

            st.mode = "bridge_audio"
            st.bridge_enabled = True
            st.title = title or st.title or "TikTok Live"
            st.rtmp_url = rtmp_url or st.rtmp_url
            st.last_seen_at = time.time()
            st.status = "playing"
            return {"ok": True, "state": st.public()}

    async def disable_bridge(self, chat_id: int) -> dict[str, Any]:
        async with self._locks[int(chat_id)]:
            st = self._get_state(chat_id)
            if not st.active:
                st.bridge_enabled = False
                st.mode = "live"
                return {"ok": True, "state": st.public()}

            st.mode = "live"
            st.bridge_enabled = False
            st.last_seen_at = time.time()
            st.status = "playing"
            return {"ok": True, "state": st.public()}

    async def start(
        self,
        *,
        chat_id: int,
        rtmp_url: str,
        title: str = "",
        join_as: Any = None,
        invite_hash: str | None = None,
        mode: str = "live",
    ) -> dict[str, Any]:
        if mode == "bridge_audio":
            return await self.enable_bridge(
                chat_id=chat_id,
                rtmp_url=rtmp_url,
                title=title,
                join_as=join_as,
                invite_hash=invite_hash,
            )
        return await self.start_live(
            chat_id=chat_id,
            rtmp_url=rtmp_url,
            title=title,
            join_as=join_as,
            invite_hash=invite_hash,
        )

    async def stop(self, chat_id: int) -> dict[str, Any]:
        async with self._locks[int(chat_id)]:
            st = self._get_state(chat_id)
            st.active = False
            st.bridge_enabled = False
            st.status = "stopping"

            for task_map in (self._silence_task, self._writer_task, self._stderr_task):
                task = task_map.get(chat_id)
                if task:
                    task.cancel()
                    task_map[chat_id] = None

            gc = self._group_call.get(chat_id)
            if gc:
                try:
                    stop_fn = getattr(gc, "stop", None) or getattr(gc, "leave", None) or getattr(gc, "disconnect", None)
                    if stop_fn:
                        maybe = stop_fn()
                        if asyncio.iscoroutine(maybe):
                            await maybe
                except Exception as e:
                    st.last_error = f"{type(e).__name__}: {e}"

            proc = self._ffmpeg.get(chat_id)
            if proc:
                try:
                    proc.terminate()
                    await asyncio.to_thread(proc.wait, 5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            self._ffmpeg[chat_id] = None
            self._group_call[chat_id] = None
            self._queues.pop(chat_id, None)

            st.status = "idle"
            st.last_seen_at = time.time()
            st.ffmpeg_pid = 0
            return {"ok": True, "state": st.public()}