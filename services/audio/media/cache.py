from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from config import (
    AUDIO_CACHE_ENABLED,
    R2_ACCESS_KEY_ID,
    R2_BUCKET,
    R2_ENDPOINT,
    R2_PRESIGN_SECONDS,
    R2_SECRET_ACCESS_KEY,
)


class R2AudioCache:
    def __init__(self) -> None:
        self.enabled = AUDIO_CACHE_ENABLED
        self.bucket = R2_BUCKET
        self.presign_seconds = R2_PRESIGN_SECONDS
        self.client: Any | None = None

        if self.enabled:
            self.client = boto3.client(
                "s3",
                endpoint_url=R2_ENDPOINT,
                aws_access_key_id=R2_ACCESS_KEY_ID,
                aws_secret_access_key=R2_SECRET_ACCESS_KEY,
                region_name="auto",
                config=BotoConfig(
                    signature_version="s3v4",
                    retries={"max_attempts": 3, "mode": "standard"},
                ),
            )

    @staticmethod
    def key_for(*parts: object, suffix: str = ".bin") -> str:
        raw = "|".join(str(x or "").strip() for x in parts)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        safe_suffix = suffix if suffix.startswith(".") else f".{suffix}"
        return f"audio/{digest}{safe_suffix}"

    async def exists(self, key: str) -> bool:
        if not self.enabled or not self.client or not key:
            return False

        def _head() -> bool:
            try:
                self.client.head_object(Bucket=self.bucket, Key=key)
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_head)

    async def upload(self, path: str | Path, key: str) -> bool:
        if not self.enabled or not self.client or not key:
            return False

        source = Path(path)
        if not source.is_file():
            return False

        content_type = mimetypes.guess_type(source.name)[0] or "application/octet-stream"

        def _put() -> bool:
            try:
                self.client.upload_file(
                    str(source),
                    self.bucket,
                    key,
                    ExtraArgs={"ContentType": content_type},
                )
                return True
            except Exception:
                return False

        return await asyncio.to_thread(_put)

    async def download(self, key: str, destination: str | Path) -> bool:
        if not self.enabled or not self.client or not key:
            return False

        dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)

        def _get() -> bool:
            try:
                self.client.download_file(self.bucket, key, str(dest))
                return True
            except Exception:
                try:
                    dest.unlink(missing_ok=True)
                except Exception:
                    pass
                return False

        return await asyncio.to_thread(_get)

    async def presigned_url(self, key: str) -> str:
        if not self.enabled or not self.client or not key:
            return ""

        def _sign() -> str:
            try:
                return str(
                    self.client.generate_presigned_url(
                        "get_object",
                        Params={"Bucket": self.bucket, "Key": key},
                        ExpiresIn=self.presign_seconds,
                    )
                )
            except Exception:
                return ""

        return await asyncio.to_thread(_sign)

    async def delete(self, key: str) -> None:
        if not self.enabled or not self.client or not key:
            return

        def _delete() -> None:
            try:
                self.client.delete_object(Bucket=self.bucket, Key=key)
            except Exception:
                pass

        await asyncio.to_thread(_delete)