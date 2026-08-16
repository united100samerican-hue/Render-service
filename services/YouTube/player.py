from __future__ import annotations

import asyncio
from typing import Any

from pytgcalls import PyTgCalls
from telethon import TelegramClient


class Player:
    def __init__(self, client: TelegramClient) -> None:
        self.calls = PyTgCalls(client)
        self.started = False

    async def _call(self, method: str, *args: Any) -> Any:
        fn = getattr(self.calls, method, None)
        if not callable(fn):
            raise RuntimeError(f"pytgcalls_method_not_available:{method}")
        result = fn(*args)
        return await result if asyncio.iscoroutine(result) else result

    async def start(self) -> None:
        if self.started:
            return
        await self._call("start")
        self.started = True

    async def stop(self) -> None:
        if not self.started:
            return
        try:
            fn = getattr(self.calls, "stop", None)
            if callable(fn):
                result = fn()
                if asyncio.iscoroutine(result):
                    await result
        finally:
            self.started = False

    async def play(self, chat_id: int, source: str) -> None:
        await self._call("play", chat_id, source)

    async def pause(self, chat_id: int) -> None:
        await self._call("pause", chat_id)

    async def resume(self, chat_id: int) -> None:
        await self._call("resume", chat_id)

    async def seek(self, chat_id: int, delta: int) -> None:
        await self._call("seek", chat_id, int(delta))

    async def leave(self, chat_id: int) -> bool:
        fn = getattr(self.calls, "leave_call", None)
        if callable(fn):
            result = fn(chat_id)
            if asyncio.iscoroutine(result):
                await result
            return True
        fn = getattr(self.calls, "leave_current_group_call", None)
        if callable(fn):
            result = fn()
            if asyncio.iscoroutine(result):
                await result
            return True
        return False
