from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from service import (
    AudioService,
    ControlRequest,
    MetaRequest,
    QueueAddRequest,
    SeekRequest,
    StartRequest,
    service,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_app")

app = FastAPI(title="Render Audio Service", version="4.0")

KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()


def _guard(secret: str | None) -> None:
    if KEEPALIVE_SECRET and (secret or "").strip() != KEEPALIVE_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


def _pick(body: dict[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in body and body[name] is not None:
            return body[name]
    return default


def _make_meta(body: dict[str, Any]) -> MetaRequest:
    return MetaRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        source_type=str(_pick(body, "source_type", "sourceType", default="url")),
        source_id=str(_pick(body, "source_id", "sourceId", default="")),
        title=str(_pick(body, "title", default="")),
        duration=_coerce_int(_pick(body, "duration", default=0)),
    )


def _make_start(body: dict[str, Any]) -> StartRequest:
    return StartRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        source_type=str(_pick(body, "source_type", "sourceType", default="url")),
        source_id=str(_pick(body, "source_id", "sourceId", default="")),
        title=str(_pick(body, "title", default="")),
        duration=_coerce_int(_pick(body, "duration", default=0)),
        offset=_coerce_int(_pick(body, "offset", default=0)),
    )


def _make_control(body: dict[str, Any]) -> ControlRequest:
    return ControlRequest(chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)))


def _make_seek(body: dict[str, Any]) -> SeekRequest:
    return SeekRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        delta=_coerce_int(_pick(body, "delta", default=0)),
    )


def _make_queue_add(body: dict[str, Any]) -> QueueAddRequest:
    return QueueAddRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        source_type=str(_pick(body, "source_type", "sourceType", default="url")),
        source_id=str(_pick(body, "source_id", "sourceId", default="")),
        title=str(_pick(body, "title", default="")),
        duration=_coerce_int(_pick(body, "duration", default=0)),
        requested_by=str(_pick(body, "requested_by", "requestedBy", default="")),
        auto_start=bool(_pick(body, "auto_start", "autoStart", default=True)),
    )


@app.on_event("startup")
async def _startup() -> None:
    try:
        await service.ensure_ready()
        logger.info("startup_done", extra={"ready": service.ready, "backend_error": service.backend_error})
    except Exception:
        logger.exception("audio startup failed")


@app.get("/")
async def root():
    return {"ok": True, "service": "audio"}


@app.get("/ping")
async def ping():
    return {"ok": True, "service": "audio", "ping": True}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "ready": service.ready,
        "backend_error": service.backend_error,
        "active_sessions": service.active_sessions_count(),
        "queue_items": service.queues_count(),
    }


@app.post("/meta")
async def meta(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    return await service.meta(_make_meta(body))


@app.post("/start")
async def start(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    try:
        return await service.start(_make_start(body))
    except Exception as e:
        logger.exception("audio start failed", extra={"body": body})
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/pause")
async def pause(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    return await service.pause(_make_control(body).chat_id)


@app.post("/resume")
async def resume(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    return await service.resume(_make_control(body).chat_id)


@app.post("/stop")
async def stop(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    return await service.stop(_make_control(body).chat_id)


@app.post("/seek")
async def seek(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    seek_req = _make_seek(body)
    return await service.seek(seek_req.chat_id, seek_req.delta)


@app.post("/enqueue")
async def enqueue(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    try:
        return await service.enqueue(_make_queue_add(body))
    except Exception as e:
        logger.exception("audio enqueue failed", extra={"body": body})
        raise HTTPException(status_code=500, detail=f"{type(e).__name__}: {e}")


@app.post("/queue")
async def queue(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    chat_id = _coerce_int(_pick(body, "chatId", "chat_id", default=0))
    return await service.queue_list(chat_id)


@app.post("/clear")
async def clear(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    chat_id = _coerce_int(_pick(body, "chatId", "chat_id", default=0))
    return await service.queue_clear(chat_id)


@app.post("/skip")
async def skip(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    chat_id = _coerce_int(_pick(body, "chatId", "chat_id", default=0))
    return await service.skip(chat_id)


@app.get("/state/{chat_id}")
async def state(chat_id: int):
    return service.state(chat_id)