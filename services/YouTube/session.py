from __future__ import annotations

import asyncio

from .models import Session


class SessionStore:
    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}
        self._locks: dict[int, asyncio.Lock] = {}

    def lock(self, chat_id: int) -> asyncio.Lock:
        return self._locks.setdefault(int(chat_id), asyncio.Lock())

    def get(self, chat_id: int) -> Session:
        return self._sessions.get(int(chat_id), Session(chat_id=int(chat_id)))

    def put(self, session: Session) -> Session:
        self._sessions[int(session.chat_id)] = session
        return session

    def remove(self, chat_id: int) -> None:
        self._sessions.pop(int(chat_id), None)

    def values(self) -> list[Session]:
        return list(self._sessions.values())

    def count_active(self) -> int:
        return sum(s.status in {"playing", "paused"} for s in self._sessions.values())