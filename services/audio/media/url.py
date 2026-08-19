from __future__ import annotations
import asyncio
import mimetypes
from pathlib import Path
from typing import Any
import httpx
import yt_dlp

VIDEO_EXTS = {'.mp4','.mkv','.mov','.webm','.m4v','.avi','.m3u8','.mpd'}
AUDIO_EXTS = {'.mp3','.ogg','.oga','.wav','.m4a','.aac','.flac','.opus'}

class UrlResolver:
    def __init__(self):
        self._timeout = 30

    @staticmethod
    def _is_direct(url: str, content_type: str = '') -> tuple[bool,str]:
        ct = content_type.lower().split(';',1)[0].strip()
        ext = Path(url.split('?',1)[0]).suffix.lower()
        if ct.startswith('video/') or ext in VIDEO_EXTS:
            return True,'video'
        if ct.startswith('audio/') or ext in AUDIO_EXTS:
            return True,'audio'
        if 'mpegurl' in ct or 'dash+xml' in ct:
            return True,'video'
        return False,'audio'

    async def _head(self, url: str) -> tuple[str,str]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as c:
                r = await c.head(url, headers={'User-Agent':'Mozilla/5.0'})
                return str(r.headers.get('content-type','')), str(r.url)
        except Exception:
            return '',url

    def _extract(self, url: str) -> dict[str,Any]:
        opts = {
            'quiet': True, 'no_warnings': True, 'skip_download': True,
            'noplaylist': True, 'geo_bypass': True,
            'format': 'best[ext=mp4][vcodec!=none][acodec!=none]/best[acodec!=none]/best',
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and info.get('entries'):
                info = next((x for x in info['entries'] if x), None)
            if not info:
                raise RuntimeError('url_metadata_empty')
            stream = info.get('url') or ''
            if not stream:
                formats = info.get('formats') or []
                usable = [f for f in formats if f.get('url')]
                if not usable:
                    raise RuntimeError('url_stream_not_found')
                usable.sort(key=lambda f: (f.get('height') or 0, f.get('tbr') or 0), reverse=True)
                stream = usable[0]['url']
            vcodec = str(info.get('vcodec') or '')
            kind = 'video' if vcodec and vcodec != 'none' else 'audio'
            duration = int(info.get('duration') or 0)
            return {
                'source_url': url, 'stream_url': str(stream),
                'title': str(info.get('title') or url), 'duration': duration,
                'webpage_url': str(info.get('webpage_url') or url),
                'thumbnail': str(info.get('thumbnail') or ''),
                'video': kind == 'video', 'media_kind': kind,
                'live': bool(info.get('is_live')),
            }

    async def resolve(self, url: str) -> dict[str,Any]:
        url = str(url or '').strip()
        if not url:
            raise RuntimeError('url_missing')
        content_type, final_url = await self._head(url)
        direct, kind = self._is_direct(url, content_type)
        if direct:
            return {
                'source_url': url, 'stream_url': final_url or url,
                'title': url, 'duration': 0, 'webpage_url': url,
                'thumbnail': '', 'video': kind == 'video', 'media_kind': kind,
                'live': False,
            }
        return await asyncio.to_thread(self._extract, url)
