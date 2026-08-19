from __future__ import annotations
import re
from pathlib import Path
from typing import Any
import httpx

AUDIO_EXTS = {'.mp3','.ogg','.oga','.wav','.m4a','.aac','.flac','.opus','.webm'}
VIDEO_EXTS = {'.mp4','.mkv','.mov','.webm','.m4v','.avi'}

class TelegramMedia:
    def __init__(self, bot_token: str, client: Any, base_dir: Path):
        self.bot_token = bot_token
        self.client = client
        self.base_dir = base_dir

    @staticmethod
    def kind(name: str = '', mime: str = '') -> tuple[bool,str]:
        ext = Path(name).suffix.lower()
        mime = mime.lower().strip()
        if mime.startswith('video/') or ext in VIDEO_EXTS:
            return True, 'video'
        return False, 'audio'

    async def from_file_id(self, file_id: str, source_type: str, title: str = '') -> tuple[Path,bool,str]:
        if not self.bot_token:
            raise RuntimeError('missing_env: BOT_TOKEN')
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.get(f'https://api.telegram.org/bot{self.bot_token}/getFile', params={'file_id':file_id})
            r.raise_for_status()
            data = r.json()
            if not data.get('ok'):
                raise RuntimeError(f"telegram_getFile_failed: {data}")
            file_path = str(data['result']['file_path'])
            original = Path(file_path).name or title or file_id
            st = str(source_type or '').lower()
            video, kind = (st == 'telegram_video', 'video' if st == 'telegram_video' else 'audio') if st in {'telegram_video','telegram_audio'} else self.kind(original)
            safe = re.sub(r'[^A-Za-z0-9._-]+','_',file_id)[:120]
            out = self.base_dir / f'{safe}{Path(original).suffix.lower() or (".mp4" if video else ".ogg")}'
            fr = await c.get(f'https://api.telegram.org/file/bot{self.bot_token}/{file_path}')
            fr.raise_for_status()
            out.write_bytes(fr.content)
            return out, video, kind

    async def from_message(self, chat_id: int, message_id: int, title: str = '') -> tuple[Path,bool,str]:
        msg = await self.client.get_messages(int(chat_id), ids=int(message_id))
        if not msg or not msg.media:
            raise RuntimeError('telegram_message_media_not_found')
        out = self.base_dir / f'tg_{int(chat_id)}_{int(message_id)}'
        path = await msg.download_media(file=str(out))
        if not path:
            raise RuntimeError('telegram_message_download_failed')
        video, kind = self.kind(Path(path).name, '')
        return Path(path), video, kind
