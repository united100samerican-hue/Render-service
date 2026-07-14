from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request

from service import AudioService, ControlRequest, MetaRequest, SeekRequest, StartRequest

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_app")

app = FastAPI(title="Audio Service", version="1.1.0")
service = AudioService()

KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()


def _guard(secret: str | None) -> None:
    if KEEPALIVE_SECRET and (secret or "").strip() != KEEPALIVE_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")


def _to_model(model_cls: Any, data: dict[str, Any]) -> Any:
    fields = getattr(model_cls, "model_fields", None)
    if isinstance(fields, dict) and fields:
        allowed = set(fields.keys())
    else:
        allowed = set(getattr(model_cls, "__annotations__", {}) or {})
    if allowed:
        data = {k: v for k, v in data.items() if k in allowed}
    return model_cls(**data)


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


@app.get("/health")
async def health():
    return {
        "ok": True,
        "ready": service.ready,
        "backend_error": service.backend_error,
        "active_sessions": service.active_sessions_count(),
    }


@app.post("/meta")
async def meta(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    payload = _to_model(
        MetaRequest,
        {
            "chat_id": int(body.get("chatId", body.get("chat_id", 0)) or 0),
            "source_type": str(body.get("source_type", body.get("sourceType", "url"))),
            "source_id": str(body.get("source_id", body.get("sourceId", ""))),
            "title": str(body.get("title", "")),
            "duration": int(body.get("duration", 0) or 0),
        },
    )
    return await service.meta(payload)


@app.post("/start")
async def start(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    payload = _to_model(
        StartRequest,
        {
            "chat_id": int(body.get("chatId", body.get("chat_id", 0)) or 0),
            "source_type": str(body.get("source_type", body.get("sourceType", "url"))),
            "source_id": str(body.get("source_id", body.get("sourceId", ""))),
            "title": str(body.get("title", "")),
            "duration": int(body.get("duration", 0) or 0),
        },
    )
    try:
        return await service.start(payload)
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
    payload = _to_model(
        ControlRequest,
        {"chat_id": int(body.get("chatId", body.get("chat_id", 0)) or 0)},
    )
    return await service.pause(payload.chat_id)


@app.post("/resume")
async def resume(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    payload = _to_model(
        ControlRequest,
        {"chat_id": int(body.get("chatId", body.get("chat_id", 0)) or 0)},
    )
    return await service.resume(payload.chat_id)


@app.post("/stop")
async def stop(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    payload = _to_model(
        ControlRequest,
        {"chat_id": int(body.get("chatId", body.get("chat_id", 0)) or 0)},
    )
    return await service.stop(payload.chat_id)


@app.post("/seek")
async def seek(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await req.json()
    payload = _to_model(
        SeekRequest,
        {
            "chat_id": int(body.get("chatId", body.get("chat_id", 0)) or 0),
            "delta": int(body.get("delta", 0) or 0),
        },
    )
    return await service.seek(payload.chat_id, payload.delta)
