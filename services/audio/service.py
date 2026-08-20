from __future__ import annotations
import asyncio
import logging
import tempfile
import time
from pathlib import Path
from typing import Any
from telethon import TelegramClient
from telethon.errors import AuthKeyDuplicatedError
from telethon.sessions import StringSession
from call.manager import CallManager
from config import API_HASH,API_ID,BOT_TOKEN,SESSION_STRING
from errors import AudioServiceError
from media.telegram import TelegramMedia
from media.url import UrlResolver
from state.models import AudioSession
log=logging.getLogger('audio_service')
class AudioService:
    def __init__(self):
        self.ready=False
        self.backend_error=''
        self.client:TelegramClient|None=None
        self.calls:CallManager|None=None
        self.telegram_media:TelegramMedia|None=None
        self.urls=UrlResolver()
        self.sessions:dict[int,AudioSession]={}
        self.locks:dict[int,asyncio.Lock]={}
        self.ready_lock=asyncio.Lock()
        self.root=Path(tempfile.gettempdir())/'render_audio_media'
        self.root.mkdir(parents=True,exist_ok=True)
    def lock(self,chat_id:int)->asyncio.Lock:return self.locks.setdefault(int(chat_id),asyncio.Lock())
    def _now(self)->float:return time.time()
    async def ensure_ready(self):
        if self.ready:return
        async with self.ready_lock:
            if self.ready:return
            if not API_ID or not API_HASH or not SESSION_STRING:
                self.backend_error='missing_env: API_ID/API_HASH/AUDIO_SESSION_STRING';raise RuntimeError(self.backend_error)
            client=TelegramClient(StringSession(SESSION_STRING),API_ID,API_HASH)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    await client.disconnect()
                    self.backend_error='session_not_authorized'
                    raise RuntimeError(self.backend_error)
                calls=CallManager(client)
                await calls.start()
                media=TelegramMedia(BOT_TOKEN,client,self.root)
            except AuthKeyDuplicatedError as e:
                try:await client.disconnect()
                except Exception:pass
                self.backend_error='auth_key_duplicated'
                raise AudioServiceError('auth_key_duplicated',str(e)) from e
            except Exception as e:
                try:await client.disconnect()
                except Exception:pass
                self.backend_error=f'{type(e).__name__}: {e}'
                raise
            self.client=client
            self.calls=calls
            self.telegram_media=media
            self.ready=True
            self.backend_error=''
            log.info('ready')
    async def close(self):
        for chat_id in list(self.sessions):
            try:
                if self.calls:await self.calls.stop(chat_id)
            except Exception:log.exception('leave_on_shutdown_failed chat_id=%s',chat_id)
            s=self.sessions.pop(chat_id,None)
            if s:self._clean_file(s.local_path)
        if self.client:
            try:await self.client.disconnect()
            except Exception:pass
        self.client=None;self.calls=None;self.telegram_media=None;self.ready=False
    def state(self,chat_id:int)->dict[str,Any]:
        s=self.sessions.get(int(chat_id))
        return {'ok':True,'ready':self.ready,'active':bool(s and s.status in {'playing','paused'}),'state':s.to_dict() if s else {'chat_id':int(chat_id),'status':'idle'}}
    async def call_state(self,chat_id:int)->dict[str,Any]:
        await self.ensure_ready()
        if not self.calls:raise RuntimeError('call_backend_not_ready')
        try:active=await self.calls.active(int(chat_id))
        except Exception as e:
            msg=str(e)
            if 'GROUPCALL_INVALID' in msg or 'NoActiveGroupCall' in msg or 'No active group call' in msg:active=False
            else:raise
        return {'ok':True,'chat_id':int(chat_id),'active':bool(active)}
    async def _resolve(self,chat_id:int,source_type:str,source_id:str,title:str='',source_chat_id:int=0,source_message_id:int=0)->dict[str,Any]:
        st=str(source_type or '').strip().lower().replace('-','_')
        if st in {'url','link','youtube','yt'}:
            r=await self.urls.resolve(source_id);r.update(source_type='url',source_id=source_id);return r
        if st=='telegram_message':
            if not self.telegram_media:raise RuntimeError('telegram_media_not_ready')
            path,video,kind=await self.telegram_media.from_message(source_chat_id or chat_id,source_message_id,title)
            return {'source_type':st,'source_id':source_id,'stream_url':str(path),'title':title or path.stem,'duration':0,'webpage_url':'','thumbnail':'','video':video,'media_kind':kind,'local_path':str(path)}
        if st in {'telegram_audio','telegram_video','telegram_file_id','file_id'}:
            if not self.telegram_media:raise RuntimeError('telegram_media_not_ready')
            path,video,kind=await self.telegram_media.from_file_id(source_id,st,title)
            return {'source_type':st,'source_id':source_id,'stream_url':str(path),'title':title or path.stem,'duration':0,'webpage_url':'','thumbnail':'','video':video,'media_kind':kind,'local_path':str(path)}
        raise RuntimeError(f'unsupported_source_type: {source_type}')
    async def meta(self,chat_id:int,source_type:str,source_id:str,**kw)->dict[str,Any]:
        await self.ensure_ready()
        async with self.lock(chat_id):
            r=await self._resolve(chat_id,source_type,source_id,**kw)
            local=str(r.get('local_path') or '')
            try:
                return {'ok':True,'action':'meta','state':{'chat_id':chat_id,'source_type':str(r.get('source_type') or source_type),'source_id':str(r.get('source_id') or source_id),'title':str(r.get('title') or kw.get('title') or 'غير معروف'),'duration':int(r.get('duration') or kw.get('duration') or 0),'video':bool(r.get('video')),'media_kind':str(r.get('media_kind') or ('video' if r.get('video') else 'audio')),'webpage_url':str(r.get('webpage_url') or r.get('source_url') or source_id),'source_url':str(r.get('source_url') or source_id),'thumbnail':str(r.get('thumbnail') or ''),'live':bool(r.get('live',False))}}
            finally:
                if local:self._clean_file(local)
    def _clean_file(self,path:str):
        if path:
            try:Path(path).unlink(missing_ok=True)
            except Exception:pass
    async def _start_locked(self,chat_id:int,source_type:str,source_id:str,title:str='',duration:int=0,offset:int=0,source_chat_id:int=0,source_message_id:int=0)->dict[str,Any]:
        await self.ensure_ready()
        if not self.calls:raise RuntimeError('call_backend_not_ready')
        if not await self.calls.active(chat_id):raise AudioServiceError('no_active_call','no_active_call')
        r=await self._resolve(chat_id,source_type,source_id,title,source_chat_id,source_message_id)
        new_path=str(r.get('local_path') or '')
        old=self.sessions.get(chat_id)
        if old and old.local_path and old.local_path!=new_path:self._clean_file(old.local_path)
        stream=str(r.get('stream_url') or '')
        if not stream:
            self._clean_file(new_path)
            raise RuntimeError('stream_url_missing')
        try:
            await self.calls.play(chat_id,stream)
        except AuthKeyDuplicatedError as e:
            self._clean_file(new_path);self.backend_error='auth_key_duplicated';raise AudioServiceError('auth_key_duplicated',str(e)) from e
        except Exception as e:
            self._clean_file(new_path)
            msg=str(e)
            if 'NoActiveGroupCall' in msg or 'No active group call' in msg or 'GROUPCALL_INVALID' in msg:raise AudioServiceError('no_active_call','no_active_call') from e
            raise
        now=self._now()
        s=AudioSession(chat_id,status='playing',title=str(r.get('title') or title or source_id),source_type=str(r.get('source_type') or source_type),source_id=str(r.get('source_id') or source_id),source_chat_id=str(source_chat_id or ''),source_message_id=str(source_message_id or ''),source_url=str(r.get('source_url') or source_id if str(source_type)=='url' else ''),duration=int(r.get('duration') or duration or 0),position=max(0,int(offset or 0)),started_at=now-max(0,int(offset or 0)),video=bool(r.get('video')),media_kind=str(r.get('media_kind') or ('video' if r.get('video') else 'audio')),thumbnail=str(r.get('thumbnail') or ''),webpage_url=str(r.get('webpage_url') or source_id),local_path=new_path,updated_at=now)
        self.sessions[chat_id]=s
        return {'ok':True,'action':'start','state':s.to_dict()}
    async def start(self,chat_id:int,source_type:str,source_id:str,**kw):
        async with self.lock(chat_id):return await self._start_locked(chat_id,source_type,source_id,**kw)
    async def stop(self,chat_id:int):
        await self.ensure_ready()
        async with self.lock(chat_id):
            s=self.sessions.get(chat_id)
            error=None
            try:
                if self.calls:await self.calls.stop(chat_id)
            except Exception as e:
                error=e
            finally:
                if s:self._clean_file(s.local_path)
                self.sessions.pop(chat_id,None)
            if error:raise error
            return {'ok':True,'action':'stop','state':self.state(chat_id)}
    async def pause(self,chat_id:int):
        await self.ensure_ready()
        async with self.lock(chat_id):
            s=self.sessions.get(chat_id)
            if not s or s.status!='playing':return {'ok':False,'action':'pause','error':'no_active_audio','state':self.state(chat_id)}
            await self.calls.pause(chat_id);now=self._now();s.position=max(0,int(now-s.started_at));s.status='paused';s.paused_at=int(now);s.updated_at=now
            return {'ok':True,'action':'pause','state':s.to_dict()}
    async def resume(self,chat_id:int):
        await self.ensure_ready()
        async with self.lock(chat_id):
            s=self.sessions.get(chat_id)
            if not s or s.status!='paused':return {'ok':False,'action':'resume','error':'not_paused','state':self.state(chat_id)}
            await self.calls.resume(chat_id);now=self._now();s.started_at=now-s.position;s.status='playing';s.paused_at=0;s.updated_at=now
            return {'ok':True,'action':'resume','state':s.to_dict()}
    async def seek(self,chat_id:int,delta:int):
        await self.ensure_ready()
        async with self.lock(chat_id):
            s=self.sessions.get(chat_id)
            if not s or s.status not in {'playing','paused'}:return {'ok':False,'action':'seek','error':'no_active_audio','state':self.state(chat_id)}
            current=s.position if s.status=='paused' else max(0,int(self._now()-s.started_at));target=max(0,current+int(delta))
            if s.duration>0:target=min(target,s.duration)
            await self.calls.seek(chat_id,target);s.position=target;s.updated_at=self._now()
            if s.status=='playing':s.started_at=s.updated_at-target
            return {'ok':True,'action':'seek','position':target,'state':s.to_dict()}
    async def enqueue(self,chat_id,source_type,source_id,**kw):return {'ok':True,'action':'enqueue','state':self.state(chat_id)}
    async def queue_list(self,chat_id):return {'ok':True,'action':'queue_list','queue':[],'state':self.state(chat_id)}
    async def queue_clear(self,chat_id):return await self.stop(chat_id)
    async def skip(self,chat_id):return {'ok':False,'action':'skip','error':'worker_owns_playlist','state':self.state(chat_id)}
service=AudioService()
