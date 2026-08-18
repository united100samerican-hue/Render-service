from __future__ import annotations
import asyncio,os,time
import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
from .cleanup import Cleaner
from .config import Settings
from .errors import SocialMediaError
from .extractor import Extractor
from .models import MediaRequest,Session
from .player import Player
from .session import SessionStore

class SocialMediaService:
    def __init__(self,settings:Settings)->None:
        self.s=settings;self.extractor=Extractor(settings);self.cleaner=Cleaner();self.sessions=SessionStore();self.client:TelegramClient|None=None;self.player:Player|None=None;self.ready=False;self.backend_error=''
        self.audio_url=os.getenv('AUDIO_API_URL','').strip().rstrip('/');self.audio_secret=os.getenv('AUDIO_KEEPALIVE_SECRET',os.getenv('KEEPALIVE_SECRET','')).strip();self._owner_lock=asyncio.Lock()

    async def _wait_audio_release(self)->None:
        if not self.audio_url:return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                for _ in range(8):
                    r=await c.get(f'{self.audio_url}/health',headers={'x-keepalive-secret':self.audio_secret})
                    if not r.ok or not r.json().get('ready'):return
                    await asyncio.sleep(.5)
        except Exception:pass

    async def _release_audio(self)->None:
        if not self.audio_url:return
        try:
            async with httpx.AsyncClient(timeout=20) as c:await c.post(f'{self.audio_url}/release',headers={'x-keepalive-secret':self.audio_secret})
        except Exception:pass
        await self._wait_audio_release()

    async def _connect(self)->None:
        if not self.s.api_id or not self.s.api_hash or not self.s.session_string:raise RuntimeError('missing_env: API_ID/API_HASH/SESSION_STRING')
        if self.client is None:self.client=TelegramClient(StringSession(self.s.session_string),self.s.api_id,self.s.api_hash)
        if not self.client.is_connected():await self.client.connect()
        if not await self.client.is_user_authorized():raise RuntimeError('telegram_session_not_authorized')
        if self.player is None:self.player=Player(self.client)
        await self.player.start();self.ready=True;self.backend_error=''

    async def _ensure_owner(self)->None:
        async with self._owner_lock:
            if self.ready and self.client and self.client.is_connected() and self.player:return
            await self._release_audio();await self._connect();logger_msg='social_owner_acquired'

    async def ensure_ready(self)->None:
        if self.ready:return
        try:await self._ensure_owner()
        except Exception as exc:self.ready=False;self.backend_error=f'{type(exc).__name__}: {exc}'

    async def release(self)->dict:
        async with self._owner_lock:
            try:
                if self.player:await self.player.stop()
                if self.client:
                    try:await self.client.disconnect()
                    except Exception:pass
                self.player=None;self.client=None;self.ready=False;self.backend_error='released';return{'ok':True,'action':'release','released':True}
            except Exception as exc:self.ready=False;self.backend_error=f'{type(exc).__name__}: {exc}';return{'ok':False,'action':'release','error':type(exc).__name__,'detail':str(exc)}

    async def close(self)->None:
        for s in self.sessions.values():await self.cleaner.remove(s.local_path)
        self.sessions=SessionStore();await self.release()

    async def meta(self,request:MediaRequest)->dict:
        try:
            info=await self.extractor.inspect(request.url)
            return{'ok':True,'action':'meta','state':{'chat_id':request.chat_id,'title':info.title,'source_url':info.webpage_url,'duration':info.duration,'thumbnail':info.thumbnail,'extractor':info.extractor,'source_id':info.source_id,'video':info.has_video,'live':info.is_live,'has_audio':info.has_audio,'direct_available':bool(info.direct_url),'cookies_configured':bool(self.s.cookies_file and self.s.cookies_file.is_file())}}
        except Exception as exc:
            if isinstance(exc,SocialMediaError):return{'ok':False,'action':'meta','error':exc.code,'detail':exc.detail}
            return{'ok':False,'action':'meta','error':type(exc).__name__,'detail':str(exc)}

    async def _switch_locked(self,request:MediaRequest,action:str)->dict:
        info=await self.extractor.inspect(request.url);new_path='';previous=self.sessions.get(request.chat_id);previous_path=previous.local_path
        try:
            if info.is_live:
                if not info.direct_url:raise SocialMediaError('live_stream_unavailable')
                source=info.direct_url
            else:
                new_path=await self.extractor.download_vod(info);source=new_path
            await self._ensure_owner()
            if not self.player:return{'ok':False,'action':action,'error':'service_not_ready','detail':self.backend_error}
            await self.player.play(request.chat_id,source)
            session=Session(chat_id=request.chat_id,status='playing',title=info.title or request.title,source_url=info.webpage_url or request.url,duration=info.duration or request.duration,position=max(0,request.offset),thumbnail=info.thumbnail,extractor=info.extractor,source_id=info.source_id,video=info.has_video,live=info.is_live,local_path=new_path,direct_url=info.direct_url if info.is_live else '',updated_at=time.time(),error='')
            self.sessions.put(session)
            if previous_path and previous_path!=new_path:await self.cleaner.remove(previous_path)
            return{'ok':True,'action':action,'played':True,'switched':bool(previous.source_url),'state':session.to_dict()}
        except Exception as exc:
            if new_path:await self.cleaner.remove(new_path)
            previous.error=f'{type(exc).__name__}: {exc}';previous.updated_at=time.time();self.sessions.put(previous)
            if isinstance(exc,SocialMediaError):return{'ok':False,'action':action,'error':exc.code,'detail':exc.detail,'state':previous.to_dict()}
            return{'ok':False,'action':action,'error':type(exc).__name__,'detail':str(exc),'state':previous.to_dict()}

    async def start(self,request:MediaRequest)->dict:
        async with self.sessions.lock(request.chat_id):return await self._switch_locked(request,'start')
    async def next(self,request:MediaRequest)->dict:
        async with self.sessions.lock(request.chat_id):return await self._switch_locked(request,'next')
    async def skip(self,request:MediaRequest)->dict:
        async with self.sessions.lock(request.chat_id):return await self._switch_locked(request,'skip')

    async def pause(self,chat_id:int)->dict:
        await self._ensure_owner()
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            if not self.player or s.status not in {'playing','paused'}:return{'ok':False,'action':'pause','error':'no_active_media'}
            try:await self.player.pause(chat_id);s.status='paused';s.updated_at=time.time();self.sessions.put(s);return{'ok':True,'action':'pause','state':s.to_dict()}
            except Exception as exc:return{'ok':False,'action':'pause','error':type(exc).__name__,'detail':str(exc)}

    async def resume(self,chat_id:int)->dict:
        await self._ensure_owner()
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            if not self.player or s.status!='paused':return{'ok':False,'action':'resume','error':'not_paused'}
            try:await self.player.resume(chat_id);s.status='playing';s.updated_at=time.time();self.sessions.put(s);return{'ok':True,'action':'resume','state':s.to_dict()}
            except Exception as exc:return{'ok':False,'action':'resume','error':type(exc).__name__,'detail':str(exc)}

    async def seek(self,chat_id:int,delta:int,position:int|None=None)->dict:
        await self._ensure_owner()
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            if s.live:return{'ok':False,'action':'seek','error':'live_seek_unsupported'}
            if not self.player or s.status not in {'playing','paused'}:return{'ok':False,'action':'seek','error':'no_active_media'}
            target=max(0,int(s.position)+int(delta)) if position is None else max(0,int(position));target=min(target,s.duration) if s.duration else target
            if s.duration and target>=s.duration:return{'ok':True,'action':'seek','ended':True,'position':s.duration,'state':s.to_dict()}
            try:new_target,new_path=await self.player.seek(chat_id,int(delta),position=target,duration=s.duration)
            except Exception as exc:return{'ok':False,'action':'seek','error':type(exc).__name__,'detail':str(exc)}
            old=s.local_path;s.local_path=new_path;s.position=new_target;s.updated_at=time.time();self.sessions.put(s)
            if old:await self.cleaner.remove(old)
            return{'ok':True,'action':'seek','position':new_target,'state':s.to_dict()}

    async def stop(self,chat_id:int)->dict:
        async with self.sessions.lock(chat_id):
            s=self.sessions.get(chat_id)
            try:
                left=await self.player.leave(chat_id) if self.player else False
                if s.local_path:await self.cleaner.remove(s.local_path)
                self.sessions.remove(chat_id);await self.release();return{'ok':True,'action':'stop','stopped':True,'left_call':left}
            except Exception as exc:
                self.sessions.remove(chat_id);await self.release();return{'ok':False,'action':'stop','error':type(exc).__name__,'detail':str(exc)}

    def state(self,chat_id:int)->dict:
        s=self.sessions.get(chat_id);return{'ok':True,'chat_id':int(chat_id),'ready':self.ready,'backend_error':self.backend_error,'active_sessions':self.sessions.count_active(),'session':s.to_dict()}
