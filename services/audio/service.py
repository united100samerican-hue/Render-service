from __future__ import annotations
import asyncio,logging,os,re,shutil,subprocess,tempfile,time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import httpx
from telethon import TelegramClient
from telethon.sessions import StringSession
try:
    from pytgcalls import GroupCallFactory
except Exception:
    GroupCallFactory=None

logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s: %(message)s')
logger=logging.getLogger('audio_service')
ALLOWED_SOURCE_TYPES={'telegram_file_id','telegram_audio','telegram_video','telegram_message'}
AUDIO_EXTS={'.mp3','.ogg','.oga','.wav','.m4a','.aac','.flac','.opus'}
VIDEO_EXTS={'.mp4','.mkv','.mov','.webm','.m4v','.avi'}

@dataclass
class AudioSession:
    chat_id:int
    status:str='idle'
    title:str=''
    source_type:str=''
    source_id:str=''
    source_chat_id:str=''
    source_message_id:str=''
    duration:int=0
    position:int=0
    paused:bool=False
    last_error:str=''
    local_path:str=''
    source_path:str=''
    video:bool=False
    updated_at:float=0.0

class AudioService:
    def __init__(self)->None:
        self.api_id=int(os.getenv('API_ID','0') or '0');self.api_hash=os.getenv('API_HASH','').strip();self.session_string=os.getenv('SESSION_STRING','').strip();self.bot_token=os.getenv('BOT_TOKEN','').strip()
        self.social_url=os.getenv('SOCIAL_MEDIA_API_URL','').strip().rstrip('/');self.social_secret=os.getenv('SOCIAL_MEDIA_KEEPALIVE_SECRET',os.getenv('KEEPALIVE_SECRET','')).strip()
        self.ready=False;self.backend_error='';self._client:TelegramClient|None=None;self.calls:Any=None;self._group_call:Any=None
        self._sessions:dict[int,AudioSession]={};self._locks:dict[int,asyncio.Lock]={};self._owner_lock=asyncio.Lock();self._download_dir=Path(tempfile.gettempdir())/'render_audio_service_media';self._download_dir.mkdir(parents=True,exist_ok=True)

    @staticmethod
    async def _maybe_await(value:Any)->Any:return await value if asyncio.iscoroutine(value) else value
    def _now(self)->float:return time.time()
    def _touch(self,s:AudioSession)->AudioSession:s.updated_at=self._now();return s
    def _lock_for(self,chat_id:int)->asyncio.Lock:return self._locks.setdefault(chat_id,asyncio.Lock())

    def _normalize_source_type(self,source_type:str)->str:
        raw=(source_type or '').strip().lower().replace('-','_');aliases={'telegram':'telegram_file_id','tg':'telegram_file_id','telegram_file':'telegram_file_id','telegram_file_id':'telegram_file_id','telegram_media':'telegram_file_id','telegram_document':'telegram_file_id','file':'telegram_file_id','document':'telegram_file_id','media':'telegram_file_id','audio':'telegram_audio','voice':'telegram_audio','song':'telegram_audio','music':'telegram_audio','telegram_audio':'telegram_audio','video':'telegram_video','clip':'telegram_video','movie':'telegram_video','telegram_video':'telegram_video','telegram_message':'telegram_message'};v=aliases.get(raw,raw or 'telegram_file_id')
        if v not in ALLOWED_SOURCE_TYPES:raise ValueError('unsupported_source_type')
        return v

    @staticmethod
    def _infer_video(source_type:str,file_name:str='',mime_type:str='')->bool:
        st=source_type.strip().lower();suffix=Path(file_name).suffix.lower()
        if st=='telegram_video':return True
        if st=='telegram_audio':return False
        if mime_type.startswith('video/'):return True
        if mime_type.startswith('audio/'):return False
        if suffix in VIDEO_EXTS:return True
        if suffix in AUDIO_EXTS:return False
        return False

    async def _http_get_json(self,url:str,params:dict[str,Any]|None=None)->dict[str,Any]:
        async with httpx.AsyncClient(timeout=120) as c:
            r=await c.get(url,params=params);r.raise_for_status();v=r.json()
            if not isinstance(v,dict):raise RuntimeError('invalid_json_response')
            return v

    async def _http_get_bytes(self,url:str)->bytes:
        async with httpx.AsyncClient(timeout=300) as c:
            r=await c.get(url);r.raise_for_status();return r.content

    async def _release_social(self)->None:
        if not self.social_url:return
        try:
            async with httpx.AsyncClient(timeout=20) as c:await c.post(f'{self.social_url}/release',headers={'x-keepalive-secret':self.social_secret})
        except Exception as exc:logger.warning('social_release_failed error=%s',exc)
        await self._wait_remote_release()

    async def _wait_remote_release(self)->None:
        if not self.social_url:return
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                for _ in range(8):
                    r=await c.get(f'{self.social_url}/health',headers={'x-keepalive-secret':self.social_secret})
                    if not r.ok or not r.json().get('ready'):return
                    await asyncio.sleep(.5)
        except Exception:pass

    async def _connect(self)->None:
        if not self.api_id or not self.api_hash or not self.session_string:raise RuntimeError('missing_env: API_ID/API_HASH/SESSION_STRING')
        if GroupCallFactory is None:raise RuntimeError('pytgcalls_group_call_factory_unavailable')
        if self._client is None:self._client=TelegramClient(StringSession(self.session_string),self.api_id,self.api_hash)
        if not self._client.is_connected():await self._client.connect()
        if not await self._client.is_user_authorized():raise RuntimeError('telegram_session_not_authorized')
        if self.calls is None:self.calls=GroupCallFactory(self._client,GroupCallFactory.MTPROTO_CLIENT_TYPE.TELETHON)
        self.ready=True;self.backend_error=''

    async def _ensure_owner(self)->None:
        async with self._owner_lock:
            if self.ready and self._client and self._client.is_connected() and self.calls:return
            await self._release_social();await self._connect();logger.info('audio_owner_acquired')

    async def ensure_ready(self)->None:
        if self.ready:return
        try:await self._ensure_owner()
        except Exception as exc:self.ready=False;self.backend_error=f'{type(exc).__name__}: {exc}';logger.exception('audio ensure_ready failed')

    async def _download_telegram_file(self,file_id:str,source_type:str,title:str='')->tuple[Path,bool]:
        if not self.bot_token:raise RuntimeError('missing_env: BOT_TOKEN')
        if not file_id.strip():raise RuntimeError('missing_source_id')
        info=await self._http_get_json(f'https://api.telegram.org/bot{self.bot_token}/getFile',{'file_id':file_id})
        if not info.get('ok'):raise RuntimeError(f'telegram_getFile_failed: {info}')
        file_path=str(info['result']['file_path']);name=Path(file_path).name or (title.strip() or file_id);video=self._infer_video(source_type,name);ext=Path(file_path).suffix.lower() or ('.mp4' if video else '.ogg')
        local=self._download_dir/f"{re.sub(r'[^A-Za-z0-9._-]+','_',file_id)[:80]}_{int(time.time()*1000)}{ext}";local.write_bytes(await self._http_get_bytes(f'https://api.telegram.org/file/bot{self.bot_token}/{file_path}'));return local,video

    async def _download_telegram_message(self,source_chat_id:int,source_message_id:int,source_type:str,title:str='')->tuple[Path,bool]:
        await self._ensure_owner();m=await self._client.get_messages(int(source_chat_id),ids=int(source_message_id))
        if not m or not getattr(m,'media',None):raise RuntimeError('telegram_message_not_found')
        mime=str(getattr(getattr(m,'file',None),'mime_type','') or '').lower();name=str(getattr(getattr(m,'file',None),'name','') or '');video=bool(getattr(m,'video',None)) or self._infer_video(source_type,name,mime);stem=re.sub(r'[^A-Za-z0-9._-]+','_',title or name or f'{source_chat_id}_{source_message_id}')[:80]
        local=self._download_dir/f'{stem}_{int(time.time()*1000)}';out=await self._client.download_media(m,file=str(local))
        if not out:raise RuntimeError('telegram_download_failed')
        return Path(out),video

    async def _probe_duration(self,path:Path)->int:
        try:
            p=await asyncio.to_thread(subprocess.run,['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(path)],capture_output=True,text=True,check=False,timeout=30)
            return max(0,int(round(float((p.stdout or '').strip() or 0)))) if p.returncode==0 else 0
        except Exception:return 0

    async def _cleanup_file(self,path:str)->None:
        if path:
            try:await asyncio.to_thread(Path(path).unlink,missing_ok=True)
            except Exception:pass

    async def _make_seek_file(self,s:AudioSession,target:int)->str:
        if not s.source_path:raise RuntimeError('seek_source_missing')
        out=self._download_dir/f'seek_{s.chat_id}_{int(time.time()*1000)}.ogg';cmd=['ffmpeg','-y','-ss',str(target),'-i',s.source_path,'-vn','-acodec','copy',str(out)]
        p=await asyncio.to_thread(subprocess.run,cmd,capture_output=True,text=True,check=False,timeout=120)
        if p.returncode!=0:
            p=await asyncio.to_thread(subprocess.run,['ffmpeg','-y','-ss',str(target),'-i',s.source_path,'-vn','-c:a','libopus','-b:a','128k',str(out)],capture_output=True,text=True,check=False,timeout=120)
        if p.returncode!=0:raise RuntimeError('seek_transcode_failed')
        return str(out)

    async def _play(self,chat_id:int,source:str)->None:
        if self.calls is None:raise RuntimeError('pytgcalls_not_ready')
        if self._group_call is not None and getattr(self._group_call,'is_connected',False):
            self._group_call.input_filename=source;return
        self._group_call=self.calls.get_file_group_call(source,play_on_repeat=False)
        try:await self._group_call.start(chat_id)
        except Exception as exc:
            self._group_call=None
            if 'GroupCallNotFound' in type(exc).__name__:raise RuntimeError('voice_chat_not_found')
            raise

    async def meta(self,chat_id:int,source_type:str,source_id:str,title:str='',duration:int=0,source_chat_id:int=0,source_message_id:int=0)->dict[str,Any]:
        st=self._normalize_source_type(source_type);s=self._sessions.get(chat_id) or AudioSession(chat_id=chat_id);s.title=title;s.source_type=st;s.source_id=source_id;s.source_chat_id=str(source_chat_id or '');s.source_message_id=str(source_message_id or '');s.duration=max(0,int(duration or 0));s.video=st=='telegram_video';self._sessions[chat_id]=self._touch(s);return{'ok':True,'action':'meta','state':self.state(chat_id)}

    async def start(self,chat_id:int,source_type:str,source_id:str,title:str='',duration:int=0,offset:int=0,source_chat_id:int=0,source_message_id:int=0)->dict[str,Any]:
        st=self._normalize_source_type(source_type)
        async with self._lock_for(chat_id):
            s=self._sessions.get(chat_id) or AudioSession(chat_id=chat_id);new_path=''
            try:
                path,video=await (self._download_telegram_message(source_chat_id,source_message_id,st,title) if st=='telegram_message' else self._download_telegram_file(source_id,st,title));probed=await self._probe_duration(path);new_path=str(path)
                await self._ensure_owner();await self._play(chat_id,new_path)
                if s.source_path and s.source_path!=new_path:await self._cleanup_file(s.source_path)
                if s.local_path and s.local_path!=s.source_path and s.local_path!=new_path:await self._cleanup_file(s.local_path)
                s.status='playing';s.title=title;s.source_type=st;s.source_id=source_id;s.source_chat_id=str(source_chat_id or '');s.source_message_id=str(source_message_id or '');s.duration=probed or max(0,int(duration or 0));s.position=max(0,int(offset or 0));s.paused=False;s.last_error='';s.local_path=new_path;s.source_path=new_path;s.video=video;self._sessions[chat_id]=self._touch(s)
                return{'ok':True,'action':'start','played':True,'state':self.state(chat_id)}
            except Exception as exc:
                if new_path:await self._cleanup_file(new_path)
                s.status='error';s.last_error=f'{type(exc).__name__}: {exc}';self._sessions[chat_id]=self._touch(s);logger.exception('audio start failed chat_id=%s',chat_id);return{'ok':False,'action':'start','error':type(exc).__name__,'detail':str(exc),'state':self.state(chat_id)}

    async def next(self,*args,**kwargs)->dict[str,Any]:r=await self.start(*args,**kwargs);r['action']='next';return r
    async def skip(self,*args,**kwargs)->dict[str,Any]:r=await self.start(*args,**kwargs);r['action']='skip';return r

    async def pause(self,chat_id:int)->dict[str,Any]:
        async with self._lock_for(chat_id):
            s=self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            try:
                await self._ensure_owner()
                if not self._group_call or not getattr(self._group_call,'is_connected',False):raise RuntimeError('voice_call_not_connected')
                self._group_call.pause_playout();s.status='paused';s.paused=True;self._sessions[chat_id]=self._touch(s);return{'ok':True,'action':'pause','state':self.state(chat_id)}
            except Exception as exc:return{'ok':False,'action':'pause','error':type(exc).__name__,'detail':str(exc),'state':self.state(chat_id)}

    async def resume(self,chat_id:int)->dict[str,Any]:
        async with self._lock_for(chat_id):
            s=self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            try:
                await self._ensure_owner()
                if not self._group_call or not getattr(self._group_call,'is_connected',False):raise RuntimeError('voice_call_not_connected')
                self._group_call.resume_playout();s.status='playing';s.paused=False;self._sessions[chat_id]=self._touch(s);return{'ok':True,'action':'resume','state':self.state(chat_id)}
            except Exception as exc:return{'ok':False,'action':'resume','error':type(exc).__name__,'detail':str(exc),'state':self.state(chat_id)}

    async def seek(self,chat_id:int,delta:int=0,position:int|None=None)->dict[str,Any]:
        async with self._lock_for(chat_id):
            s=self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            try:
                await self._ensure_owner();target=max(0,int(s.position)+int(delta)) if position is None else max(0,int(position));target=min(target,s.duration) if s.duration else target
                if s.duration and target>=s.duration:return{'ok':True,'action':'seek','ended':True,'position':s.duration,'state':self.state(chat_id)}
                new_path=await self._make_seek_file(s,target)
                if not self._group_call or not getattr(self._group_call,'is_connected',False):raise RuntimeError('voice_call_not_connected')
                self._group_call.input_filename=new_path
                old=s.local_path;s.local_path=new_path;s.position=target;self._sessions[chat_id]=self._touch(s)
                if old and old!=s.source_path:await self._cleanup_file(old)
                return{'ok':True,'action':'seek','position':target,'state':self.state(chat_id)}
            except Exception as exc:return{'ok':False,'action':'seek','error':type(exc).__name__,'detail':str(exc),'state':self.state(chat_id)}

    async def release(self)->dict[str,Any]:
        async with self._owner_lock:
            try:
                if self._group_call:
                    try:await self._group_call.stop()
                    except Exception:pass
                self._group_call=None;self.calls=None
                if self._client:
                    try:await self._client.disconnect()
                    except Exception:pass
                self._client=None;self.ready=False;self.backend_error='released';return{'ok':True,'action':'release','released':True}
            except Exception as exc:self.ready=False;self.backend_error=f'{type(exc).__name__}: {exc}';return{'ok':False,'action':'release','error':type(exc).__name__,'detail':str(exc)}

    async def stop(self,chat_id:int)->dict[str,Any]:
        async with self._lock_for(chat_id):
            s=self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
            try:
                left=False
                if self._group_call:
                    try:await self._group_call.stop();left=True
                    except Exception:pass
                self._group_call=None;self.calls=None
                await self._cleanup_file(s.local_path)
                if s.source_path and s.source_path!=s.local_path:await self._cleanup_file(s.source_path)
                self._sessions.pop(chat_id,None);await self.release();return{'ok':True,'action':'stop','stopped':True,'left_call':left,'state':self.state(chat_id)}
            except Exception as exc:return{'ok':False,'action':'stop','error':type(exc).__name__,'detail':str(exc),'state':self.state(chat_id)}

    async def close(self)->None:
        for s in list(self._sessions.values()):
            await self._cleanup_file(s.local_path)
            if s.source_path and s.source_path!=s.local_path:await self._cleanup_file(s.source_path)
        self._sessions.clear();await self.release()

    def active_sessions_count(self)->int:return sum(1 for s in self._sessions.values() if s.status in {'playing','paused'})
    def state(self,chat_id:int)->dict[str,Any]:
        s=self._sessions.get(chat_id) or AudioSession(chat_id=chat_id)
        return{'ok':True,'chat_id':chat_id,'ready':self.ready,'backend_error':self.backend_error,'active_sessions':self.active_sessions_count(),'session':{'chat_id':s.chat_id,'status':s.status,'title':s.title,'source_type':s.source_type,'source_id':s.source_id,'source_chat_id':s.source_chat_id,'source_message_id':s.source_message_id,'duration':s.duration,'position':s.position,'paused':s.paused,'last_error':s.last_error,'video':s.video,'updated_at':s.updated_at}}

service=AudioService()
