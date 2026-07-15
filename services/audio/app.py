from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from service import service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_app")

app = FastAPI(title="Render Audio Service", version="4.0")

KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()
ALLOWED_SOURCE_TYPES = {"telegram", "telegram_file_id", "telegram_audio", "telegram_video", "file_id", "video_id"}

SOURCE_TYPE_ALIASES = {
    "telegram": "telegram_file_id",
    "tg": "telegram_file_id",
    "telegram_file_id": "telegram_file_id",
    "telegram_media": "telegram_file_id",
    "telegram_document": "telegram_file_id",
    "document": "telegram_file_id",
    "media": "telegram_file_id",
    "file": "telegram_file_id",

    "audio": "telegram_audio",
    "voice": "telegram_audio",
    "song": "telegram_audio",
    "music": "telegram_audio",
    "telegram_audio": "telegram_audio",

    "video": "telegram_video",
    "clip": "telegram_video",
    "movie": "telegram_video",
    "telegram_video": "telegram_video",

    "file_id": "file_id",
    "telegram_file": "file_id",

    "video_id": "video_id",
    "youtube": "video_id",
    "yt": "video_id",
    "youtube_video": "video_id",
}


def _guard(secret: str | None) -> None:
    if KEEPALIVE_SECRET and (secret or "").strip() != KEEPALIVE_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")


async def _json(req: Request) -> dict[str, Any]:
    try:
        body = await req.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _pick(body: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in body and body[name] is not None:
            return body[name]
    return default


def _chat_id(body: dict[str, Any]) -> int:
    try:
        return int(_pick(body, "chatId", "chat_id", default=0))
    except Exception:
        return 0


def _source_id(body: dict[str, Any]) -> str:
    return str(_pick(body, "sourceId", "source_id", default="")).strip()


def _source_type(body: dict[str, Any]) -> str:
    raw = str(_pick(body, "sourceType", "source_type", default="")).strip().lower()
    mime = str(_pick(body, "mimeType", "mime_type", default="")).strip().lower()
    kind = str(_pick(body, "mediaType", "media_type", "type", default="")).strip().lower()

    if not raw:
        if mime.startswith("audio/") or kind in {"audio", "voice", "music", "song"}:
            raw = "audio"
        elif mime.startswith("video/") or kind in {"video", "clip", "movie"}:
            raw = "video"
        elif kind in {"document", "file", "media"}:
            raw = "document"
        else:
            raw = "telegram_file_id"

    raw = raw.replace("-", "_")
    st = SOURCE_TYPE_ALIASES.get(raw, raw)

    if st not in ALLOWED_SOURCE_TYPES:
        if raw in {"audio", "voice", "song", "music"}:
            st = "telegram_audio"
        elif raw in {"video", "clip", "movie"}:
            st = "telegram_video"
        elif raw in {"document", "file", "media", "telegram_document"}:
            st = "telegram_file_id"
        elif raw in {"youtube", "yt", "video_id", "youtube_video"}:
            st = "video_id"
        else:
            raise HTTPException(status_code=400, detail=f"unsupported_source_type: {raw}")

    return st


def _title(body: dict[str, Any]) -> str:
    return str(_pick(body, "title", default="")).strip()


def _int(body: dict[str, Any], *names: str, default: int = 0) -> int:
    try:
        return int(_pick(body, *names, default=default))
    except Exception:
        return default


def _bool(body: dict[str, Any], *names: str, default: bool = False) -> bool:
    v = _pick(body, *names, default=default)
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(v)


@app.on_event("startup")
async def _startup() -> None:
    try:
        await service.ensure_ready()
        logger.info("startup_done")
    except Exception:
        logger.exception("startup_failed")


@app.on_event("shutdown")
async def _shutdown() -> None:
    try:
        await service.close()
    except Exception:
        logger.exception("shutdown_failed")


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "render-audio-service", "ready": service.ready}


@app.get("/ping")
async def ping(x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    return {"ok": True}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "ready": service.ready,
        "backend_error": service.backend_error,
        "active_sessions": service.active_sessions_count(),
        "queues": service.queues_count(),
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "ready": True}


@app.get("/state/{chat_id}")
async def state(chat_id: int) -> dict[str, Any]:
    return service.state(chat_id)


@app.post("/meta")
async def meta(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.meta(chat_id, _source_type(body), _source_id(body), title=_title(body), duration=_int(body, "duration", default=0))
    except Exception as e:
        logger.exception("meta failed")
        return {"ok": False, "action": "meta", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/start")
async def start(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.start(
            chat_id,
            _source_type(body),
            _source_id(body),
            title=_title(body),
            duration=_int(body, "duration", default=0),
            offset=_int(body, "offset", default=0),
        )
    except Exception as e:
        logger.exception("start failed")
        return {"ok": False, "action": "start", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/pause")
async def pause(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.pause(chat_id)
    except Exception as e:
        logger.exception("pause failed")
        return {"ok": False, "action": "pause", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/resume")
async def resume(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.resume(chat_id)
    except Exception as e:
        logger.exception("resume failed")
        return {"ok": False, "action": "resume", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/stop")
async def stop(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        result = await service.stop(chat_id)
        if not result.get("ok", False):
            return JSONResponse(status_code=502, content=result)
        return result
    except Exception as e:
        logger.exception("stop failed")
        return JSONResponse(status_code=502, content={"ok": False, "action": "stop", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)})


@app.post("/seek")
async def seek(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.seek(chat_id, _int(body, "delta", default=0))
    except Exception as e:
        logger.exception("seek failed")
        return {"ok": False, "action": "seek", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/enqueue")
async def enqueue(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.enqueue(
            chat_id,
            _source_type(body),
            _source_id(body),
            title=_title(body),
            duration=_int(body, "duration", default=0),
            requested_by=str(_pick(body, "requestedBy", "requested_by", default="")).strip(),
            auto_start=_bool(body, "autoStart", "auto_start", default=True),
        )
    except Exception as e:
        logger.exception("enqueue failed")
        return {"ok": False, "action": "enqueue", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/queue")
async def queue(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.queue_list(chat_id)
    except Exception as e:
        logger.exception("queue failed")
        return {"ok": False, "action": "queue_list", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/queue/list")
async def queue_list(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    return await queue(req, x_keepalive_secret)


@app.post("/queue/clear")
async def queue_clear(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.queue_clear(chat_id)
    except Exception as e:
        logger.exception("queue_clear failed")
        return {"ok": False, "action": "queue_clear", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/queue/skip")
async def queue_skip(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.skip(chat_id)
    except Exception as e:
        logger.exception("queue_skip failed")
        return {"ok": False, "action": "skip", "error": f"{type(e).__name__}: {e}", "state": service.state(chat_id)}


@app.post("/queue/next")
async def queue_next(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    return await queue_skip(req, x_keepalive_secret)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)