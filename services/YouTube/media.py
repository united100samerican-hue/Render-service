from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path


class MediaTools:
    async def duration(self, path: str) -> int:
        try:
            proc = await asyncio.to_thread(
                subprocess.run,
                [
                    "ffprobe", "-v", "error", "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1", path,
                ],
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            if proc.returncode != 0:
                return 0
            return max(0, int(round(float((proc.stdout or "").strip() or 0))))
        except Exception:
            return 0

    async def size_ok(self, path: str, max_bytes: int) -> bool:
        try:
            return Path(path).stat().st_size <= max_bytes
        except Exception:
            return False
