from __future__ import annotations
from typing import Any
from pytgcalls import PyTgCalls
from pytgcalls.types import MediaStream
from pytgcalls.exceptions import NoActiveGroupCall,NotInCallError
class CallManager:
    def __init__(self,client:Any):
        self.client=client
        self.calls=PyTgCalls(client)
    async def start(self)->None:
        result=self.calls.start()
        if hasattr(result,'__await__'):await result
    async def active(self,chat_id:int)->bool:
        wrapper=getattr(self.calls,'_app',None)
        if wrapper is None:return False
        bind=getattr(wrapper,'_bind_client',None)
        getter=getattr(bind,'get_call',None) if bind is not None else None
        if getter is not None:
            try:return await getter(int(chat_id)) is not None
            except(NoActiveGroupCall,NotInCallError):return False
        getter=getattr(wrapper,'get_input_call',None)
        if getter is None:return False
        try:return await getter(int(chat_id)) is not None
        except(NoActiveGroupCall,NotInCallError):return False
    async def play(self,chat_id:int,source:str,video:bool=False,offset:int=0)->None:
        if not await self.active(chat_id):raise NoActiveGroupCall()
        stream=MediaStream(str(source),ffmpeg_parameters=f'-ss {max(0,int(offset))}' if int(offset)>0 else '')
        await self.calls.play(int(chat_id),stream)
    async def pause(self,chat_id:int)->None:
        await self.calls.pause(int(chat_id))
    async def resume(self,chat_id:int)->None:
        await self.calls.resume(int(chat_id))
    async def set_muted(self,chat_id:int,muted:bool)->None:
        await self.calls.change_volume_call(int(chat_id),0 if muted else 100)
    async def stop(self,chat_id:int)->None:
        try:await self.calls.leave_call(int(chat_id))
        except(NoActiveGroupCall,NotInCallError):return
    async def seek(self,chat_id:int,source:str,position:int)->None:
        if not await self.active(chat_id):raise NoActiveGroupCall()
        stream=MediaStream(str(source),ffmpeg_parameters=f'-ss {max(0,int(position))}')
        await self.calls.play(int(chat_id),stream)
