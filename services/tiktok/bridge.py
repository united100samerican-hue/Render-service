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
    Extension point for Telegram->TikTok relay pipeline.
    Currently implements TikTok-live-to-Telegram streaming only.
    """

    def __init__(self) -> None:
        self.active = False

    async def start(self, *args, **kwargs) -> BridgeResult:
        self.active = False
        return BridgeResult(
            ok=False, 
            error="bridge_mode_not_supported"
        )

    async def stop(self, *args, **kwargs) -> BridgeResult:
        self.active = False
        return BridgeResult(ok=True, state={"status": "stopped"})

    async def state(self) -> dict[str, Any]:
        return {"status": "idle", "bridge": False}