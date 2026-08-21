from __future__ import annotations
import asyncio
import base64
import mimetypes
import os
import tempfile
from pathlib import Path
from typing import Any
import httpx
import yt_dlp
from config import YOUTUBE_COOKIES
VIDEO_EXTS={'.mp4','.mkv','.mov','.webm','.m4v','.avi','.m3u8','.mpd','.ts','.m2ts'}
AUDIO_EXTS={'.mp3','.ogg','.oga','.wav','.m4a','.aac','.flac','.opus','.aac'}
class UrlResolver:
    def __init__(self):
        self._timeout=30
        self._cookie_file=''
        self._prepare_cookies()
    def _prepare_cookies(self):
    raw=str(YOUTUBE_COOKIES or '').strip()
    if not raw:return
    text=raw
    if raw.startswith('base64:'):
        try:text=base64.b64decode(raw[7:]).decode('utf-8')
        except Exception:return
    if not text.startswith('# Netscape HTTP Cookie File') and '\n' not in text and '\r' not in text:return
    try:
        fd,path=tempfile.mkstemp(prefix='youtube_cookies_',suffix='.txt')
        os.close(fd)
        Path(path).write_text(text,encoding='utf-8')
        self._cookie_file=path
    except Exception:
        self._cookie_file=''
    @staticmethod
    def _is_direct(url:str,content_type:str='')->tuple[bool,str,bool]:
        u=str(url or '').strip().lower()
        ct=content_type.lower().split(';',1)[0].strip()
        ext=Path(url.split('?',1)[0]).suffix.lower()
        if u.startswith(('rtmp://','rtmps://','rtsp://')):return True,'video',True
        if ct.startswith('video/') or ext in VIDEO_EXTS:return True,'video',ext in {'.m3u8','.mpd'}
        if ct.startswith('audio/') or ext in AUDIO_EXTS:return True,'audio',False
        if 'mpegurl' in ct or 'dash+xml' in ct:return True,'video',True
        return False,'audio',False
    async def _head(self,url:str)->tuple[str,str]:
        if not str(url).lower().startswith(('http://','https://')):return '',url
        try:
            async with httpx.AsyncClient(timeout=self._timeout,follow_redirects=True) as c:
                r=await c.head(url,headers={'User-Agent':'Mozilla/5.0'})
                return str(r.headers.get('content-type','')),str(r.url)
        except Exception:return '',url
    def _extract(self,url:str)->dict[str,Any]:
        opts={'quiet':True,'no_warnings':True,'skip_download':True,'noplaylist':True,'geo_bypass':True,'format':'best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best[acodec!=none]/best'}
        if self._cookie_file:opts['cookiefile']=self._cookie_file
        with yt_dlp.YoutubeDL(opts) as ydl:
            info=ydl.extract_info(url,download=False)
            if info and info.get('entries'):info=next((x for x in info['entries'] if x),None)
            if not info:raise RuntimeError('url_metadata_empty')
            stream=str(info.get('url') or '')
            if not stream:
                formats=[f for f in(info.get('formats') or []) if f.get('url')]
                if not formats:raise RuntimeError('url_stream_not_found')
                formats.sort(key=lambda f:(f.get('height') or 0,f.get('tbr') or 0),reverse=True)
                stream=str(formats[0]['url'])
            vcodec=str(info.get('vcodec') or '')
            kind='video' if vcodec and vcodec!='none' else 'audio'
            return {'source_url':url,'stream_url':stream,'title':str(info.get('title') or url),'duration':int(info.get('duration') or 0),'webpage_url':str(info.get('webpage_url') or url),'thumbnail':str(info.get('thumbnail') or ''),'video':kind=='video','media_kind':kind,'live':bool(info.get('is_live'))}
    async def resolve(self,url:str)->dict[str,Any]:
        url=str(url or '').strip()
        if not url:raise RuntimeError('url_missing')
        content_type,final_url=await self._head(url)
        direct,kind,live=self._is_direct(url,content_type)
        if direct:return {'source_url':url,'stream_url':final_url or url,'title':url,'duration':0,'webpage_url':url,'thumbnail':'','video':kind=='video','media_kind':kind,'live':live}
        return await asyncio.to_thread(self._extract,url)
