from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, Request

from service import AudioService, ControlRequest, MetaRequest, QueueAddRequest, SeekRequest, StartRequest, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_app")

app = FastAPI(title="Render Audio Service", version="5.0")

KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()


def _guard(secret: str | None) -> None:
    if KEEPALIVE_SECRET and (secret or "").strip() != KEEPALIVE_SECRET:
        raise RuntimeError("forbidden")


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _pick(body: dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        if n in body and body[n] is not None:
            return body[n]
    return default


def _meta(body: dict[str, Any]) -> MetaRequest:
    return MetaRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        source_type=str(_pick(body, "source_type", "sourceType", default="telegram")),
        source_id=str(_pick(body, "source_id", "sourceId", default="")),
        title=str(_pick(body, "title", default="")),
        duration=_coerce_int(_pick(body, "duration", default=0)),
    )


def _start(body: dict[str, Any]) -> StartRequest:
    return StartRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        source_type=str(_pick(body, "source_type", "sourceType", default="telegram")),
        source_id=str(_pick(body, "source_id", "sourceId", default="")),
        title=str(_pick(body, "title", default="")),
        duration=_coerce_int(_pick(body, "duration", default=0)),
        offset=_coerce_int(_pick(body, "offset", default=0)),
    )


def _control(body: dict[str, Any]) -> ControlRequest:
    return ControlRequest(chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)))


def _seek(body: dict[str, Any]) -> SeekRequest:
    return SeekRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        delta=_coerce_int(_pick(body, "delta", default=0)),
    )


def _queue(body: dict[str, Any]) -> QueueAddRequest:
    return QueueAddRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        source_type=str(_pick(body, "source_type", "sourceType", default="telegram")),
        source_id=str(_pick(body, "source_id", "sourceId", default="")),
        title=str(_pick(body, "title", default="")),
        duration=_coerce_int(_pick(body, "duration", default=0)),
        requested_by=str(_pick(body, "requested_by", "requestedBy", default="")),
        auto_start=bool(_pick(body, "auto_start", "autoStart", default=True)),
    )


@app.on_event("startup")
async def _startup() -> None:
    await service.ensure_ready()
    logger.info("startup_done", extra={"ready": service.ready, "backend_error": service.backend_error})


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
async def meta(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        return await service.meta(_meta(await req.json()))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/start")
async def start(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        return await service.start(_start(await req.json()))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/pause")
async def pause(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        return await service.pause(_control(await req.json()).chat_id)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/resume")
async def resume(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        return await service.resume(_control(await req.json()).chat_id)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/stop")
async def stop(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        return await service.stop(_control(await req.json()).chat_id)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/seek")
async def seek(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        s = _seek(await req.json())
        return await service.seek(s.chat_id, s.delta)
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/enqueue")
async def enqueue(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        return await service.enqueue(_queue(await req.json()))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/queue")
async def queue(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await req.json()
        return await service.queue_list(_coerce_int(_pick(body, "chatId", "chat_id", default=0)))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/clear")
async def clear(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await req.json()
        return await service.queue_clear(_coerce_int(_pick(body, "chatId", "chat_id", default=0)))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/skip")
async def skip(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await req.json()
        return await service.skip(_coerce_int(_pick(body, "chatId", "chat_id", default=0)))
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.get("/state/{chat_id}")
async def state(chat_id: int):
    return service.state(chat_id)