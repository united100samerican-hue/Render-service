from __future__ import annotations
from dataclasses import dataclass,asdict
from typing import Any
@dataclass
class MediaInfo:
    url:str;webpage_url:str='';title:str='';duration:int=0;thumbnail:str='';extractor:str='';is_live:bool=False;has_video:bool=False;has_audio:bool=True;direct_url:str='';direct_ext:str='';source_id:str=''
    def to_dict(self)->dict[str,Any]: return asdict(self)
@dataclass
class Session:
    chat_id:int;status:str='idle';title:str='';source_url:str='';duration:int=0;position:int=0;thumbnail:str='';extractor:str='';source_id:str='';video:bool=False;live:bool=False;local_path:str='';direct_url:str='';updated_at:float=0.0;error:str=''
    def to_dict(self)->dict[str,Any]: return asdict(self)
@dataclass(frozen=True)
class MediaRequest:
    chat_id:int;url:str;title:str='';duration:int=0;offset:int=0;by:str=''
