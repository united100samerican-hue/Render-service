from __future__ import annotations
import time
from dataclasses import dataclass,field
from typing import Any

@dataclass(slots=True)
class StreamInfo:
    title:str=""
    author:str=""
    stream_url:str=""
    video_url:str=""
    audio_url:str=""
    thumbnail:str=""
    webpage_url:str=""
    duration:int=0
    viewers:int=0
    live:bool=False
    updated_at:float=field(default_factory=time.time)

@dataclass(slots=True)
class PlayerState:
    running:bool=False
    connected:bool=False
    restarting:bool=False
    ffmpeg_pid:int=0
    group_call:Any=None
    started_at:float=0.0
    last_frame_at:float=0.0
    last_error:str=""

@dataclass(slots=True)
class BridgeState:
    enabled:bool=False
    running:bool=False
    ffmpeg_pid:int=0
    audio_frames:int=0
    started_at:float=0.0
    last_packet_at:float=0.0
    last_error:str=""

@dataclass(slots=True)
class SessionState:
    chat_id:int
    title:str="TikTok Live"
    source_url:str=""
    rtmp_url:str=""
    status:str="idle"
    join_as:Any=None
    invite_hash:str|None=None
    created_at:float=field(default_factory=time.time)
    updated_at:float=field(default_factory=time.time)
    stream:StreamInfo=field(default_factory=StreamInfo)
    player:PlayerState=field(default_factory=PlayerState)
    bridge:BridgeState=field(default_factory=BridgeState)

    def touch(self)->None:
        self.updated_at=time.time()

    @property
    def active(self)->bool:
        return self.player.running

    @property
    def bridge_enabled(self)->bool:
        return self.bridge.enabled

    @property
    def duration(self)->int:
        if not self.player.started_at:
            return 0
        return int(time.time()-self.player.started_at)

    def public(self)->dict[str,Any]:
        return{
            "chat_id":self.chat_id,
            "title":self.title,
            "status":self.status,
            "source_url":self.source_url,
            "rtmp_url":self.rtmp_url,
            "duration":self.duration,
            "created_at":int(self.created_at),
            "updated_at":int(self.updated_at),
            "stream":{
                "title":self.stream.title,
                "author":self.stream.author,
                "thumbnail":self.stream.thumbnail,
                "viewers":self.stream.viewers,
                "live":self.stream.live
            },
            "player":{
                "running":self.player.running,
                "connected":self.player.connected,
                "restarting":self.player.restarting,
                "ffmpeg_pid":self.player.ffmpeg_pid,
                "last_error":self.player.last_error
            },
            "bridge":{
                "enabled":self.bridge.enabled,
                "running":self.bridge.running,
                "ffmpeg_pid":self.bridge.ffmpeg_pid,
                "audio_frames":self.bridge.audio_frames,
                "last_error":self.bridge.last_error
            }
        }
