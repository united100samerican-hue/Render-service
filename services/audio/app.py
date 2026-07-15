
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

ALLOWED_SOURCE_TYPES = {
    "file_id",
    "telegram_file_id",
    "telegram_audio",
    "telegram_video",
    "telegram",
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


def _pick(body: dict[str, Any], *keys: str, default: Any = None) -> Any:
    for k in keys:
        if k in body and body[k] is not None:
            return body[k]
    return default


def _chat_id(body: dict[str, Any]) -> int:
    v = _pick(body, "chatId", "chat_id", default=0)
    try:
        return int(v)
    except Exception:
        return 0


def _source_type(body: dict[str, Any]) -> str:
    st = str(_pick(body, "sourceType", "source_type", default="telegram_file_id")).strip().lower()
    if st not in ALLOWED_SOURCE_TYPES:
        raise HTTPException(status_code=400, detail="unsupported_source_type")
    return st


def _source_id(body: dict[str, Any]) -> str:
    return str(_pick(body, "sourceId", "source_id", default="")).strip()


def _title(body: dict[str, Any]) -> str:
    return str(_pick(body, "title", default="")).strip()


def _int(body: dict[str, Any], *keys: str, default: int = 0) -> int:
    v = _pick(body, *keys, default=default)
    try:
        return int(v)
    except Exception:
        return default


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
    return {"ok": True, "service": "render-audio-service"}


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "render-audio-service",
        "ready": service.ready,
        "backend_error": service.backend_error,
    }


@app.get("/state/{chat_id}")
async def state(chat_id: int) -> dict[str, Any]:
    return service._state(int(chat_id))


@app.post("/meta")
async def meta(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.meta(chat_id, _source_type(body), _source_id(body), title=_title(body), duration=_int(body, "duration", default=0))
    except Exception as e:
        logger.exception("meta failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


@app.post("/start")
async def start(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.start(chat_id, _source_type(body), _source_id(body), title=_title(body), duration=_int(body, "duration", default=0), offset=_int(body, "offset", default=0))
    except Exception as e:
        logger.exception("start failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


@app.post("/pause")
async def pause(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.pause(chat_id)
    except Exception as e:
        logger.exception("pause failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


@app.post("/resume")
async def resume(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.resume(chat_id)
    except Exception as e:
        logger.exception("resume failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


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
        return JSONResponse(status_code=502, content={"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)})


@app.post("/seek")
async def seek(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.seek(chat_id, _int(body, "delta", default=0))
    except Exception as e:
        logger.exception("seek failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


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
            auto_start=bool(_pick(body, "autoStart", "auto_start", default=True)),
        )
    except Exception as e:
        logger.exception("enqueue failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


@app.post("/queue")
async def queue(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    return await queue_list(req, x_keepalive_secret)


@app.post("/queue/list")
async def queue_list(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.queue_list(chat_id)
    except Exception as e:
        logger.exception("queue_list failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


@app.post("/queue/clear")
async def queue_clear(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.queue_clear(chat_id)
    except Exception as e:
        logger.exception("queue_clear failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


@app.post("/queue/skip")
async def queue_skip(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    _guard(x_keepalive_secret)
    body = await _json(req)
    chat_id = _chat_id(body)
    try:
        return await service.skip(chat_id)
    except Exception as e:
        logger.exception("queue_skip failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "state": service._state(chat_id)}


@app.post("/queue/next")
async def queue_next(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")):
    return await queue_skip(req, x_keepalive_secret)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)