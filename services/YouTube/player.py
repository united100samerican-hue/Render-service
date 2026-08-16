from __future__ import annotations
import asyncio
from telethon import TelegramClient
from pytgcalls import PyTgCalls
class Player:
    def __init__(self,client:TelegramClient):self.calls=PyTgCalls(client);self.started=False
    async def start(self):
        if self.started:return
        r=self.calls.start();await r if asyncio.iscoroutine(r) else None;self.started=True
    async def stop(self):
        if not self.started:return
        fn=getattr(self.calls,'stop',None)
        if callable(fn):
            r=fn();await r if asyncio.iscoroutine(r) else None
        self.started=False
    async def play(self,chat_id:int,source:str):
        r=self.calls.play(chat_id,source);await r if asyncio.iscoroutine(r) else None
    async def pause(self,chat_id:int):
        fn=getattr(self.calls,'pause',None)
        if not callable(fn):raise RuntimeError('pause_method_not_available')
        r=fn(chat_id);await r if asyncio.iscoroutine(r) else None
    async def resume(self,chat_id:int):
        fn=getattr(self.calls,'resume',None)
        if not callable(fn):raise RuntimeError('resume_method_not_available')
        r=fn(chat_id);await r if asyncio.iscoroutine(r) else None
    async def seek(self,chat_id:int,delta:int):
        fn=getattr(self.calls,'seek',None)
        if not callable(fn):raise RuntimeError('seek_method_not_available')
        r=fn(chat_id,int(delta));await r if asyncio.iscoroutine(r) else None
    async def leave(self,chat_id:int)->bool:
        fn=getattr(self.calls,'leave_call',None)
        if callable(fn):
            r=fn(chat_id);await r if asyncio.iscoroutine(r) else None;return True
        fn=getattr(self.calls,'leave_current_group_call',None)
        if callable(fn):
            r=fn();await r if asyncio.iscoroutine(r) else None;return True
        return False
