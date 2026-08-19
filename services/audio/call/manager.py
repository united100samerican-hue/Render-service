from __future__ import annotations
from typing import Any
from pytgcalls import PyTgCalls

class CallManager:
    def __init__(self, client: Any):
        self.client = client
        self.calls = PyTgCalls(client)

    async def start(self) -> None:
        result = self.calls.start()
        if hasattr(result, '__await__'):
            await result

    async def active(self, chat_id: int) -> bool:
        # Refresh PyTgCalls cache from Telegram before asking for the cached InputGroupCall.
        wrapper = getattr(self.calls, '_app', None)
        bind = getattr(wrapper, '_bind_client', None)
        getter = getattr(bind, 'get_call', None)
        if getter is not None:
            return await getter(int(chat_id)) is not None
        call = await wrapper.get_input_call(int(chat_id))
        return call is not None

    async def play(self, chat_id: int, source: str) -> None:
        # The active-call check is repeated here to protect against a race between
        # /call/state and /start. PyTgCalls then joins the existing call or switches
        # the current stream inside the same assistant session.
        if not await self.active(int(chat_id)):
            raise RuntimeError('NoActiveGroupCall')
        await self.calls.play(int(chat_id), source)

    async def pause(self, chat_id: int) -> None:
        await self.calls.pause(int(chat_id))

    async def resume(self, chat_id: int) -> None:
        await self.calls.resume(int(chat_id))

    async def stop(self, chat_id: int) -> None:
        try:
            await self.calls.leave_call(int(chat_id))
        except Exception:
            pass

    async def seek(self, chat_id: int, position: int) -> None:
        fn = getattr(self.calls, 'seek', None)
        if fn is None:
            raise RuntimeError('seek_not_supported_by_backend')
        result = fn(int(chat_id), max(0, int(position)))
        if hasattr(result, '__await__'):
            await result
