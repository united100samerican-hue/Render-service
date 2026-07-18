from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("tiktok_bridge")


@dataclass
class BridgeResult:
    ok: bool
    error: str = ""
    state: dict[str, Any] | None = None


class TikTokBridge:
    def __init__(self) -> None:
        self.active = False
        self._state: dict[str, Any] = {"status": "idle", "bridge": False}
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None

    def _base(self) -> str:
        return os.getenv("TIKTOK_RELAY_URL", "").strip().rstrip("/")

    def _secret(self) -> str:
        return os.getenv("TIKTOK_RELAY_SECRET", "").strip()

    def _timeout(self) -> float:
        try:
            return max(5.0, float(os.getenv("TIKTOK_RELAY_TIMEOUT", "30")))
        except Exception:
            return 30.0

    def _headers(self) -> dict[str, str]:
        h = {"content-type": "application/json"}
        s = self._secret()
        if s:
            h["x-keepalive-secret"] = s
        return h

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout(), follow_redirects=True)
        return self._client

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        base = self._base()
        if not base:
            raise RuntimeError("bridge_backend_missing")
        client = await self._ensure_client()
        r = await client.request(method, f"{base}{path}", json=payload or {}, headers=self._headers())
        r.raise_for_status()
        try:
            data = r.json()
            return data if isinstance(data, dict) else {"ok": True, "data": data}
        except Exception:
            return {"ok": True, "text": r.text}

    async def start(self, chat_id: int, source_url: str, title: str = "") -> BridgeResult:
        async with self._lock:
            try:
                payload = {
                    "chat_id": int(chat_id),
                    "source_url": str(source_url or "").strip().rstrip("/"),
                    "title": str(title or "").strip(),
                }
                j = await self._request("POST", "/bridge/start", payload)
                if not bool(j.get("ok", True)):
                    return BridgeResult(ok=False, error=str(j.get("error", "bridge_start_failed")), state=j.get("state"))
                self.active = True
                self._state.update({
                    "status": "playing",
                    "bridge": True,
                    "chat_id": int(chat_id),
                    "source_url": payload["source_url"],
                    "title": payload["title"],
                })
                if isinstance(j.get("state"), dict):
                    self._state.update(j["state"])
                return BridgeResult(ok=True, state=self._state.copy())
            except Exception as e:
                logger.exception("bridge_start_failed")
                return BridgeResult(ok=False, error=f"{type(e).__name__}: {e}", state=self._state.copy())

    async def stop(self, chat_id: int | None = None) -> BridgeResult:
        async with self._lock:
            try:
                try:
                    await self._request("POST", "/bridge/stop", {"chat_id": int(chat_id or 0)})
                except RuntimeError as e:
                    if str(e) != "bridge_backend_missing":
                        raise
                self.active = False
                self._state = {"status": "stopped", "bridge": False, "chat_id": int(chat_id or 0)}
                return BridgeResult(ok=True, state=self._state.copy())
            except Exception as e:
                logger.exception("bridge_stop_failed")
                return BridgeResult(ok=False, error=f"{type(e).__name__}: {e}", state=self._state.copy())

    async def enable_bridge(self, chat_id: int, source_url: str, title: str = "") -> BridgeResult:
        return await self.start(chat_id=chat_id, source_url=source_url, title=title)

    async def disable_bridge(self, chat_id: int | None = None) -> BridgeResult:
        return await self.stop(chat_id=chat_id)

    async def state(self) -> dict[str, Any]:
        async with self._lock:
            try:
                j = await self._request("GET", "/bridge/state")
                if isinstance(j, dict):
                    if isinstance(j.get("state"), dict):
                        self._state.update(j["state"])
                    else:
                        self._state.update({k: v for k, v in j.items() if k != "ok"})
                self._state["bridge"] = bool(self.active or self._state.get("bridge"))
                return self._state.copy()
            except RuntimeError:
                return self._state.copy()
            except Exception as e:
                logger.warning("bridge_state_failed: %s", e)
                return self._state.copy()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None