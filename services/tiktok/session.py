from __future__ import annotations
import asyncio
import time
from typing import Dict
from models import SessionState

class SessionManager:

    def __init__(self)->None:
        self._sessions:Dict[int,SessionState]={}
        self._locks:Dict[int,asyncio.Lock]={}

    def get(self,chat_id:int)->SessionState:
        chat_id=int(chat_id)
        if chat_id not in self._sessions:
            self._sessions[chat_id]=SessionState(chat_id=chat_id)
        return self._sessions[chat_id]

    def exists(self,chat_id:int)->bool:
        return int(chat_id) in self._sessions

    def create(self,chat_id:int)->SessionState:
        chat_id=int(chat_id)
        if chat_id not in self._sessions:
            self._sessions[chat_id]=SessionState(chat_id=chat_id)
        return self._sessions[chat_id]

    def remove(self,chat_id:int)->None:
        self._sessions.pop(int(chat_id),None)
        self._locks.pop(int(chat_id),None)

    def lock(self,chat_id:int)->asyncio.Lock:
        chat_id=int(chat_id)
        if chat_id not in self._locks:
            self._locks[chat_id]=asyncio.Lock()
        return self._locks[chat_id]

    def all(self)->list[SessionState]:
        return list(self._sessions.values())

    def active(self)->list[SessionState]:
        return [s for s in self._sessions.values() if s.player.running]

    def bridge_enabled(self)->list[SessionState]:
        return [s for s in self._sessions.values() if s.bridge.enabled]

    def count(self)->int:
        return len(self._sessions)

    def touch(self,chat_id:int)->None:
        self.get(chat_id).updated_at=time.time()

    def update_stream(self,chat_id:int,**kwargs)->SessionState:
        s=self.get(chat_id)
        for k,v in kwargs.items():
            if hasattr(s.stream,k):
                setattr(s.stream,k,v)
        s.touch()
        return s

    def update_player(self,chat_id:int,**kwargs)->SessionState:
        s=self.get(chat_id)
        for k,v in kwargs.items():
            if hasattr(s.player,k):
                setattr(s.player,k,v)
        s.touch()
        return s

    def update_bridge(self,chat_id:int,**kwargs)->SessionState:
        s=self.get(chat_id)
        for k,v in kwargs.items():
            if hasattr(s.bridge,k):
                setattr(s.bridge,k,v)
        s.touch()
        return s

    def state(self,chat_id:int)->dict:
        return self.get(chat_id).public()

    def reset(self,chat_id:int)->SessionState:
        self._sessions[int(chat_id)]=SessionState(chat_id=int(chat_id))
        return self._sessions[int(chat_id)]

sessions=SessionManager()
