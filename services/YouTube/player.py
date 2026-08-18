from __future__ import annotations
import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Any
from pytgcalls import PyTgCalls
from pytgcalls.exceptions import NoActiveGroupCall
from pytgcalls.types import GroupCallConfig, MediaStream
from telethon import TelegramClient

logger=logging.getLogger('social_player')

class Player:
    def __init__(self,client:TelegramClient)->None:
        self.client=client
        self.calls:PyTgCalls|None=None
        self.chat_id:int|None=None
        self.base_source=''
        self.source=''

    async def start(self)->None:
        if self.calls is None:
            self.calls=PyTgCalls(self.client)
            self.calls.start()

    async def play(self,chat_id:int,source:str)->None:
        if not source:raise RuntimeError('missing_source')
        await self.start()
        if self.calls is None:raise RuntimeError('voice_call_not_initialized')
        try:
            await self.calls.play(chat_id,MediaStream(source),config=GroupCallConfig(auto_start=False))
        except NoActiveGroupCall as exc:
            raise RuntimeError('voice_chat_not_found') from exc
        self.chat_id=chat_id
        self.base_source=source
        self.source=source

    async def pause(self,chat_id:int)->None:
        if self.calls is None:raise RuntimeError('voice_call_not_connected')
        try:
            await self.calls.pause(chat_id)
        except Exception as exc:
            raise RuntimeError('voice_call_not_connected') from exc

    async def resume(self,chat_id:int)->None:
        if self.calls is None:raise RuntimeError('voice_call_not_connected')
        try:
            await self.calls.resume(chat_id)
        except Exception as exc:
            raise RuntimeError('voice_call_not_connected') from exc

    async def seek(self,chat_id:int,delta:int,position:int|None=None,duration:int=0)->tuple[int,str]:
        if self.calls is None:raise RuntimeError('voice_call_not_connected')
        if not self.base_source or not Path(self.base_source).is_file():raise RuntimeError('seek_source_unavailable')
        if position is None:raise RuntimeError('seek_position_required')
        target=max(0,int(position))
        if duration:target=min(target,int(duration))
        if duration and target>=duration:return target,''
        out=Path('/tmp')/f'social_seek_{chat_id}_{int(time.time()*1000)}.ogg'
        cmd=['ffmpeg','-y','-ss',str(target),'-i',self.base_source,'-vn','-acodec','copy',str(out)]
        p=await asyncio.to_thread(subprocess.run,cmd,capture_output=True,text=True,check=False,timeout=120)
        if p.returncode!=0:
            cmd=['ffmpeg','-y','-ss',str(target),'-i',self.base_source,'-vn','-c:a','libopus','-b:a','128k',str(out)]
            p=await asyncio.to_thread(subprocess.run,cmd,capture_output=True,text=True,check=False,timeout=120)
        if p.returncode!=0:raise RuntimeError('seek_transcode_failed')
        try:
            await self.calls.play(chat_id,MediaStream(str(out)),config=GroupCallConfig(auto_start=False))
        except NoActiveGroupCall as exc:
            raise RuntimeError('voice_chat_not_found') from exc
        self.source=str(out)
        return target,str(out)

    async def leave(self,chat_id:int)->bool:
        if self.calls is None or self.chat_id!=chat_id:return False
        try:
            await self.calls.leave_call(chat_id)
        except NoActiveGroupCall:
            pass
        finally:
            self.chat_id=None
            self.base_source=''
            self.source=''
        return True

    async def stop(self)->None:
        if self.calls is not None and self.chat_id is not None:
            try:
                await self.calls.leave_call(self.chat_id)
            except Exception:
                pass
        self.chat_id=None
        self.base_source=''
        self.source=''
