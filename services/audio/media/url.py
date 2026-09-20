from __future__ import annotations
import asyncio,base64,logging,os,re,shutil,tempfile,time
from http.cookiejar import MozillaCookieJar
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs,urlsplit
import httpx,yt_dlp
from config import POT_PROVIDER_URL,YOUTUBE_COOKIES,YOUTUBE_PROXY
log=logging.getLogger("audio_url")
VIDEO_EXTS={".mp4",".mkv",".mov",".webm",".m4v",".avi",".m3u8",".mpd",".ts",".m2ts"}
AUDIO_EXTS={".mp3",".ogg",".oga",".wav",".m4a",".aac",".flac",".opus"}
YOUTUBE_HOSTS={"youtube.com","www.youtube.com","m.youtube.com","music.youtube.com","youtu.be","www.youtu.be"}
SEARCH_PREFIXES=("ytsearch:","ytsearch1:","ytsearch2:","ytsearch3:")

class UrlResolver:
 def __init__(self):
  self._timeout=20;self._cache_ttl=90;self._cache={};self._cookie_file="";self._cookie_status={"configured":False,"loaded":False,"valid_format":False,"source":"none","bytes":0,"youtube_domains":0,"cookie_rows":0,"auth_cookies":0,"expired_auth_cookies":0}
  self._pot_available=None;self._pot_version="";self._pot_checked_at=0.0;self._yt_dlp_auth_checked_at=0.0;self._yt_dlp_auth_status={"checked":False,"detected":False,"login_info":False,"sapisid":False,"cookie_count":0,"error":""};self._prepare_cookies()
 def _cache_get(self,source):
  item=self._cache.get(source)
  if not item:return None
  if time.time()-item[0]>self._cache_ttl:self._cache.pop(source,None);return None
  return dict(item[1])
 def invalidate(self,source):self._cache.pop(str(source or "").strip(),None)
 def _cache_set(self,source,value):
  self._cache[source]=(time.time(),dict(value))
  if len(self._cache)>128:
   old=min(self._cache.items(),key=lambda x:x[1][0])[0];self._cache.pop(old,None)
 @staticmethod
 def _looks_like_base64(value):
  compact=re.sub(r"\s+","",str(value or ""))
  return len(compact)>=64 and len(compact)%4==0 and bool(re.fullmatch(r"[A-Za-z0-9+/=_-]+",compact))
 @staticmethod
 def _decode_cookie_text(raw):
  value=str(raw or "").strip()
  if not value:return "","none"
  if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):value=value[1:-1].strip()
  if "\\n" in value or "\\r" in value or "\\t" in value:value=value.replace("\\r\\n","\n").replace("\\n","\n").replace("\\r","\r").replace("\\t","\t")
  if value.startswith("base64:"):
   try:return base64.b64decode(re.sub(r"\s+","",value[7:]),validate=False).decode("utf-8-sig"),"base64"
   except Exception:return "","base64_invalid"
  if value.startswith("data:text/plain;base64,"):
   try:return base64.b64decode(re.sub(r"\s+","",value.split(",",1)[1]),validate=False).decode("utf-8-sig"),"base64"
   except Exception:return "","base64_invalid"
  if "Netscape HTTP Cookie File" in value[:256] or "HTTP Cookie File" in value[:256] or "\n" in value or "\r" in value:return value,"text"
  if UrlResolver._looks_like_base64(value):
   try:
    decoded=base64.b64decode(re.sub(r"\s+","",value),validate=False).decode("utf-8-sig")
    if "HTTP Cookie File" in decoded[:256] or "Netscape HTTP Cookie File" in decoded[:256]:return decoded,"base64_auto"
   except Exception:pass
  if "\n" not in value and "\r" not in value and "\x00" not in value and len(value)<=4096:
   try:
    p=Path(value).expanduser()
    if p.is_file():return p.read_text(encoding="utf-8"),"file"
   except (OSError,UnicodeError,ValueError):pass
  return value,"text_invalid"
 @staticmethod
 def _cookie_stats(text):
  auth_names={"SID","HSID","SSID","APISID","SAPISID","LOGIN_INFO","SIDCC","__SECURE-1PSID","__SECURE-3PSID","__SECURE-1PSIDTS","__SECURE-3PSIDTS","__SECURE-1PAPISID","__SECURE-3PAPISID"}
  now=int(time.time());lines=str(text or "").splitlines();first=next((x.lstrip("\ufeff").strip() for x in lines if x.strip()),"");domains=set();rows=auth=expired=login=sapi=0
  for line in lines:
   if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):continue
   if line.startswith("#HttpOnly_"):line=line[len("#HttpOnly_"):]
   parts=line.split("\t")
   if len(parts)<7:continue
   rows+=1;domain=parts[0].strip().lower().lstrip(".");name=parts[5].strip().upper()
   if domain=="youtube.com" or domain.endswith(".youtube.com"):
    domains.add(domain)
    if name in auth_names:
     auth+=1
     if name=="LOGIN_INFO":login+=1
     if name in {"SAPISID","__SECURE-3PAPISID","__SECURE-1PAPISID"}:sapi+=1
     try:expires=int(parts[4].strip() or 0)
     except Exception:expires=0
     if expires and expires<now:expired+=1
  return {"valid_format":first in {"# Netscape HTTP Cookie File","# HTTP Cookie File"},"cookie_rows":rows,"youtube_domains":len(domains),"auth_cookies":auth,"expired_auth_cookies":expired,"login_info":login>0,"sapisid_cookies":sapi}
 def _prepare_cookies(self):
  raw=str(YOUTUBE_COOKIES or "").strip();self._cookie_file=""
  if not raw:return
  text,source=self._decode_cookie_text(raw);stats=self._cookie_stats(text);self._cookie_status={"configured":True,"loaded":False,"source":source,"bytes":len(text.encode("utf-8","ignore")),"**":0,**stats}
  self._cookie_status.pop("**",None)
  if not stats["valid_format"] or stats["cookie_rows"]<=0:return
  fd,path=tempfile.mkstemp(prefix="youtube_cookies_",suffix=".txt");os.close(fd)
  try:
   Path(path).write_text(text.replace("\r\n","\n").replace("\r","\n"),encoding="utf-8");os.chmod(path,0o600);MozillaCookieJar(path).load(ignore_discard=True,ignore_expires=True);self._cookie_file=path
  except Exception as exc:
   try:Path(path).unlink(missing_ok=True)
   except Exception:pass
   self._cookie_status.update(valid_format=False,load_error=type(exc).__name__);return
  self._cookie_status["loaded"]=True
  self._check_yt_dlp_cookie_auth(force=True)
  log.info("youtube cookies configured source=%s valid=%s bytes=%d youtube_domains=%d auth=%d expired_auth=%d login_info=%s sapisid=%d yt_dlp_auth=%s yt_dlp_login=%s yt_dlp_sapisid=%s",source,bool(self._cookie_file),self._cookie_status["bytes"],self._cookie_status["youtube_domains"],self._cookie_status["auth_cookies"],self._cookie_status["expired_auth_cookies"],self._cookie_status.get("login_info"),self._cookie_status.get("sapisid_cookies",0),self._yt_dlp_auth_status.get("detected"),self._yt_dlp_auth_status.get("login_info"),self._yt_dlp_auth_status.get("sapisid"))
 def _check_yt_dlp_cookie_auth(self,force=False):
  now=time.time()
  if not force and self._yt_dlp_auth_status.get("checked") and now-self._yt_dlp_auth_checked_at<60:return dict(self._yt_dlp_auth_status)
  result={"checked":True,"detected":False,"login_info":False,"sapisid":False,"cookie_count":0,"error":""}
  if not self._cookie_file:
   self._yt_dlp_auth_status=result;self._yt_dlp_auth_checked_at=now;return result
  try:
   with yt_dlp.YoutubeDL({"quiet":True,"no_warnings":True,"cookiefile":self._cookie_file}) as ydl:
    ie=ydl.get_info_extractor("Youtube")
    jar=getattr(ie,"_youtube_cookies",None)
    cookies=list(jar) if jar is not None else []
    result["cookie_count"]=len(cookies)
    names={str(c.name).upper():c.value for c in cookies if c.value is not None}
    result["login_info"]="LOGIN_INFO" in names
    result["sapisid"]=any(names.get(n) for n in ("SAPISID","__SECURE-1PAPISID","__SECURE-3PAPISID"))
    result["detected"]=bool(getattr(ie,"is_authenticated",False))
  except Exception as exc:
   result["error"]=type(exc).__name__
  self._yt_dlp_auth_status=result;self._yt_dlp_auth_checked_at=now
  return dict(result)
 def cookie_status(self):
  auth=self._check_yt_dlp_cookie_auth();status=dict(self._cookie_status);status["yt_dlp_auth"]=auth;status["provider"]={"configured":bool(POT_PROVIDER_URL),"reachable":self._provider_available(),"url":POT_PROVIDER_URL,"version":self._pot_version};status["yt_dlp"]=getattr(yt_dlp.version,"__version__","unknown");return status
 @staticmethod
 def _is_youtube_url(value):
  raw=str(value or "").strip()
  if not raw.lower().startswith(("http://","https://")):return False
  try:return (urlsplit(raw).hostname or "").lower().rstrip(".") in YOUTUBE_HOSTS
  except Exception:return False
 @staticmethod
 def _is_youtube_search(value):return str(value or "").strip().lower().startswith(SEARCH_PREFIXES)
 @staticmethod
 def youtube_video_id(value):
  raw=str(value or "").strip()
  if not raw:return ""
  if raw.lower().startswith(SEARCH_PREFIXES):return ""
  try:
   p=urlsplit(raw);host=(p.hostname or "").lower()
   if host in {"youtu.be","www.youtu.be"}:return p.path.strip("/").split("/")[0]
   q=parse_qs(p.query)
   if q.get("v"):return q["v"][0]
   m=re.search(r"/(?:shorts|embed|live)/([A-Za-z0-9_-]{6,})",p.path)
   return m.group(1) if m else ""
  except Exception:return ""
 @staticmethod
 def _is_direct(url,content_type=""):
  raw=str(url or "").strip().lower();content=content_type.lower().split(";",1)[0].strip();ext=Path(urlsplit(raw).path).suffix.lower()
  if raw.startswith(("rtmp://","rtmps://","rtsp://")):return True,"video",True
  if content.startswith("video/") or ext in VIDEO_EXTS:return True,"video",ext in {".m3u8",".mpd"}
  if content.startswith("audio/") or ext in AUDIO_EXTS:return True,"audio",False
  if "mpegurl" in content or "dash+xml" in content:return True,"video",True
  return False,"audio",False
 async def _head(self,url):
  if not str(url).lower().startswith(("http://","https://")):return "",url
  try:
   async with httpx.AsyncClient(timeout=self._timeout,follow_redirects=True) as c:
    r=await c.head(url,headers={"User-Agent":"Mozilla/5.0"});return str(r.headers.get("content-type","")),str(r.url)
  except Exception:return "",url
 @staticmethod
 def _is_bot_check_error(exc):
  msg=str(exc).lower();return "sign in to confirm" in msg or "not a bot" in msg or "bot check" in msg or "confirm you’re not a bot" in msg or "login_required" in msg
 def _provider_available(self):
  now=time.time()
  if self._pot_available is not None and now-self._pot_checked_at<10:return bool(self._pot_available)
  self._pot_version=""
  try:
   r=httpx.get(f"{POT_PROVIDER_URL.rstrip('/')}/ping",timeout=.8);payload=r.json() if r.content else {};self._pot_version=str(payload.get("version") or "").strip();self._pot_available=r.status_code==200 and bool(self._pot_version)
  except Exception as exc:self._pot_available=False;log.warning("youtube pot provider unavailable error=%s",type(exc).__name__)
  self._pot_checked_at=now;return bool(self._pot_available)
 def _youtube_options(self,player_clients,use_provider=False,use_cookies=True,download=False,outdir=""):
  trace=str(os.getenv("YOUTUBE_VERBOSE","")).strip()=="1"
  opts={"quiet":not trace,"no_warnings":not trace,"verbose":trace,"skip_download":not download,"noplaylist":True,"playlistend":1,"ignoreerrors":False,"socket_timeout":25,"retries":2,"extractor_retries":1,"fragment_retries":3,"file_access_retries":2,"concurrent_fragment_downloads":3,"continuedl":True,"geo_bypass":True,"format":"best[height<=720][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/18/best[acodec!=none]/best"}
  yargs={"player_client":[str(x).strip() for x in player_clients if str(x).strip()]}
  if use_provider:yargs["fetch_pot"]=["auto"]
  if trace and use_provider:yargs["pot_trace"]=["true"]
  opts["extractor_args"]={"youtube":yargs}
  if use_provider and self._provider_available():opts["extractor_args"]["youtubepot-bgutilhttp"]={"base_url":[POT_PROVIDER_URL]}
  deno=shutil.which("deno") or ("/usr/local/bin/deno" if Path("/usr/local/bin/deno").is_file() else "")
  if deno:opts["js_runtimes"]={"deno":{"path":deno}}
  if use_cookies and self._cookie_file:opts["cookiefile"]=self._cookie_file
  if YOUTUBE_PROXY:opts["proxy"]=YOUTUBE_PROXY
  opts["remote_components"]=["ejs:github"]
  if download:
   d=Path(outdir);d.mkdir(parents=True,exist_ok=True);(d/"yt_tmp").mkdir(exist_ok=True)
   opts.update({"paths":{"home":str(d),"temp":str(d/"yt_tmp")},"outtmpl":{"default":str(d/"%(id)s.%(ext)s")},"merge_output_format":"mp4","overwrites":False})
  return opts
 @staticmethod
 def _generic_options():
  return {"quiet":True,"no_warnings":True,"skip_download":True,"noplaylist":True,"ignoreerrors":False,"socket_timeout":20,"retries":2,"fragment_retries":3,"concurrent_fragment_downloads":3,"continuedl":True,"geo_bypass":True,"http_headers":{"User-Agent":"Mozilla/5.0"},"format":"best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]/best[acodec!=none]/best"}
 def _attempts(self):
  provider=self._provider_available();a=[]
  if self._cookie_file:
   a.append(("default+web_embedded-cookies"+("+pot" if provider else ""),self._youtube_options(["default","web_embedded"],provider,True)))
   if provider:a.append(("web_creator-cookies+pot",self._youtube_options(["web_creator"],True,True)))
   a.append(("web_embedded-cookies",self._youtube_options(["web_embedded"],False,True)))
  a.append(("web_embedded-guest",self._youtube_options(["web_embedded"],False,False)))
  a.append(("web_safari-guest",self._youtube_options(["web_safari"],False,False)))
  if provider:a.append(("mweb-guest+pot",self._youtube_options(["mweb"],True,False)))
  a.append(("android_vr-guest",self._youtube_options(["android_vr"],False,False)))
  a.append(("tv-guest",self._youtube_options(["tv"],False,False)))
  return a
 @staticmethod
 def _pick_entry(info):
  if info and info.get("entries"):return next((e for e in info["entries"] if e),None)
  return info
 @staticmethod
 def _video_flag(info):
  rf=info.get("requested_formats") or []
  if rf:
   vc=any(str(x.get("vcodec") or "") not in {"","none"} for x in rf);ac=any(str(x.get("acodec") or "") not in {"","none"} for x in rf);return vc and ac
  return str(info.get("vcodec") or "") not in {"","none"} and str(info.get("acodec") or "") not in {"","none"}
 @staticmethod
 def _find_downloaded(outdir,video_id):
  d=Path(outdir);bad=(".part",".ytdl",".temp",".info.json",".jpg",".webp",".png")
  files=[p for p in d.glob(f"{video_id}.*") if p.is_file() and not p.name.endswith(bad) and p.stat().st_size>1024]
  if not files:return ""
  pref={".mp4":0,".m4a":1,".webm":2,".mkv":3,".mp3":4,".ogg":5}
  files.sort(key=lambda p:(pref.get(p.suffix.lower(),50),-p.stat().st_mtime))
  return str(files[0])
 def _extract(self,source,download=False,outdir=""):
  is_yt=self._is_youtube_search(source) or self._is_youtube_url(source);attempts=self._attempts() if is_yt else [("generic",self._generic_options())]
  if is_yt:
   auth=self._check_yt_dlp_cookie_auth()
   log.info("youtube extraction source=%s attempts=%d yt_dlp=%s provider=%s provider_version=%s cookies=%s yt_dlp_auth=%s yt_dlp_login=%s yt_dlp_sapisid=%s download=%s",source[:120],len(attempts),getattr(yt_dlp.version,"__version__","unknown"),self._pot_available,self._pot_version,bool(self._cookie_file),auth.get("detected"),auth.get("login_info"),auth.get("sapisid"),download)
  last=None
  for i,(label,base_opts) in enumerate(attempts):
   opts=dict(base_opts)
   if download and is_yt:
    opts=self._youtube_options(base_opts["extractor_args"]["youtube"]["player_client"],"youtubepot-bgutilhttp" in base_opts.get("extractor_args",{}), "cookiefile" in base_opts,outdir=outdir,download=True)
   try:
    with yt_dlp.YoutubeDL(opts) as ydl:
     info=self._pick_entry(ydl.extract_info(source,download=download))
     if not info:raise RuntimeError("url_metadata_empty")
     if download and is_yt:
      vid=str(info.get("id") or "");path=self._find_downloaded(outdir,vid)
      if not path:raise RuntimeError("youtube_download_file_missing")
    if is_yt:log.info("youtube extraction succeeded attempt=%d/%d client=%s yt_dlp=%s provider=%s cookies=%s download=%s",i+1,len(attempts),label,getattr(yt_dlp.version,"__version__","unknown"),self._pot_available,bool(self._cookie_file),download)
    break
   except (yt_dlp.utils.DownloadError,RuntimeError) as exc:
    last=exc;msg=str(exc).lower();retryable=self._is_bot_check_error(exc) or any(x in msg for x in ("no formats","unable to extract","failed to extract","player response","page needs to be reloaded","download failed"))
    if is_yt:
     auth=self._check_yt_dlp_cookie_auth()
     log.warning("youtube extraction failed attempt=%d/%d client=%s retryable=%s yt_dlp_auth=%s error=%s",i+1,len(attempts),label,retryable,auth.get("detected"),str(exc).splitlines()[0][:300])
    else:
     log.warning("youtube extraction failed attempt=%d/%d client=%s retryable=%s error=%s",i+1,len(attempts),label,retryable,str(exc).splitlines()[0][:300])
    if not is_yt or i>=len(attempts)-1 or not retryable:raise
    if download and outdir:
     try:
      for p in Path(outdir).glob("*"):
       if p.is_file() and p.name.split(".")[0]==str((last and "") or ""):pass
     except Exception:pass
    time.sleep(.2)
  else:
   raise last or RuntimeError("youtube_extraction_failed")
  stream=str(info.get("url") or "");raw_headers={}
  if not stream:
   formats=[x for x in (info.get("formats") or []) if x.get("url") and x.get("protocol")!="mhtml"]
   formats=[x for x in formats if x.get("vcodec") not in (None,"none") and x.get("acodec") not in (None,"none")] or formats
   if not formats:raise RuntimeError("url_stream_not_found")
   formats.sort(key=lambda x:((x.get("height") or 0),(x.get("tbr") or 0),(x.get("abr") or 0)),reverse=True);chosen=formats[0];stream=str(chosen["url"]);raw_headers=chosen.get("http_headers") or info.get("http_headers") or {}
  else:raw_headers=info.get("http_headers") or {}
  allowed={str(k):str(v) for k,v in dict(raw_headers).items() if str(k).lower() in {"user-agent","referer","origin","accept","accept-language"}}
  webpage=str(info.get("webpage_url") or info.get("original_url") or "").strip()
  if not webpage and not self._is_youtube_search(source):webpage=source
  if not webpage:raise RuntimeError("search_result_url_missing")
  title=str(info.get("title") or "").strip() or "غير معروف";video=self._video_flag(info);vid=str(info.get("id") or "")
  result={"source_url":webpage,"stream_url":stream,"title":title,"duration":int(info.get("duration") or 0),"webpage_url":webpage,"thumbnail":str(info.get("thumbnail") or ""), "video":video,"media_kind":"video" if video else "audio","live":bool(info.get("is_live")),"video_id":vid,"http_headers":allowed,"remote_stream":False}
  if download and is_yt:
   result["local_path"]=path;result["stream_url"]=path;result["remote_stream"]=False
  return result
 async def resolve(self,url,download=False,output_dir=""):
  source=str(url or "").strip()
  if not source:raise RuntimeError("url_missing")
  is_yt=self._is_youtube_search(source) or self._is_youtube_url(source)
  cached=self._cache_get(source)
  if cached and (not download or (cached.get("local_path") and Path(str(cached["local_path"])).is_file())):return cached
  if is_yt or not self._is_direct(source)[0]:
   result=await asyncio.to_thread(self._extract,source,download,output_dir)
  else:
   direct,kind,live=self._is_direct(source);ctype,final=("",source)
   if not direct:ctype,final=await self._head(source);direct,kind,live=self._is_direct(final,ctype)
   if not direct:result=await asyncio.to_thread(self._extract,source)
   else:
    name=Path(urlsplit(final or source).path).name
    result={"source_url":source,"stream_url":final or source,"title":Path(name).stem.strip() if name else "Audio","duration":0,"webpage_url":source,"thumbnail":"","video":kind=="video","media_kind":kind,"live":live}
  self._cache_set(source,result);return result
