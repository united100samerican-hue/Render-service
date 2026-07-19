from __future__ import annotations
import os
from dataclasses import dataclass
from functools import lru_cache

def _s(name:str,default:str="")->str:
    return str(os.getenv(name,default)).strip()

def _i(name:str,default:int=0)->int:
    try:
        return int(_s(name,str(default)))
    except:
        return default

@dataclass(frozen=True,slots=True)
class Settings:
    api_id:int
    api_hash:str
    session_string:str
    keepalive_secret:str
    rtmp_url:str
    ffmpeg_bin:str="ffmpeg"
    yt_dlp_bin:str="yt-dlp"
    video_size:str="1280x720"
    fps:int=30
    video_bitrate:str="4500k"
    audio_bitrate:int=128
    sample_rate:int=48000
    channels:int=2
    queue_size:int=4000
    reconnect_delay:int=5
    monitor_interval:int=5
    log_level:str="INFO"
    user_agent:str="Mozilla/5.0"
    enable_hwaccel:bool=False

@lru_cache(maxsize=1)
def settings()->Settings:
    return Settings(
        api_id=_i("API_ID"),
        api_hash=_s("API_HASH"),
        session_string=_s("SESSION_STRING"),
        keepalive_secret=_s("KEEPALIVE_SECRET"),
        rtmp_url=_s("TIKTOK_RTMP_URL")
    )