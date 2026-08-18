from __future__ import annotations
import asyncio
import logging
import subprocess
import time
from pathlib import Path
from typing import Any
from pytgcalls import GroupCallFactory
from telethon import TelegramClient

logger=logging.getLogger('social_player')

class Player:
    def __init__(self,client:TelegramClient)->None:
        self.client=client
        self.factory=GroupCallFactory(client,GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON)
        self.group_call:Any=None
        self.chat_id:int|None=None
        self.base_source=''
        self.source=''

    async def start(self)->None:return None

    async def play(self,chat_id:int,source:str)->None:
        if not source:raise RuntimeError('missing_source')
        if self.group_call is not None and getattr(self.group_call,'is_connected',False):
            self.group_call.input_filename=source
        else:
            self.group_call=self.factory.get_file_group_call(source,play_on_repeat=False)
            try:await self.group_call.start(chat_id)
            except Exception as exc:
                self.group_call=None
                if 'GroupCallNotFound' in type(exc).__name__:raise RuntimeError('voice_chat_not_found')
                raise
        self.chat_id=chat_id;self.base_source=source;self.source=source

    async def pause(self,chat_id:int)->None:
        if not self.group_call or not getattr(self.group_call,'is_connected',False):raise RuntimeError('voice_call_not_connected')
        self.group_call.pause_playout()

    async def resume(self,chat_id:int)->None:
        if not self.group_call or not getattr(self.group_call,'is_connected',False):raise RuntimeError('voice_call_not_connected')
        self.group_call.resume_playout()

    async def seek(self,chat_id:int,delta:int,position:int|None=None,duration:int=0)->tuple[int,str]:
        if not self.group_call or not getattr(self.group_call,'is_connected',False):raise RuntimeError('voice_call_not_connected')
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
        self.group_call.input_filename=str(out);self.source=str(out)
        return target,str(out)

    async def leave(self,chat_id:int)->bool:
        if not self.group_call:return False
        try:await self.group_call.stop()
        finally:self.group_call=None;self.base_source='';self.source='';self.chat_id=None
        return True

    async def stop(self)->None:
        if self.group_call is not None:
            try:await self.group_call.stop()
            except Exception:pass
        self.group_call=None;self.base_source='';self.source='';self.chat_id=None
