from __future__ import annotations
import os
from dataclasses import dataclass
from functools import lru_cache

def _s(name:str,default:str="")->str:
    return str(os.getenv(name,default)).strip()

def _i(name:str,default:int)->int:
    try:
        return int(_s(name,str(default)))
    except:
        return default

def _b(name:str,default:bool=False)->bool:
    return _s(name,"1" if default else "0").lower() in ("1","true","yes","on")

@dataclass(slots=True)
class Settings:
    api_id:int
    api_hash:str
    session_string:str
    keepalive_secret:str
    ffmpeg_bin:str
    yt_dlp_bin:str
    rtmp_url:str
    video_size:str
    fps:int
    video_bitrate:str
    audio_bitrate:int
    sample_rate:int
    channels:int
    reconnect_delay:int
    monitor_interval:int
    queue_size:int
    log_level:str
    user_agent:str
    enable_hwaccel:bool

@lru_cache(maxsize=1)
def settings()->Settings:
    return Settings(
        api_id=_i("API_ID",0),
        api_hash=_s("API_HASH"),
        session_string=_s("SESSION_STRING"),
        keepalive_secret=_s("KEEPALIVE_SECRET"),
        ffmpeg_bin=_s("FFMPEG_BIN","ffmpeg"),
        yt_dlp_bin=_s("YT_DLP_BIN","yt-dlp"),
        rtmp_url=_s("TIKTOK_RTMP_URL"),
        video_size=_s("TIKTOK_VIDEO_SIZE","1280x720"),
        fps=_i("TIKTOK_FPS",30),
        video_bitrate=_s("TIKTOK_VIDEO_BITRATE","4500k"),
        audio_bitrate=_i("TIKTOK_AUDIO_BITRATE",128),
        sample_rate=_i("TIKTOK_SAMPLE_RATE",48000),
        channels=_i("TIKTOK_CHANNELS",2),
        reconnect_delay=_i("RECONNECT_DELAY",5),
        monitor_interval=_i("MONITOR_INTERVAL",5),
        queue_size=_i("AUDIO_QUEUE_SIZE",4000),
        log_level=_s("LOG_LEVEL","INFO"),
        user_agent=_s("USER_AGENT","Mozilla/5.0"),
        enable_hwaccel=_b("ENABLE_HWACCEL",False),
    )
