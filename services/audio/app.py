from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from service import (
    AudioService,
    ControlRequest,
    MetaRequest,
    QueueAddRequest,
    QueueClearRequest,
    QueueListRequest,
    SeekRequest,
    StartRequest,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_app")

app = FastAPI(title="Render Audio Service", version="5.0.0")
service = AudioService()
KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()

ALLOWED_SOURCE_TYPES = {
    "file_id",
    "telegram_file_id",
    "telegram",
    "telegram_audio",
    "telegram_video",
}


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


def _body_int(body: dict[str, Any], *keys: str, default: int = 0) -> int:
    for key in keys:
        try:
            if key in body and body[key] is not None and body[key] != "":
                return int(body[key])
        except Exception:
            continue
    return default


def _body_str(body: dict[str, Any], *keys: str, default: str = "") -> str:
    for key in keys:
        val = body.get(key)
        if val is not None:
            s = str(val).strip()
            if s:
                return s
    return default


def _normalize_source_type(body: dict[str, Any], default: str = "telegram_file_id") -> str:
    st = _body_str(body, "source_type", "sourceType", default=default).strip().lower()
    if not st:
        st = default
    if st not in ALLOWED_SOURCE_TYPES:
        raise HTTPException(status_code=400, detail="unsupported_source_type")
    return st


async def _json(req: Request) -> dict[str, Any]:
    try:
        body = await req.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


@app.on_event("startup")
async def _startup() -> None:
    try:
        await service.ensure_ready()
        logger.info("startup_done", extra={"ready": service.ready, "backend_error": service.backend_error})
    except Exception:
        logger.exception("audio startup failed")


@app.on_event("shutdown")
async def _shutdown() -> None:
    try:
        await service.close()
    except Exception:
        logger.exception("audio shutdown failed")


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "render-audio-service", "ready": service.ready}


@app.get("/ping")
async def ping(x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    return {"ok": True, "service": "render-audio-service"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "render-audio-service",
        "ready": service.ready,
        "active_sessions": service.active_sessions_count(),
        "queues": service.queues_count(),
        "backend_error": service.backend_error,
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "ready": True}


@app.get("/state/{chat_id}")
async def state(chat_id: int) -> dict[str, Any]:
    return service.state(chat_id)


@app.post("/meta")
async def meta(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(
        MetaRequest,
        {
            "chat_id": _body_int(body, "chatId", "chat_id"),
            "source_type": _normalize_source_type(body),
            "source_id": _body_str(body, "source_id", "sourceId"),
            "title": _body_str(body, "title"),
            "duration": _body_int(body, "duration"),
        },
    )
    try:
        return await service.meta(payload)
    except Exception as e:
        logger.exception("meta failed")
        return {"ok": False, "action": "meta", "error": type(e).__name__, "detail": str(e)}


@app.post("/start")
async def start(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(
        StartRequest,
        {
            "chat_id": _body_int(body, "chatId", "chat_id"),
            "source_type": _normalize_source_type(body),
            "source_id": _body_str(body, "source_id", "sourceId"),
            "title": _body_str(body, "title"),
            "duration": _body_int(body, "duration"),
            "offset": _body_int(body, "offset"),
        },
    )
    try:
        return await service.start(payload)
    except Exception as e:
        logger.exception("audio start failed", extra={"body": body})
        return {"ok": False, "action": "start", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/pause")
async def pause(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(ControlRequest, {"chat_id": _body_int(body, "chatId", "chat_id")})
    try:
        return await service.pause(payload.chat_id)
    except Exception as e:
        logger.exception("pause failed")
        return {"ok": False, "action": "pause", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/resume")
async def resume(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(ControlRequest, {"chat_id": _body_int(body, "chatId", "chat_id")})
    try:
        return await service.resume(payload.chat_id)
    except Exception as e:
        logger.exception("resume failed")
        return {"ok": False, "action": "resume", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/stop")
async def stop(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(ControlRequest, {"chat_id": _body_int(body, "chatId", "chat_id")})
    try:
        result = await service.stop(payload.chat_id)
        if not result.get("ok", False):
            # Surface failures clearly to the caller so the bot does not report a false success.
            return JSONResponse(status_code=502, content=result)
        return result
    except Exception as e:
        logger.exception("stop failed")
        return JSONResponse(
            status_code=502,
            content={
                "ok": False,
                "action": "stop",
                "error": type(e).__name__,
                "detail": str(e),
                "state": service.state(payload.chat_id),
            },
        )


@app.post("/seek")
async def seek(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(
        SeekRequest,
        {
            "chat_id": _body_int(body, "chatId", "chat_id"),
            "delta": _body_int(body, "delta"),
        },
    )
    try:
        return await service.seek(payload.chat_id, payload.delta)
    except Exception as e:
        logger.exception("seek failed")
        return {"ok": False, "action": "seek", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/queue/add")
async def queue_add(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(
        QueueAddRequest,
        {
            "chat_id": _body_int(body, "chatId", "chat_id"),
            "source_type": _normalize_source_type(body),
            "source_id": _body_str(body, "source_id", "sourceId"),
            "title": _body_str(body, "title"),
            "duration": _body_int(body, "duration"),
            "requested_by": _body_str(body, "requested_by", "requestedBy"),
            "auto_start": bool(body.get("auto_start", body.get("autoStart", True))),
        },
    )
    try:
        return await service.enqueue(payload)
    except Exception as e:
        logger.exception("queue_add failed")
        return {"ok": False, "action": "queue_add", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/queue/list")
async def queue_list(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(QueueListRequest, {"chat_id": _body_int(body, "chatId", "chat_id")})
    try:
        return await service.queue_list(payload.chat_id)
    except Exception as e:
        logger.exception("queue_list failed")
        return {"ok": False, "action": "queue_list", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/queue/clear")
async def queue_clear(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(QueueClearRequest, {"chat_id": _body_int(body, "chatId", "chat_id")})
    try:
        return await service.queue_clear(payload.chat_id)
    except Exception as e:
        logger.exception("queue_clear failed")
        return {"ok": False, "action": "queue_clear", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/queue/skip")
async def queue_skip(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    _guard(x_keepalive_secret)
    body = await _json(req)
    payload = _to_model(ControlRequest, {"chat_id": _body_int(body, "chatId", "chat_id")})
    try:
        return await service.skip(payload.chat_id)
    except Exception as e:
        logger.exception("queue_skip failed")
        return {"ok": False, "action": "skip", "error": type(e).__name__, "detail": str(e), "state": service.state(payload.chat_id)}


@app.post("/queue/next")
async def queue_next(
    req: Request,
    x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret"),
):
    return await queue_skip(req, x_keepalive_secret)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)