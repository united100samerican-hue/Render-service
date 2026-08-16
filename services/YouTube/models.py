from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(slots=True)
class MediaInfo:
    url: str
    webpage_url: str = ""
    title: str = ""
    duration: int = 0
    thumbnail: str = ""
    extractor: str = ""
    is_live: bool = False
    has_video: bool = False
    has_audio: bool = False
    direct_url: str = ""
    direct_ext: str = ""
    source_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Session:
    chat_id: int
    status: str = "idle"
    title: str = ""
    source_url: str = ""
    duration: int = 0
    position: int = 0
    thumbnail: str = ""
    extractor: str = ""
    source_id: str = ""
    video: bool = False
    live: bool = False
    local_path: str = ""
    direct_url: str = ""
    updated_at: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MediaRequest:
    chat_id: int
    url: str
    title: str = ""
    duration: int = 0
    offset: int = 0
    by: str = ""
