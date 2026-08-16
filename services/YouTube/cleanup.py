from __future__ import annotations

import asyncio
from pathlib import Path
import shutil


class Cleaner:
    async def remove(self, path: str) -> None:
        if not path:
            return
        try:
            await asyncio.to_thread(Path(path).unlink, missing_ok=True)
        except Exception:
            return

    async def clear_dir(self, directory: Path) -> None:
        def clear() -> None:
            if not directory.exists():
                return
            for path in directory.iterdir():
                try:
                    if path.is_file() or path.is_symlink():
                        path.unlink(missing_ok=True)
                    elif path.is_dir():
                        shutil.rmtree(path, ignore_errors=True)
                except Exception:
                    continue

        try:
            await asyncio.to_thread(clear)
        except Exception:
            return
