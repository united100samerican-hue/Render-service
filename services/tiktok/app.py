from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, Request

from service import service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("tiktok.app")

app = FastAPI(title="TikTok Bridge Service", version="1.1.0")


def _guard(x_keepalive_secret: str | None) -> None:
    expected = (os.getenv("KEEPALIVE_SECRET") or "").strip()
    if expected and (x_keepalive_secret or "").strip() != expected:
        raise PermissionError("unauthorized")


async def _json(req: Request) -> dict[str, Any]:
    try:
        body = await req.json()
        return body if isinstance(body, dict) else {}
    except Exception:
        return {}

@app.get("/health")
@app.get("/healthz")
async def healthz():
    return {"ok": True, "ready": service.ready, "error": service.backend_error}


@app.post("/start")
@app.post("/tiktok/start")
async def start(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await _json(req)
        return await service.start(body)
    except Exception as e:
        logger.exception("start_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/bridge/enable")
@app.post("/tiktok/bridge/enable")
async def bridge_enable(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await _json(req)
        return await service.bridge_enable(body)
    except Exception as e:
        logger.exception("bridge_enable_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/bridge/disable")
@app.post("/tiktok/bridge/disable")
async def bridge_disable(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await _json(req)
        return await service.bridge_disable(body)
    except Exception as e:
        logger.exception("bridge_disable_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/stop")
@app.post("/tiktok/stop")
async def stop(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await _json(req)
        return await service.stop(body)
    except Exception as e:
        logger.exception("stop_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/state")
@app.post("/tiktok/state")
async def state(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await _json(req)
        return await service.state(body)
    except Exception as e:
        logger.exception("state_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/meta")
@app.post("/tiktok/meta")
async def meta(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    try:
        body = await _json(req)
        return await service.meta(body)
    except Exception as e:
        logger.exception("meta_failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}