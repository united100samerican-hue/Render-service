from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import PlainTextResponse

from service import StartRequest, service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tiktok_app")

app = FastAPI(title="TikTok Live Service", version="3.0")
KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()


def _guard(secret: str | None) -> None:
    if KEEPALIVE_SECRET and (secret or "").strip() != KEEPALIVE_SECRET:
        raise HTTPException(status_code=403, detail="forbidden")


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


def _start_from_body(body: dict[str, Any]) -> StartRequest:
    return StartRequest(
        chat_id=_coerce_int(_pick(body, "chatId", "chat_id", default=0)),
        source_url=str(_pick(body, "source_url", "sourceUrl", "tiktok_url", "url", default="")),
        title=str(_pick(body, "title", default="")),
        video=bool(_pick(body, "video", default=True)),
        mode=str(_pick(body, "mode", default="live")),
    )


def _chat_id_from_body(body: dict[str, Any]) -> int:
    return _coerce_int(_pick(body, "chatId", "chat_id", default=0))


@app.on_event("startup")
async def _startup() -> None:
    try:
        await service.boot()
        logger.info("startup_done", extra={"ready": service.ready, "backend_error": service.backend_error})
    except Exception:
        logger.exception("startup_failed")


@app.get("/")
async def root():
    return {"ok": True, "service": "tiktok"}


@app.get("/ping", response_class=PlainTextResponse)
async def ping():
    return "OK"


@app.get("/health")
async def health():
    return {
        "ok": True,
        "ready": service.ready,
        "backend_error": service.backend_error,
        "sessions": service.sessions_count(),
    }


@app.get("/healthz")
async def healthz():
    return {"ok": True, "ready": service.ready}


@app.post("/start")
@app.post("/tiktok/start")
async def start(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await req.json()
        return await service.start(_start_from_body(body))
    except Exception as e:
        logger.exception("tiktok_start_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/stop")
@app.post("/tiktok/stop")
async def stop(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await req.json()
        return await service.stop(_chat_id_from_body(body))
    except Exception as e:
        logger.exception("tiktok_stop_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/state")
@app.post("/tiktok/state")
async def state(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await req.json()
    chat_id = _chat_id_from_body(body)
    try:
        return {"ok": True, "state": await service.state(chat_id)}
    except Exception as e:
        logger.exception("tiktok_state_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/meta")
async def meta(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await req.json()
    try:
        return {"ok": True, "state": await service.meta(_start_from_body(body))}
    except Exception as e:
        logger.exception("tiktok_meta_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}