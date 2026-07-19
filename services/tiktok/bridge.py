from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections import defaultdict
from typing import Any

from telethon import TelegramClient

try:
    from pytgcalls import GroupCallFactory
except Exception:
    try:
        from py_tgcalls import GroupCallFactory
    except Exception:
        GroupCallFactory = None

from config import settings
from session import sessions

log = logging.getLogger("tiktok.bridge")
cfg = settings()


class TelegramToTikTokBridge:
    """
    المكتبة الحديثة
    تنقل صوت مكالمة تليجرام إلى بث تيك توك (RTMP)
    """

    def __init__(self, client: TelegramClient) -> None:
        if GroupCallFactory is None:
            raise RuntimeError("pytgcalls_unavailable")

        self.client = client
        self.factory = GroupCallFactory(client)
        self._calls: dict[int, Any] = {}
        self._ffmpeg: dict[int, subprocess.Popen | None] = {}
        self._queues: dict[int, asyncio.Queue] = {}
        self._tasks: dict[int, list[asyncio.Task]] = defaultdict(list)
        self._locks: dict[int, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def _lock(self, chat_id: int) -> asyncio.Lock:
        chat_id = int(chat_id)
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def state(self, chat_id: int) -> dict[str, Any]:
        s = sessions.get(chat_id)
        return {
            "enabled": s.bridge.enabled,
            "running": s.bridge.running,
            "ffmpeg_pid": s.bridge.ffmpeg_pid,
            "audio_frames": s.bridge.audio_frames,
            "last_error": s.bridge.last_error,
            "started_at": int(s.bridge.started_at or 0),
        }

    def _make_group_call(self, chat_id: int):
        def on_played_data(_call, length: int) -> bytes:
            return b"\x00" * max(0, int(length or 0))

        def on_recorded_data(_call, frame: bytes, length: int) -> None:
            if not self._loop:
                return
            s = sessions.get(chat_id)
            if not s.bridge.enabled or not s.bridge.running:
                return
            try:
                self._loop.call_soon_threadsafe(self._enqueue, chat_id, frame)
            except Exception:
                pass

        return self.factory.get_raw_group_call(
            on_played_data=on_played_data,
            on_recorded_data=on_recorded_data,
            enable_logs_to_console=False,
            outgoing_audio_bitrate_kbit=cfg.audio_bitrate or 128,
        )

    def _enqueue(self, chat_id: int, frame: bytes) -> None:
        q = self._queues.get(chat_id)
        if not q:
            return
        try:
            q.put_nowait(frame)
            s = sessions.get(chat_id)
            s.bridge.audio_frames += 1
            s.bridge.last_packet_at = time.time()
            s.touch()
        except asyncio.QueueFull:
            pass

    async def enable(
        self,
        *,
        chat_id: int,
        rtmp_url: str,
        title: str = "TikTok Live",
        join_as: Any = None,
        invite_hash: str | None = None,
    ) -> dict[str, Any]:

        async with self._lock(chat_id):
            s = sessions.get(chat_id)

            if s.bridge.running:
                s.bridge.enabled = True
                s.touch()
                return {"ok": True, "state": s.public()}

            if not rtmp_url or not rtmp_url.startswith("rtmp"):
                return {"ok": False, "error": "missing_or_invalid_rtmp_url"}

            self._loop = asyncio.get_running_loop()
            self._queues[chat_id] = asyncio.Queue(maxsize=3000)

            s.bridge.enabled = True
            s.bridge.running = True
            s.bridge.last_error = ""
            s.bridge.started_at = time.time()
            s.bridge.audio_frames = 0
            s.rtmp_url = rtmp_url
            s.touch()

            try:
                # ffmpeg: PCM → AAC → RTMP
                cmd = [
                    cfg.ffmpeg_bin or "ffmpeg",
                    "-hide_banner",
                    "-loglevel", "warning",
                    "-f", "s16le",
                    "-ar", "48000",
                    "-ac", "2",
                    "-i", "pipe:0",
                    "-c:a", "aac",
                    "-b:a", f"{cfg.audio_bitrate or 128}k",
                    "-f", "flv",
                    rtmp_url,
                ]

                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                )
                self._ffmpeg[chat_id] = proc
                s.bridge.ffmpeg_pid = proc.pid or 0

                # مهمة ضخ الصوت
                async def pump():
                    while s.bridge.running:
                        try:
                            chunk = await self._queues[chat_id].get()
                            if proc.stdin and chunk:
                                proc.stdin.write(chunk)
                                proc.stdin.flush()
                        except Exception as e:
                            s.bridge.last_error = str(e)
                            break

                self._tasks[chat_id].append(asyncio.create_task(pump()))

                # نستخدم raw group call فقط لالتقاط الصوت
                gc = self._make_group_call(chat_id)
                self._calls[chat_id] = gc

                await gc.start(chat_id, join_as=join_as, invite_hash=invite_hash)

                log.info("[BRIDGE] Enabled Telegram audio → TikTok | chat_id=%s", chat_id)
                return {"ok": True, "state": s.public()}

            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                s.bridge.last_error = err
                s.bridge.running = False
                s.bridge.enabled = False
                s.touch()
                await self.disable(chat_id)
                return {"ok": False, "error": err}

    async def disable(self, chat_id: int) -> dict[str, Any]:
        async with self._lock(chat_id):
            s = sessions.get(chat_id)
            s.bridge.enabled = False
            s.bridge.running = False
            s.touch()

            for task in self._tasks.pop(chat_id, []):
                task.cancel()

            proc = self._ffmpeg.pop(chat_id, None)
            if proc:
                try:
                    proc.terminate()
                    await asyncio.to_thread(proc.wait, 4)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            gc = self._calls.pop(chat_id, None)
            if gc:
                try:
                    stop_fn = getattr(gc, "stop", None) or getattr(gc, "leave", None)
                    if stop_fn:
                        maybe = stop_fn()
                        if asyncio.iscoroutine(maybe):
                            await maybe
                except Exception:
                    pass

            self._queues.pop(chat_id, None)
            s.bridge.ffmpeg_pid = 0
            s.touch()
            return {"ok": True, "state": s.public()}


bridge: TelegramToTikTokBridge | None = None


def create_bridge(client: TelegramClient) -> TelegramToTikTokBridge:
    global bridge
    if bridge is None:
        bridge = TelegramToTikTokBridge(client)
    return bridge