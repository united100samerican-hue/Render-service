from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("tiktok_bridge")


@dataclass
class BridgeResult:
    ok: bool
    error: str = ""
    state: dict[str, Any] | None = None


class TikTokBridge:
    """
    Extension point for a future Telegram->TikTok relay pipeline.

    The repository currently contains a working TikTok-live-to-Telegram path,
    but not a proven relay implementation from Telegram voice chat into TikTok.
    Returning a controlled error keeps the service stable until that pipeline
    is provided.
    """

    def __init__(self) -> None:
        self.active = False

    async def start(self, *args, **kwargs) -> BridgeResult:
        self.active = False
        return BridgeResult(ok=False, error="bridge_mode_not_supported")

    async def stop(self, *args, **kwargs) -> BridgeResult:
        self.active = False
        return BridgeResult(ok=True, state={"status": "stopped"})

    async def state(self) -> dict[str, Any]:
        return {"status": "idle", "bridge": False}