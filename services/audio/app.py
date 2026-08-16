from __future__ import annotations

import logging
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from service import service

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("audio_app")

app = FastAPI(title="Render Audio Service", version="6.0")
KEEPALIVE_SECRET = os.getenv("KEEPALIVE_SECRET", "").strip()


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
    raw = str(_pick(body, "sourceType", "source_type", default="")).strip().lower().replace("-", "_")
    aliases = {
        "telegram": "telegram_file_id",
        "tg": "telegram_file_id",
        "telegram_file": "telegram_file_id",
        "telegram_file_id": "telegram_file_id",
        "telegram_media": "telegram_file_id",
        "telegram_document": "telegram_file_id",
        "file": "telegram_file_id",
        "document": "telegram_file_id",
        "media": "telegram_file_id",
        "audio": "telegram_audio",
        "voice": "telegram_audio",
        "song": "telegram_audio",
        "music": "telegram_audio",
        "telegram_audio": "telegram_audio",
        "video": "telegram_video",
        "clip": "telegram_video",
        "movie": "telegram_video",
        "telegram_video": "telegram_video",
        "telegram_message": "telegram_message",
    }
    normalized = aliases.get(raw, raw or "telegram_file_id")
    if normalized not in {"telegram_file_id", "telegram_audio", "telegram_video", "telegram_message"}:
        raise HTTPException(status_code=400, detail="unsupported_source_type")
    return normalized


def _source_chat_id(body: dict[str, Any]) -> int:
    try:
        return int(_pick(body, "sourceChatId", "source_chat_id", default=0))
    except Exception:
        return 0


def _source_message_id(body: dict[str, Any]) -> int:
    try:
        return int(_pick(body, "sourceMessageId", "source_message_id", default=0))
    except Exception:
        return 0


def _title(body: dict[str, Any]) -> str:
    return str(_pick(body, "title", default="")).strip()


def _duration(body: dict[str, Any]) -> int:
    try:
        return max(0, int(_pick(body, "duration", default=0) or 0))
    except Exception:
        return 0


async def _track_call(req: Request, x_keepalive_secret: str | None) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    body = await _json(req)
    return {
        "chat_id": _chat_id(body),
        "source_type": _source_type(body),
        "source_id": _source_id(body),
        "title": _title(body),
        "duration": _duration(body),
        "offset": max(0, int(_pick(body, "offset", default=0) or 0)),
        "source_chat_id": _source_chat_id(body),
        "source_message_id": _source_message_id(body),
    }


@app.on_event("startup")
async def _startup() -> None:
    try:
        await service.ensure_ready()
        logger.info("startup_done ready=%s error=%s", service.ready, service.backend_error)
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
    return {"ok": True, "service": "render-audio-service", "ready": service.ready, "version": "6.0"}


@app.get("/ping", response_class=PlainTextResponse)
async def ping() -> str:
    return "OK"


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "ready": service.ready,
        "backend_error": service.backend_error,
        "active_sessions": service.active_sessions_count(),
        "queue_mode": "worker_d1",
    }


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True, "ready": service.ready}


@app.get("/state/{chat_id}")
async def state(chat_id: int, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    return service.state(chat_id)


@app.post("/meta")
async def meta(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    try:
        data = await _track_call(req, x_keepalive_secret)
        return await service.meta(
            data["chat_id"],
            data["source_type"],
            data["source_id"],
            title=data["title"],
            duration=data["duration"],
            source_chat_id=data["source_chat_id"],
            source_message_id=data["source_message_id"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("meta_failed")
        return {"ok": False, "action": "meta", "error": f"{type(exc).__name__}: {exc}"}


@app.post("/start")
async def start(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    try:
        data = await _track_call(req, x_keepalive_secret)
        return await service.start(
            data["chat_id"],
            data["source_type"],
            data["source_id"],
            title=data["title"],
            duration=data["duration"],
            offset=data["offset"],
            source_chat_id=data["source_chat_id"],
            source_message_id=data["source_message_id"],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("start_failed")
        return JSONResponse(status_code=502, content={"ok": False, "action": "start", "error": f"{type(exc).__name__}: {exc}"})


async def _next_impl(req: Request, x_keepalive_secret: str | None, action: str) -> dict[str, Any]:
    data = await _track_call(req, x_keepalive_secret)
    fn = service.next if action == "next" else service.skip
    return await fn(
        data["chat_id"],
        data["source_type"],
        data["source_id"],
        title=data["title"],
        duration=data["duration"],
        offset=data["offset"],
        source_chat_id=data["source_chat_id"],
        source_message_id=data["source_message_id"],
    )


@app.post("/next")
async def next_track(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    try:
        return await _next_impl(req, x_keepalive_secret, "next")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("next_failed")
        return JSONResponse(status_code=502, content={"ok": False, "action": "next", "error": f"{type(exc).__name__}: {exc}"})


@app.post("/skip")
async def skip(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    try:
        return await _next_impl(req, x_keepalive_secret, "skip")
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("skip_failed")
        return JSONResponse(status_code=502, content={"ok": False, "action": "skip", "error": f"{type(exc).__name__}: {exc}"})


@app.post("/pause")
async def pause(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        return await service.pause(_chat_id(body))
    except Exception as exc:
        logger.exception("pause_failed")
        return {"ok": False, "action": "pause", "error": f"{type(exc).__name__}: {exc}"}


@app.post("/resume")
async def resume(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        return await service.resume(_chat_id(body))
    except Exception as exc:
        logger.exception("resume_failed")
        return {"ok": False, "action": "resume", "error": f"{type(exc).__name__}: {exc}"}


@app.post("/stop")
async def stop(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        result = await service.stop(_chat_id(body))
        if not result.get("ok", False):
            return JSONResponse(status_code=502, content=result)
        return result
    except Exception as exc:
        logger.exception("stop_failed")
        return JSONResponse(status_code=502, content={"ok": False, "action": "stop", "error": f"{type(exc).__name__}: {exc}"})


@app.post("/seek")
async def seek(req: Request, x_keepalive_secret: str | None = Header(default=None, alias="x-keepalive-secret")) -> dict[str, Any]:
    _guard(x_keepalive_secret)
    body = await _json(req)
    try:
        delta=int(_pick(body,"delta",default=0) or 0)
    except Exception:
        delta=0
    try:
        raw_position=_pick(body,"position",default=None)
        position=None if raw_position is None else max(0,int(raw_position or 0))
    except Exception:
        position=None
    try:
        return await service.seek(_chat_id(body),delta,position=position)
    except Exception as exc:
        logger.exception("seek_failed")
        return {"ok":False,"action":"seek","error":f"{type(exc).__name__}: {exc}"}


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "10000")), reload=False)