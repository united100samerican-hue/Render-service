from __future__ import annotations
import asyncio,base64,os,shutil,tempfile,time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
import httpx,yt_dlp
from config import YOUTUBE_COOKIES
VIDEO_EXTS={".mp4",".mkv",".mov",".webm",".m4v",".avi",".m3u8",".mpd",".ts",".m2ts"}
AUDIO_EXTS={".mp3",".ogg",".oga",".wav",".m4a",".aac",".flac",".opus"}
YOUTUBE_HOSTS={"youtube.com","www.youtube.com","m.youtube.com","music.youtube.com","youtu.be","www.youtu.be"}
SEARCH_PREFIXES=("ytsearch:","ytsearch1:","ytsearch2:","ytsearch3:")
class UrlResolver:
 def __init__(self):self._timeout=20;self._cache_ttl=90;self._cache:dict[str,tuple[float,dict[str,Any]]]={};self._cookie_file="";self._prepare_cookies()
 def _cache_get(self,source):
  item=self._cache.get(source)
  if not item:return None
  ts,value=item
  if time.time()-ts>self._cache_ttl:self._cache.pop(source,None);return None
  return dict(value)
 def invalidate(self,source):self._cache.pop(str(source or ""),None)
 def _cache_set(self,source,value):
  self._cache[source]=(time.time(),dict(value))
  if len(self._cache)>128:self._cache.pop(min(self._cache.items(),key=lambda x:x[1][0])[0],None)
 def _prepare_cookies(self):
  raw=str(YOUTUBE_COOKIES or "").strip()
  if not raw:return
  text=raw
  if raw.startswith("base64:"):
   try:text=base64.b64decode(raw[7:].strip()).decode("utf-8")
   except Exception:return
  if "Netscape HTTP Cookie File" not in text and not("\n" in text or "\r" in text):return
  try:
   fd,path=tempfile.mkstemp(prefix="youtube_cookies_",suffix=".txt");os.close(fd);Path(path).write_text(text,encoding="utf-8");self._cookie_file=path
  except Exception:self._cookie_file=""
 @staticmethod
 def _is_youtube_url(value):
  raw=str(value or "").strip()
  if not raw.lower().startswith(("http://","https://")):return False
  try:host=str(urlsplit(raw).hostname or "").lower().rstrip(".")
  except Exception:return False
  return host in YOUTUBE_HOSTS
 @staticmethod
 def _is_youtube_search(value):return str(value or "").strip().lower().startswith(SEARCH_PREFIXES)
 @staticmethod
 def _is_direct(url,content_type=""):
  u=str(url or "").strip().lower();ct=content_type.lower().split(";",1)[0].strip();ext=Path(url.split("?",1)[0]).suffix.lower()
  if u.startswith(("rtmp://","rtmps://","rtsp://")):return True,"video",True
  if ct.startswith("video/") or ext in VIDEO_EXTS:return True,"video",ext in {".m3u8",".mpd"}
  if ct.startswith("audio/") or ext in AUDIO_EXTS:return True,"audio",False
  if "mpegurl" in ct or "dash+xml" in ct:return True,"video",True
  return False,"audio",False
 async def _head(self,url):
  if not str(url).lower().startswith(("http://","https://")):return "",url
  try:
   async with httpx.AsyncClient(timeout=self._timeout,follow_redirects=True) as client:
    r=await client.head(url,headers={"User-Agent":"Mozilla/5.0"});return str(r.headers.get("content-type","")),str(r.url)
  except Exception:return "",url
 @staticmethod
 def _is_bot_check_error(exc):
  m=str(exc).lower();return "sign in to confirm" in m or "not a bot" in m or "bot check" in m or "confirm you’re not a bot" in m
 def _youtube_options(self,embedded=False):
  o={"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True,"ignoreerrors":False,"socket_timeout":20,"retries":3,"fragment_retries":3,"concurrent_fragment_downloads":4,"continuedl":True,"geo_bypass":True,"http_headers":{"User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/140.0 Safari/537.36"},"format":"best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best[acodec!=none]/best"}
  pot=os.getenv("POT_PROVIDER_URL","").strip().rstrip("/")
  if pot:o["extractor_args"]={"youtube":{"player_client":["mweb"]},"youtubepot-bgutilhttp":{"base_url":[pot]}}
  elif embedded:o["extractor_args"]={"youtube":{"player_client":["web_embedded"]}}
  deno=shutil.which("deno") or ("/usr/local/bin/deno" if Path("/usr/local/bin/deno").is_file() else "")
  if deno:o["js_runtimes"]={"deno":{"path":deno}}
  if self._cookie_file:o["cookiefile"]=self._cookie_file
  return o
 def _extract(self,source):
  is_youtube=self._is_youtube_search(source) or self._is_youtube_url(source);options=self._youtube_options() if is_youtube else {"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True,"ignoreerrors":False,"socket_timeout":20,"retries":3,"fragment_retries":3,"concurrent_fragment_downloads":4,"continuedl":True,"geo_bypass":True,"http_headers":{"User-Agent":"Mozilla/5.0"},"format":"best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best[acodec!=none]/best"}
  def run(opts):
   with yt_dlp.YoutubeDL(opts) as ydl:
    info=ydl.extract_info(source,download=False)
    if info and info.get("entries"):info=next((e for e in info["entries"] if e),None)
    if not info:raise RuntimeError("url_metadata_empty")
    return info
  try:info=run(options)
  except yt_dlp.utils.DownloadError as exc:
   if not is_youtube or not self._is_bot_check_error(exc) or os.getenv("POT_PROVIDER_URL","").strip():raise
   info=run(self._youtube_options(True))
  stream=str(info.get("url") or "")
  if not stream:
   formats=[x for x in (info.get("formats") or []) if x.get("url") and x.get("protocol") not in {"mhtml"}];
   if is_youtube:formats=[x for x in formats if x.get("vcodec") not in (None,"none") and x.get("acodec") not in (None,"none")] or [x for x in formats if x.get("acodec") not in (None,"none")] or formats
   if not formats:raise RuntimeError("url_stream_not_found")
   formats.sort(key=lambda x:(x.get("height") or 0,x.get("tbr") or 0,x.get("abr") or 0),reverse=True);stream=str(formats[0]["url"])
  webpage=str(info.get("webpage_url") or info.get("original_url") or "").strip() or (source if not self._is_youtube_search(source) else "")
  if not webpage:raise RuntimeError("search_result_url_missing")
  title=str(info.get("title") or "").strip() or "غير معروف";v=str(info.get("vcodec") or "");a=str(info.get("acodec") or "");video=bool(v and v!="none" and a and a!="none")
  return {"source_url":webpage,"stream_url":stream,"title":title,"duration":int(info.get("duration") or 0),"webpage_url":webpage,"thumbnail":str(info.get("thumbnail") or ""),"video":video,"media_kind":"video" if video else "audio","live":bool(info.get("is_live")),"video_id":str(info.get("id") or "")}
 async def resolve(self,url):
  source=str(url or "").strip()
  if not source:raise RuntimeError("url_missing")
  if self._is_youtube_search(source) or self._is_youtube_url(source):
   cached=self._cache_get(source)
   if cached:return cached
   result=await asyncio.to_thread(self._extract,source);self._cache_set(source,result);return result
  direct,kind,live=self._is_direct(source);final=source
  if not direct:ct,final=await self._head(source);direct,kind,live=self._is_direct(final,ct)
  if direct:
   cached=self._cache_get(source)
   if cached:return cached
   name=Path(urlsplit(final or source).path).name or Path(urlsplit(source).path).name;title=Path(name).stem.strip() if name else "Audio";result={"source_url":source,"stream_url":final or source,"title":title or "Audio","duration":0,"webpage_url":source,"thumbnail":"","video":kind=="video","media_kind":kind,"live":live};self._cache_set(source,result);return result
  cached=self._cache_get(source)
  if cached:return cached
  result=await asyncio.to_thread(self._extract,source);self._cache_set(source,result);return result
