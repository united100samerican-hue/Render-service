from __future__ import annotations
import asyncio
import base64
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import httpx
import yt_dlp
from config import YOUTUBE_COOKIES
VIDEO_EXTS={".mp4",".mkv",".mov",".webm",".m4v",".avi",".m3u8",".mpd",".ts",".m2ts"}
AUDIO_EXTS={".mp3",".ogg",".oga",".wav",".m4a",".aac",".flac",".opus"}
YOUTUBE_HOSTS={"youtube.com","www.youtube.com","m.youtube.com","music.youtube.com","youtu.be","www.youtu.be"}
SEARCH_PREFIXES=("ytsearch:","ytsearch1:","ytsearch2:","ytsearch3:")
class UrlResolver:
    def __init__(self):
        self._timeout=20
        self._cache_ttl=90
        self._cache:dict[str,tuple[float,dict[str,Any]]]={}
        self._cookie_file=""
        self._prepare_cookies()
    def _cache_get(self,source:str)->dict[str,Any]|None:
        item=self._cache.get(source)
        if not item:return None
        ts,value=item
        if time.time()-ts>self._cache_ttl:
            self._cache.pop(source,None)
            return None
        return dict(value)
    def _cache_set(self,source:str,value:dict[str,Any])->None:
        self._cache[source]=(time.time(),dict(value))
        if len(self._cache)>128:
            oldest=min(self._cache.items(),key=lambda item:item[1][0])[0]
            self._cache.pop(oldest,None)
    def _prepare_cookies(self):
        raw=str(YOUTUBE_COOKIES or "").strip()
        if not raw:return
        text=raw
        if raw.startswith("base64:"):
            try:text=base64.b64decode(raw[7:]).decode("utf-8")
            except Exception:return
        if not text.startswith("# Netscape HTTP Cookie File") and "\n" not in text and "\r" not in text:return
        try:
            fd,path=tempfile.mkstemp(prefix="youtube_cookies_",suffix=".txt")
            os.close(fd)
            Path(path).write_text(text,encoding="utf-8")
            self._cookie_file=path
        except Exception:self._cookie_file=""
    @staticmethod
    def _is_youtube_url(value:str)->bool:
        raw=str(value or "").strip()
        if not raw.lower().startswith(("http://","https://")):return False
        try:host=str(urlsplit(raw).hostname or "").lower().rstrip(".")
        except Exception:return False
        return host in YOUTUBE_HOSTS
    @staticmethod
    def _is_youtube_search(value:str)->bool:return str(value or "").strip().lower().startswith(SEARCH_PREFIXES)
    @staticmethod
    def _is_direct(url:str,content_type:str="")->tuple[bool,str,bool]:
        u=str(url or "").strip().lower()
        ct=content_type.lower().split(";",1)[0].strip()
        ext=Path(url.split("?",1)[0]).suffix.lower()
        if u.startswith(("rtmp://","rtmps://","rtsp://")):return True,"video",True
        if ct.startswith("video/") or ext in VIDEO_EXTS:return True,"video",ext in {".m3u8",".mpd"}
        if ct.startswith("audio/") or ext in AUDIO_EXTS:return True,"audio",False
        if "mpegurl" in ct or "dash+xml" in ct:return True,"video",True
        return False,"audio",False
    async def _head(self,url:str)->tuple[str,str]:
        if not str(url).lower().startswith(("http://","https://")):return "",url
        try:
            async with httpx.AsyncClient(timeout=self._timeout,follow_redirects=True) as client:
                response=await client.head(url,headers={"User-Agent":"Mozilla/5.0"})
                return str(response.headers.get("content-type","")),str(response.url)
        except Exception:return "",url
    def _extract(self,source:str)->dict[str,Any]:
        is_youtube=self._is_youtube_search(source) or self._is_youtube_url(source)
        options={"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True,"ignoreerrors":False,"socket_timeout":20,"retries":3,"fragment_retries":3,"concurrent_fragment_downloads":4,"continuedl":True,"geo_bypass":True,"http_headers":{"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"},"extractor_args":{"youtube":{"player_client":["default","web_embedded"]}},"format":"bestaudio[ext=m4a]/bestaudio/best[acodec!=none]" if is_youtube else "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best[acodec!=none]/best"}
        deno=shutil.which("deno") or ("/usr/local/bin/deno" if Path("/usr/local/bin/deno").is_file() else "")
        if deno:options["js_runtimes"]={"deno":{"path":deno}}
        if self._cookie_file:options["cookiefile"]=self._cookie_file
        with yt_dlp.YoutubeDL(options) as ydl:
            info=ydl.extract_info(source,download=False)
            if info and info.get("entries"):
                info=next((entry for entry in info["entries"] if entry),None)
            if not info:raise RuntimeError("url_metadata_empty")
            stream=str(info.get("url") or "")
            if not stream:
                formats=[item for item in (info.get("formats") or []) if item.get("url")]
                if not formats:raise RuntimeError("url_stream_not_found")
                formats.sort(key=lambda item:(item.get("abr") or item.get("tbr") or 0,item.get("height") or 0),reverse=True)
                stream=str(formats[0]["url"])
            webpage_url=str(info.get("webpage_url") or info.get("original_url") or "").strip()
            title=str(info.get("title") or "").strip()
            if not title:raise RuntimeError("url_title_missing")
            if not webpage_url:
                if self._is_youtube_search(source):raise RuntimeError("search_result_url_missing")
                webpage_url=str(source).strip()
            vcodec=str(info.get("vcodec") or "")
            acodec=str(info.get("acodec") or "")
            kind="video" if vcodec and vcodec!="none" and acodec and acodec!="none" else "audio"
            return {"source_url":webpage_url,"stream_url":stream,"title":title,"duration":int(info.get("duration") or 0),"webpage_url":webpage_url,"thumbnail":str(info.get("thumbnail") or ""),"video":kind=="video","media_kind":kind,"live":bool(info.get("is_live"))}
    async def resolve(self,url:str)->dict[str,Any]:
        source=str(url or "").strip()
        if not source:raise RuntimeError("url_missing")
        if self._is_youtube_search(source) or self._is_youtube_url(source):
            cached=self._cache_get(source)
            if cached:return cached
            result=await asyncio.to_thread(self._extract,source)
            self._cache_set(source,result)
            return result
        direct,kind,live=self._is_direct(source)
        final_url=source
        if not direct:
            content_type,final_url=await self._head(source)
            direct,kind,live=self._is_direct(final_url,content_type)
        if direct:
            cached=self._cache_get(source)
            if cached:return cached
            name=Path(urlsplit(final_url or source).path).name or Path(urlsplit(source).path).name
            title=Path(name).stem.strip() if name else "Audio"
            result={"source_url":source,"stream_url":final_url or source,"title":title or "Audio","duration":0,"webpage_url":source,"thumbnail":"","video":kind=="video","media_kind":kind,"live":live}
            self._cache_set(source,result)
            return result
        cached=self._cache_get(source)
        if cached:return cached
        result=await asyncio.to_thread(self._extract,source)
        self._cache_set(source,result)
        return result