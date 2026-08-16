from __future__ import annotations
import time
from telethon import TelegramClient
from telethon.sessions import StringSession
from .cleanup import Cleaner
from .config import Settings
from .errors import SocialMediaError
from .extractor import Extractor
from .models import MediaRequest
from .player import Player
from .session import SessionStore
class SocialMediaService:
    def __init__(self,settings:Settings):self.s=settings;self.extractor=Extractor(settings);self.cleaner=Cleaner();self.sessions=SessionStore();self.client=None;self.player=None;self.ready=False;self.backend_error=''
    async def ensure_ready(self):
        if self.ready:return
        if not self.s.api_id or not self.s.api_hash or not self.s.session_string:self.backend_error='missing_env: API_ID/API_HASH/SESSION_STRING';return
        try:
            if self.client is None:self.client=TelegramClient(StringSession(self.s.session_string),self.s.api_id,self.s.api_hash)
            if not self.client.is_connected():await self.client.connect()
            if not self.client.is_user_authorized():self.backend_error='telegram_session_not_authorized';return
            if self.player is None:self.player=Player(self.client);await self.player.start()
            self.ready=True;self.backend_error=''
        except Exception as e:self.ready=False;self.backend_error=f'{type(e).__name__}: {e}'
    async def close(self):
        for s in self.sessions.values():await self.cleaner.remove(s.local_path)
        if self.player:await self.player.stop()
        if self.client:await self.client.disconnect()
        self.sessions=SessionStore();self.player=None;self.client=None;self.ready=False
    async def meta(self,r:MediaRequest):
        await self.ensure_ready()
        if not self.ready:return {'ok':False,'action':'meta','error':'service_not_ready','detail':self.backend_error}
        i=await self.extractor.inspect(r.url)
        return {'ok':True,'action':'meta','state':{'chat_id':r.chat_id,'title':i.title,'source_url':i.webpage_url,'duration':i.duration,'thumbnail':i.thumbnail,'extractor':i.extractor,'source_id':i.source_id,'video':i.has_video,'live':i.is_live,'has_audio':i.has_audio,'direct_available':bool(i.direct_url)}}
    async def _switch(self,r:MediaRequest,action:str):
        if not self.ready or not self.player:return {'ok':False,'action':action,'error':'service_not_ready','detail':self.backend_error}
        i=await self.extractor.inspect(r.url);old=self.sessions.get(r.chat_id);new_path=''
        try:
            if i.is_live:
                if not i.direct_url:raise SocialMediaError('live_stream_unavailable')
                source=i.direct_url
            else:new_path=await self.extractor.download_vod(i);source=new_path
            await self.player.play(r.chat_id,source)
            old_source=old.source_url
            old_path=old.local_path
            old.status='playing';old.title=i.title or r.title;old.source_url=i.webpage_url or r.url;old.duration=i.duration or r.duration;old.position=max(0,r.offset);old.thumbnail=i.thumbnail;old.extractor=i.extractor;old.source_id=i.source_id;old.video=i.has_video;old.live=i.is_live;old.local_path=new_path;old.direct_url=i.direct_url if i.is_live else '';old.updated_at=time.time();old.error='';self.sessions.put(old)
            if old_path and old_path!=new_path:await self.cleaner.remove(old_path)
            return {'ok':True,'action':action,'played':True,'switched':bool(old_source),'state':old.to_dict()}
        except Exception as e:
            if new_path:await self.cleaner.remove(new_path)
            old.error=f'{type(e).__name__}: {e}';old.updated_at=time.time();self.sessions.put(old)
            return {'ok':False,'action':action,'error':e.code,'detail':e.detail} if isinstance(e,SocialMediaError) else {'ok':False,'action':action,'error':type(e).__name__,'detail':str(e)}
    async def start(self,r):
        await self.ensure_ready()
        async with self.sessions.lock(r.chat_id):return await self._switch(r,'start')
    async def next(self,r):
        await self.ensure_ready()
        async with self.sessions.lock(r.chat_id):return await self._switch(r,'next')
    async def skip(self,r):
        await self.ensure_ready()
        async with self.sessions.lock(r.chat_id):return await self._switch(r,'skip')
    async def pause(self,chat_id):
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            if not self.player or s.status not in {'playing','paused'}:return {'ok':False,'action':'pause','error':'no_active_media'}
            try:await self.player.pause(chat_id);s.status='paused';s.updated_at=time.time();return {'ok':True,'action':'pause','state':s.to_dict()}
            except Exception as e:return {'ok':False,'action':'pause','error':type(e).__name__,'detail':str(e)}
    async def resume(self,chat_id):
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            if not self.player or s.status!='paused':return {'ok':False,'action':'resume','error':'not_paused'}
            try:await self.player.resume(chat_id);s.status='playing';s.updated_at=time.time();return {'ok':True,'action':'resume','state':s.to_dict()}
            except Exception as e:return {'ok':False,'action':'resume','error':type(e).__name__,'detail':str(e)}
    async def seek(self,chat_id,delta,position=None):
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            if s.live:return {'ok':False,'action':'seek','error':'live_seek_unsupported'}
            if not self.player or s.status not in {'playing','paused'}:return {'ok':False,'action':'seek','error':'no_active_media'}
            try:
                await self.player.seek(chat_id,int(delta));target=max(0,int(position) if position is not None else int(s.position)+int(delta));s.position=min(target,s.duration) if s.duration else target;s.updated_at=time.time();return {'ok':True,'action':'seek','position':s.position,'state':s.to_dict()}
            except Exception as e:return {'ok':False,'action':'seek','error':type(e).__name__,'detail':str(e)}
    async def stop(self,chat_id):
        await self.ensure_ready()
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            try:
                left=await self.player.leave(chat_id) if self.player else False;await self.cleaner.remove(s.local_path);self.sessions.remove(chat_id);return {'ok':True,'action':'stop','stopped':True,'left_call':left}
            except Exception as e:self.sessions.remove(chat_id);return {'ok':False,'action':'stop','error':type(e).__name__,'detail':str(e)}
    def state(self,chat_id):return {'ok':True,'chat_id':int(chat_id),'ready':self.ready,'backend_error':self.backend_error,'session':self.sessions.get(chat_id).to_dict()}
