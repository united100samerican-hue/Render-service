from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx


AUDIO_EXTS = {
    ".mp3",
    ".ogg",
    ".oga",
    ".wav",
    ".m4a",
    ".aac",
    ".flac",
    ".opus",
    ".webm",
}

VIDEO_EXTS = {
    ".mp4",
    ".mkv",
    ".mov",
    ".webm",
    ".m4v",
    ".avi",
}


class TelegramMedia:
    def __init__(self, bot_token: str, client: Any, base_dir: Path):
        self.bot_token = bot_token
        self.client = client
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def kind(name: str = "", mime: str = "") -> tuple[bool, str]:
        ext = Path(name).suffix.lower()
        mime = mime.lower().strip()

        if mime.startswith("video/") or ext in VIDEO_EXTS:
            return True, "video"

        return False, "audio"

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or ""))[:120]

    async def from_file_id(
        self,
        file_id: str,
        source_type: str,
        title: str = "",
    ) -> tuple[Path, bool, str]:
        if not self.bot_token:
            raise RuntimeError("missing_env: BOT_TOKEN")

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=20.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(
                f"https://api.telegram.org/bot{self.bot_token}/getFile",
                params={"file_id": file_id},
            )
            response.raise_for_status()

            data = response.json()

            if not data.get("ok"):
                raise RuntimeError(f"telegram_getFile_failed: {data}")

            file_path = str(data["result"]["file_path"])
            original = Path(file_path).name or title or file_id

            source_kind = str(source_type or "").lower().strip()

            if source_kind in {"telegram_video", "telegram_audio"}:
                video = source_kind == "telegram_video"
                kind = "video" if video else "audio"
            else:
                video, kind = self.kind(original)

            suffix = Path(original).suffix.lower()
            if not suffix:
                suffix = ".mp4" if video else ".ogg"

            safe = self._safe_name(file_id)
            out = self.base_dir / f"{safe}{suffix}"

            async with client.stream(
                "GET",
                f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}",
            ) as media:
                media.raise_for_status()

                with out.open("wb") as target:
                    async for chunk in media.aiter_bytes(1024 * 1024):
                        if chunk:
                            target.write(chunk)

            return out, video, kind

    async def from_message(
        self,
        chat_id: int,
        message_id: int,
        title: str = "",
    ) -> tuple[Path, bool, str]:
        message = await self.client.get_messages(
            int(chat_id),
            ids=int(message_id),
        )

        if not message or not message.media:
            raise RuntimeError("telegram_message_media_not_found")

        out = self.base_dir / f"tg_{int(chat_id)}_{int(message_id)}"

        path = await message.download_media(file=str(out))

        if not path:
            raise RuntimeError("telegram_message_download_failed")

        result = Path(path)

        mime = ""
        document = getattr(message.media, "document", None)
        if document:
            mime = str(getattr(document, "mime_type", "") or "")

        video, kind = self.kind(result.name, mime)

        return result, video, kind