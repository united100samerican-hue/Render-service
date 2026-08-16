from __future__ import annotations
from dataclasses import dataclass
import os
from pathlib import Path

@dataclass(frozen=True)
class Settings:
    api_id:int; api_hash:str; session_string:str; keepalive_secret:str
    media_dir:Path; max_media_bytes:int; max_duration:int; request_timeout:int; max_height:int
    @classmethod
    def from_env(cls)->'Settings':
        d=Path(os.getenv('SOCIAL_MEDIA_DIR','/tmp/render_social_media'));d.mkdir(parents=True,exist_ok=True)
        return cls(int(os.getenv('API_ID','0') or 0),os.getenv('API_HASH','').strip(),os.getenv('SESSION_STRING','').strip(),os.getenv('KEEPALIVE_SECRET','').strip(),d,max(8*1024*1024,int(os.getenv('SOCIAL_MAX_MEDIA_BYTES',str(1024*1024*1024))),),max(0,int(os.getenv('SOCIAL_MAX_DURATION',str(6*60*60))),),max(15,int(os.getenv('SOCIAL_REQUEST_TIMEOUT','120'))),max(360,int(os.getenv('SOCIAL_MAX_HEIGHT','720'))))
