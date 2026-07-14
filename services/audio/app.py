from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse

from service import ControlRequest, MetaRequest, SeekRequest, StartRequest, service

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("audio_app")

app = FastAPI(title="Render Audio Service", version="3.0")
KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()


def _guard(secret: str | None) -> None:
    if KEEPALIVE_SECRET and (secret or "").strip() != KEEPALIVE_SECRET:
        raise RuntimeError("forbidden")


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


async def _json(req: Request) -> dict[str, Any]:
    try:
        body = await req.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}


def _chat_id(body: dict[str, Any]) -> int:
    return _coerce_int(body.get("chatId", body.get("chat_id", 0)))


def _source_type(body: dict[str, Any]) -> str:
    return str(body.get("source_type", body.get("sourceType", "url")) or "url")


def _source_id(body: dict[str, Any]) -> str:
    return str(body.get("source_id", body.get("sourceId", "")) or "")


def _title(body: dict[str, Any]) -> str:
    return str(body.get("title", "") or "")


def _duration(body: dict[str, Any]) -> int:
    return _coerce_int(body.get("duration", 0))


@app.on_event("startup")
async def _startup() -> None:
    await service.ensure_ready()
    logger.info("startup_done")


@app.get("/")
async def root() -> dict[str, Any]:
    return {"ok": True, "service": "render-audio-service", "ready": service.ready}


@app.get("/ping")
async def ping(x_keepalive_secret: str | None = Header(default=None)) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    return {"ok": True}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "ready": True}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "render-audio-service",
        "ready": service.ready,
        "active_sessions": service.active_sessions_count(),
        "backend_error": service.backend_error,
    }


@app.post("/meta")
async def meta(req: Request, x_keepalive_secret: str | None = Header(default=None)):
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        state = await service.meta(
            MetaRequest(
                chat_id=_chat_id(body),
                source_type=_source_type(body),
                source_id=_source_id(body),
                title=_title(body),
                duration=_duration(body),
            )
        )
        return {"ok": True, "action": "meta", "state": state}
    except Exception as e:
        logger.exception("meta failed")
        return {"ok": False, "error": type(e).__name__, "detail": str(e)}


@app.post("/start")
async def start(req: Request, x_keepalive_secret: str | None = Header(default=None)):
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        state = await service.start(
            StartRequest(
                chat_id=_chat_id(body),
                source_type=_source_type(body),
                source_id=_source_id(body),
                title=_title(body),
                duration=_duration(body),
            )
        )
        return {"ok": True, "action": "start", "state": state}
    except Exception as e:
        logger.exception("start failed")
        return {"ok": False, "error": type(e).__name__, "detail": str(e)}


@app.post("/pause")
async def pause(req: Request, x_keepalive_secret: str | None = Header(default=None)):
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        state = await service.pause(_chat_id(body))
        return {"ok": True, "action": "pause", "state": state}
    except Exception as e:
        logger.exception("pause failed")
        return {"ok": False, "error": type(e).__name__, "detail": str(e)}


@app.post("/resume")
async def resume(req: Request, x_keepalive_secret: str | None = Header(default=None)):
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        state = await service.resume(_chat_id(body))
        return {"ok": True, "action": "resume", "state": state}
    except Exception as e:
        logger.exception("resume failed")
        return {"ok": False, "error": type(e).__name__, "detail": str(e)}


@app.post("/stop")
async def stop(req: Request, x_keepalive_secret: str | None = Header(default=None)):
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        state = await service.stop(_chat_id(body))
        return {"ok": True, "action": "stop", "state": state}
    except Exception as e:
        logger.exception("stop failed")
        return JSONResponse(
            status_code=200,
            content={"ok": False, "action": "stop", "error": type(e).__name__, "detail": str(e)},
        )


@app.post("/seek")
async def seek(req: Request, x_keepalive_secret: str | None = Header(default=None)):
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        state = await service.seek(_chat_id(body), _coerce_int(body.get("delta", 0)))
        return {"ok": True, "action": "seek", "state": state}
    except Exception as e:
        logger.exception("seek failed")
        return {"ok": False, "error": type(e).__name__, "detail": str(e)}