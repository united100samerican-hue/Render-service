from __future__ import annotations
from dataclasses import dataclass, asdict
from typing import Any
@dataclass
class AudioSession:
    chat_id:int
    status:str='idle'
    title:str=''
    source_type:str=''
    source_id:str=''
    source_chat_id:str=''
    source_message_id:str=''
    source_url:str=''
    duration:int=0
    position:int=0
    paused_at:int=0
    started_at:float=0.0
    video:bool=False
    media_kind:str='audio'
    live:bool=False
    muted:bool=False
    thumbnail:str=''
    webpage_url:str=''
    last_error:str=''
    local_path:str=''
    updated_at:float=0.0
    def to_dict(self)->dict[str,Any]:
        return asdict(self)
