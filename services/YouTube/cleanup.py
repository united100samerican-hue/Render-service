from __future__ import annotations
import asyncio,shutil
from pathlib import Path
class Cleaner:
    async def remove(self,path:str)->None:
        if not path:return
        try: await asyncio.to_thread(Path(path).unlink,missing_ok=True)
        except Exception: pass
    async def clear(self,directory:Path)->None:
        def _clear():
            for p in directory.iterdir():
                try:
                    shutil.rmtree(p,ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
                except Exception: pass
        try: await asyncio.to_thread(_clear)
        except Exception: pass
