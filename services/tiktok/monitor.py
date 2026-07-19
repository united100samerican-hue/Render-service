from __future__ import annotations
import asyncio
import logging
import time
from typing import Any

from config import settings

log = logging.getLogger("tiktok.monitor")

cfg = settings()


class StreamMonitor:

    def __init__(
        self,
        receiver: Any,
        player: Any,
        bridge: Any,
    ) -> None:

        self.receiver = receiver
        self.player = player
        self.bridge = bridge

        self.running = False

        self.tasks: dict[int, asyncio.Task] = {}

    async def start(
        self,
        chat_id: int,
        source_url: str,
    ) -> None:

        if chat_id in self.tasks:
            return

        self.tasks[chat_id] = asyncio.create_task(
            self._loop(
                chat_id,
                source_url,
            )
        )

    async def stop(
        self,
        chat_id: int,
    ) -> None:

        task = self.tasks.pop(chat_id, None)

        if task:

            task.cancel()

            try:
                await task
            except Exception:
                pass

    async def stop_all(self) -> None:

        for chat_id in list(self.tasks):

            await self.stop(chat_id)

    async def _loop(
        self,
        chat_id: int,
        source_url: str,
    ) -> None:

        while True:

            try:

                await asyncio.sleep(cfg.MONITOR_INTERVAL)

                player_state = self.player.state(chat_id)

                if not player_state.get("running", False):

                    log.warning(
                        "Player stopped %s",
                        chat_id,
                    )

                    result = await self.receiver.refresh(
                        chat_id=chat_id,
                        url=source_url,
                    )

                    if result.get("ok"):

                        await self.player.restart(
                            chat_id=chat_id,
                            video_url=result["video_url"],
                            audio_url=result["audio_url"],
                        )

                        continue

                bridge_state = self.bridge.state(chat_id)

                if bridge_state.get("bridge_enabled", False):

                    if not bridge_state.get("active", False):

                        log.warning(
                            "Restart bridge %s",
                            chat_id,
                        )

                        await self.bridge.enable_bridge(

                            chat_id=chat_id,

                            rtmp_url=bridge_state.get(
                                "rtmp_url",
                                "",
                            ),

                            title=bridge_state.get(
                                "title",
                                "TikTok Live",
                            ),

                            join_as=bridge_state.get(
                                "join_as",
                            ),

                            invite_hash=bridge_state.get(
                                "invite_hash",
                            ),

                        )

            except asyncio.CancelledError:

                break

            except Exception as e:

                log.exception(
                    "Monitor error %s",
                    e,
                )

                await asyncio.sleep(
                    cfg.RECONNECT_DELAY
                )


monitor: StreamMonitor | None = None


def create_monitor(
    receiver: Any,
    player: Any,
    bridge: Any,
) -> StreamMonitor:

    global monitor

    if monitor is None:

        monitor = StreamMonitor(

            receiver,

            player,

            bridge,

        )

    return monitor
