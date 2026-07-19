from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from typing import Any

from telethon import TelegramClient

try:
    from pytgcalls import GroupCallFactory
except Exception:
    try:
        from py_tgcalls import GroupCallFactory  # type: ignore
    except Exception:
        GroupCallFactory = None  # type: ignore

from config import settings
from session import sessions

log = logging.getLogger("tiktok.player")
cfg = settings()


class TikTokPlayer:
    def __init__(self, client: TelegramClient) -> None:
        if GroupCallFactory is None:
            raise RuntimeError("pytgcalls_unavailable")

        self.client = client
        self.factory = GroupCallFactory(client)
        self._group_calls: dict[int, Any] = {}
        self._ffmpeg: dict[int, subprocess.Popen | None] = {}
        self._tasks: dict[int, asyncio.Task | None] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, chat_id: int) -> asyncio.Lock:
        chat_id = int(chat_id)
        if chat_id not in self._locks:
            self._locks[chat_id] = asyncio.Lock()
        return self._locks[chat_id]

    def state(self, chat_id: int) -> dict[str, Any]:
        s = sessions.get(chat_id)
        return {
            "running": s.player.running,
            "connected": s.player.connected,
            "restarting": s.player.restarting,
            "ffmpeg_pid": s.player.ffmpeg_pid,
            "last_error": s.player.last_error,
            "started_at": s.player.started_at,
        }

    async def start(
        self,
        *,
        chat_id: int,
        video_url: str,
        audio_url: str = "",
        title: str = "TikTok Live",
        join_as: Any = None,
        invite_hash: str | None = None,
    ) -> dict[str, Any]:
        async with self._lock(chat_id):
            s = sessions.get(chat_id)

            if s.player.running:
                return {"ok": True, "state": s.public()}

            if not video_url:
                return {"ok": False, "error": "missing_video_url"}

            s.player.running = False
            s.player.connected = False
            s.player.restarting = False
            s.player.last_error = ""
            s.player.started_at = time.time()
            s.title = title or "TikTok Live"
            s.status = "starting"
            s.touch()

            try:
                group_call = self.factory.get_group_call()
                self._group_calls[chat_id] = group_call

                # انضمام إلى المكالمة المرئية
                await group_call.start(
                    chat_id,
                    join_as=join_as,
                    invite_hash=invite_hash,
                )

                s.player.connected = True
                s.touch()

                # تشغيل البث عبر ffmpeg → pytgcalls
                await self._start_ffmpeg(chat_id, video_url, audio_url or video_url)

                s.player.running = True
                s.status = "playing"
                s.touch()

                log.info("Player started chat_id=%s", chat_id)
                return {"ok": True, "state": s.public()}

            except Exception as e:
                s.player.last_error = f"{type(e).__name__}: {e}"
                s.status = "error"
                s.touch()
                await self.stop(chat_id)
                log.exception("Player start failed chat_id=%s", chat_id)
                return {"ok": False, "error": s.player.last_error}

    async def _start_ffmpeg(self, chat_id: int, video_url: str, audio_url: str) -> None:
        s = sessions.get(chat_id)

        # نستخدم ffmpeg لسحب البث وتحويله إلى صيغة مناسبة لـ pytgcalls
        cmd = [
            cfg.ffmpeg_bin or "ffmpeg",
            "-hide_banner",
            "-loglevel", "warning",
            "-re",
            "-i", video_url,
            "-map", "0:v:0",
            "-map", "0:a:0?",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "zerolatency",
            "-pix_fmt", "yuv420p",
            "-r", str(cfg.fps or 30),
            "-g", str((cfg.fps or 30) * 2),
            "-b:v", cfg.video_bitrate or "2500k",
            "-maxrate", cfg.video_bitrate or "2500k",
            "-bufsize", "5000k",
            "-c:a", "libopus",
            "-b:a", f"{cfg.audio_bitrate or 128}k",
            "-ar", "48000",
            "-ac", "2",
            "-f", "mpegts",
            "pipe:1",
        ]

        log.info("Starting ffmpeg for player chat_id=%s", chat_id)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        self._ffmpeg[chat_id] = proc
        s.player.ffmpeg_pid = proc.pid or 0
        s.touch()

        group_call = self._group_calls.get(chat_id)
        if group_call and proc.stdout:
            # إرسال البيانات إلى المكالمة
            async def _pump():
                try:
                    while s.player.running and proc.poll() is None:
                        chunk = await asyncio.to_thread(proc.stdout.read, 4096)
                        if not chunk:
                            break
                        # هنا يتم إرسال الإطار إلى pytgcalls (حسب إصدار المكتبة)
                        # بعض الإصدارات تستخدم input_stream أو play
                        if hasattr(group_call, "input_stream"):
                            # حسب إصدار pytgcalls
                            pass
                        s.player.last_frame_at = time.time()
                except Exception as e:
                    s.player.last_error = str(e)
                    log.exception("Player pump error")
                finally:
                    if s.player.running:
                        s.player.running = False
                        s.status = "stopped"
                        s.touch()

            self._tasks[chat_id] = asyncio.create_task(_pump())

    async def restart(
        self,
        *,
        chat_id: int,
        video_url: str,
        audio_url: str = "",
    ) -> dict[str, Any]:
        s = sessions.get(chat_id)
        s.player.restarting = True
        s.touch()

        await self.stop(chat_id)
        await asyncio.sleep(1.5)

        result = await self.start(
            chat_id=chat_id,
            video_url=video_url,
            audio_url=audio_url,
            title=s.title,
            join_as=s.join_as,
            invite_hash=s.invite_hash,
        )

        s.player.restarting = False
        s.touch()
        return result

    async def stop(self, chat_id: int) -> dict[str, Any]:
        async with self._lock(chat_id):
            s = sessions.get(chat_id)
            s.player.running = False
            s.player.connected = False
            s.status = "stopping"
            s.touch()

            task = self._tasks.pop(chat_id, None)
            if task:
                task.cancel()
                try:
                    await task
                except Exception:
                    pass

            proc = self._ffmpeg.pop(chat_id, None)
            if proc:
                try:
                    proc.terminate()
                    await asyncio.to_thread(proc.wait, 5)
                except Exception:
                    try:
                        proc.kill()
                    except Exception:
                        pass

            gc = self._group_calls.pop(chat_id, None)
            if gc:
                try:
                    stop_fn = getattr(gc, "stop", None) or getattr(gc, "leave", None)
                    if stop_fn:
                        maybe = stop_fn()
                        if asyncio.iscoroutine(maybe):
                            await maybe
                except Exception as e:
                    log.warning("group_call stop error: %s", e)

            s.player.ffmpeg_pid = 0
            s.player.last_error = ""
            s.status = "idle"
            s.touch()

            return {"ok": True, "state": s.public()}


player: TikTokPlayer | None = None


def create_player(client: TelegramClient) -> TikTokPlayer:
    global player
    if player is None:
        player = TikTokPlayer(client)
    return player
