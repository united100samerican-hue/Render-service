from __future__ import annotations
import asyncio
from typing import Any
import yt_dlp
from .config import Settings
from .errors import SocialMediaError
from .models import MediaInfo
class Extractor:
    def __init__(self,settings:Settings):self.s=settings
    def _opts(self)->dict[str,Any]:
        return {'quiet':True,'no_warnings':True,'noplaylist':True,'socket_timeout':self.s.request_timeout,'retries':2,'fragment_retries':2,'concurrent_fragment_downloads':4,'http_headers':{'User-Agent':'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131 Safari/537.36'}}
    def _info(self,url:str)->dict[str,Any]:
        try:
            with yt_dlp.YoutubeDL(self._opts()) as y: info=y.extract_info(url,download=False)
        except yt_dlp.utils.DownloadError as e: raise SocialMediaError('extract_failed',str(e)) from e
        if not info:raise SocialMediaError('empty_info')
        if info.get('_type')=='playlist':raise SocialMediaError('playlist_not_allowed')
        return info
    @staticmethod
    def _formats(info:dict[str,Any]): return [f for f in info.get('formats') or [] if f.get('url')]
    def _best_combined(self,info):
        fs=[f for f in self._formats(info) if f.get('vcodec') not in (None,'none') and f.get('acodec') not in (None,'none')]
        return max(fs,key=lambda f:(int(f.get('height') or 0),float(f.get('tbr') or 0))) if fs else None
    def _best_audio(self,info):
        fs=[f for f in self._formats(info) if f.get('acodec') not in (None,'none')]
        return max(fs,key=lambda f:float(f.get('abr') or f.get('tbr') or 0)) if fs else None
    async def inspect(self,url:str)->MediaInfo:
        url=str(url or '').strip()
        if not url.lower().startswith(('http://','https://')):raise SocialMediaError('invalid_url')
        info=await asyncio.to_thread(self._info,url);combined=self._best_combined(info);audio=self._best_audio(info);formats=self._formats(info)
        has_video_stream=any(f.get('vcodec') not in (None,'none') for f in formats);has_video=bool(combined or (not bool(info.get('is_live')) and has_video_stream));has_audio=bool(audio or combined)
        duration=max(0,int(float(info.get('duration') or 0)))
        if self.s.max_duration and duration>self.s.max_duration:raise SocialMediaError('duration_limit')
        direct=combined or audio
        return MediaInfo(url=url,webpage_url=str(info.get('webpage_url') or url),title=str(info.get('title') or 'وسائط غير معروفة').strip(),duration=duration,thumbnail=str(info.get('thumbnail') or '').strip(),extractor=str(info.get('extractor_key') or info.get('extractor') or '').strip(),is_live=bool(info.get('is_live')),has_video=has_video,has_audio=has_audio,direct_url=str(direct.get('url') if direct else '').strip(),direct_ext=str(direct.get('ext') if direct else '').strip(),source_id=str(info.get('id') or '').strip())
    def _download(self,info:MediaInfo)->str:
        out=str(self.s.media_dir/'%(extractor_key)s_%(id)s_%(epoch)s.%(ext)s');o=self._opts();o.update({'format':f'bv*[height<=?{self.s.max_height}][ext=mp4]+ba[ext=m4a]/bv*[height<=?{self.s.max_height}]+ba/b[ext=mp4]/b','merge_output_format':'mp4','outtmpl':out,'overwrites':False})
        try:
            with yt_dlp.YoutubeDL(o) as y:
                data=y.extract_info(info.url,download=True)
                if not data:raise SocialMediaError('download_empty')
                p=Path(y.prepare_filename(data))
                if not p.exists():
                    cand=sorted(self.s.media_dir.glob(f"{data.get('extractor_key','unknown')}_{data.get('id','unknown')}_*.mp4"),key=lambda x:x.stat().st_mtime,reverse=True)
                    p=cand[0] if cand else p
                if not p.exists():
                    cand=sorted(self.s.media_dir.glob(p.with_suffix('').name+'.*'),key=lambda x:x.stat().st_mtime,reverse=True);p=cand[0] if cand else p
                if not p.exists():raise SocialMediaError('download_file_missing')
                if p.stat().st_size>self.s.max_media_bytes:
                    p.unlink(missing_ok=True);raise SocialMediaError('media_size_limit')
                return str(p)
        except SocialMediaError:raise
        except yt_dlp.utils.DownloadError as e:raise SocialMediaError('download_failed',str(e)) from e
    async def download_vod(self,info:MediaInfo)->str:
        if info.is_live:raise SocialMediaError('live_download_not_supported')
        return await asyncio.to_thread(self._download,info)
