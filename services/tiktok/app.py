from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, Request

from service import TikTokService, StartRequest, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tiktok_app")

app = FastAPI(title="TikTok Live Service", version="1.0")
KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()


def _guard(secret: str | None) -> None:
    if KEEPALIVE_SECRET and (secret or "").strip() != KEEPALIVE_SECRET:
        raise PermissionError("forbidden")


def _pick(body: dict[str, Any], *names: str, default: Any = None) -> Any:
    for n in names:
        if n in body and body[n] is not None:
            return body[n]
    return default


def _coerce_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except Exception:
        return default


def _make_start(body: dict[str, Any]) -> StartRequest:
    return StartRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        tiktok_url=str(_pick(body, "source_url", "sourceUrl", "tiktok_url", "url", default="")),
        video=bool(_pick(body, "video", default=True)),
    )


@app.on_event("startup")
async def _startup() -> None:
    await service.boot()
    logger.info("startup_done", extra={"ready": service.ready, "backend_error": service.backend_error})


@app.get("/")
async def root():
    return {"ok": True, "service": "tiktok"}


@app.get("/ping")
async def ping():
    return {"ok": True, "service": "tiktok", "ping": True}


@app.get("/health")
async def health():
    return {
        "ok": True,
        "ready": service.ready,
        "backend_error": service.backend_error,
        "sessions": service.sessions_count(),
    }


@app.post("/tiktok/start")
async def tiktok_start(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await req.json()
    try:
        return await service.start(_make_start(body))
    except Exception as e:
        logger.exception("tiktok_start_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/tiktok/stop")
async def tiktok_stop(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await req.json()
    chat_id = _coerce_int(_pick(body, "chatId", "chat_id", default=0))
    try:
        return await service.stop(chat_id)
    except Exception as e:
        logger.exception("tiktok_stop_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/tiktok/state")
async def tiktok_state(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await req.json()
    chat_id = _coerce_int(_pick(body, "chatId", "chat_id", default=0))
    return {"ok": True, "state": await service.state(chat_id)}