from __future__ import annotations
import asyncio
from .models import Session
class SessionStore:
    def __init__(self): self._sessions:dict[int,Session]={};self._locks:dict[int,asyncio.Lock]={}
    def lock(self,chat_id:int)->asyncio.Lock: return self._locks.setdefault(int(chat_id),asyncio.Lock())
    def get(self,chat_id:int)->Session: return self._sessions.get(int(chat_id),Session(chat_id=int(chat_id)))
    def put(self,s:Session)->Session: self._sessions[int(s.chat_id)]=s;return s
    def remove(self,chat_id:int)->None: self._sessions.pop(int(chat_id),None)
    def values(self): return list(self._sessions.values())
