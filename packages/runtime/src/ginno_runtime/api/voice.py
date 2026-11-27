"""Local settings and a single active dictation stream per runtime."""

import asyncio
import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from pydantic import BaseModel

from .. import providers, settings_store, voice_asr
from ..session_meta import _find_meta

router = APIRouter(prefix="/api/voice")
log = logging.getLogger(__name__)
_active = False
_MAX_BYTES = 60 * 32000


def _available(settings):
    return {cfg["id"]: cfg for cfg in providers.load_configs(settings)
            if cfg.get("enabled") and voice_asr.endpoint(cfg)}


@router.get("/settings")
def get_voice_settings():
    settings = settings_store.load_doc().data
    models = []
    unavailable = []
    for cfg in providers.load_configs(settings):
        if cfg.get("kind") != "asr":
            continue
        card = {"id": cfg["id"], "name": cfg.get("name") or cfg["id"]}
        reason = voice_asr.unavailable_reason(cfg)
        if reason:
            unavailable.append({**card, "models": cfg.get("models", []), "reason": reason})
        else:
            _, region = voice_asr.endpoint(cfg)
            models.append({**card, "model": voice_asr.MODEL, "region": region})
    return {
        "selected": settings.get("voice_asr_config_id", ""),
        "shortcut": settings.get("voice_shortcut", "fn"),
        "models": models,
        "unavailable": unavailable,
    }


class VoiceSettings(BaseModel):
    selected: str
    shortcut: str


@router.put("/settings")
def put_voice_settings(body: VoiceSettings):
    def update(settings):
        if body.shortcut not in {"fn", "ctrl-alt-v", "off"} or (
            body.selected and body.selected not in _available(settings)
        ):
            raise HTTPException(400, "Invalid voice settings")
        return {**settings, "voice_asr_config_id": body.selected, "voice_shortcut": body.shortcut}

    settings_store.update(update)
    return {"ok": True}


async def _audio(ws, task, stopping):
    total = 0
    async with asyncio.timeout(65):
        while True:
            packet = await ws.receive()
            if packet["type"] == "websocket.disconnect":
                return False
            if packet.get("text") == "stop":
                stopping.set()
                log.info("voice_capture_finished pcm_bytes=%d", total)
                await task.finish()
                return True
            if packet.get("text") == "cancel":
                return False
            chunk = packet.get("bytes")
            if not chunk or len(chunk) > 3200 or len(chunk) % 2:
                raise ValueError("Invalid PCM frame")
            if total == 0:
                log.info("voice_capture_first_frame")
            total += len(chunk)
            if total > _MAX_BYTES:
                raise ValueError("Audio exceeds maximum duration")
            await asyncio.wait_for(task.audio(chunk), 2)


async def _results(ws, task):
    async for result in task.results():
        await ws.send_json(result)


async def _relay(ws, task):
    stopping = asyncio.Event()
    audio = asyncio.create_task(_audio(ws, task, stopping))
    results = asyncio.create_task(_results(ws, task))
    try:
        done, _ = await asyncio.wait((audio, results), return_when=asyncio.FIRST_COMPLETED)
        if results in done:
            await results
            if not stopping.is_set() and not audio.done():
                raise RuntimeError("ASR ended before stop")
        if await audio:
            await asyncio.wait_for(results, 5)
            await ws.send_json({"type": "done"})
    finally:
        for future in (audio, results):
            future.cancel()
        await asyncio.gather(audio, results, return_exceptions=True)


@router.websocket("/dictation/ws")
async def dictation(ws: WebSocket):
    global _active
    await ws.accept()
    acquired = False
    stage = "request"
    try:
        request = await asyncio.wait_for(ws.receive_json(), 5)
        if not isinstance(request, dict):
            raise ValueError("Invalid dictation request")
        sid = request.get("session_id")
        meta = (_find_meta(sid) or ({}, None))[0] if isinstance(sid, str) and sid else None
        settings = settings_store.load_doc().data
        config = _available(settings).get(settings.get("voice_asr_config_id"))
        if request.get("type") != "start" or (sid and (not meta or meta.get("type") == "channel")):
            await ws.send_json({"type": "error", "code": "unavailable"})
        elif not config:
            await ws.send_json({"type": "error", "code": "configuration"})
        elif _active:
            await ws.send_json({"type": "error", "code": "busy"})
        else:
            _active = acquired = True
            stage = "handshake"
            async with voice_asr.open_provider(config) as socket:
                stage = "initialize"
                task = voice_asr.Task(socket)
                await asyncio.wait_for(task.start(), 5)
                await ws.send_json({"type": "ready"})
                log.info("voice_gateway_ready")
                stage = "stream"
                await _relay(ws, task)
    except WebSocketDisconnect:
        return
    except Exception as exc:
        log.warning("voice_dictation_failed stage=%s error_type=%s", stage, type(exc).__name__)
        try:
            await ws.send_json({"type": "error", "code": "connection"})
        except (RuntimeError, OSError):
            return
    finally:
        if acquired:
            _active = False
        if ws.client_state != WebSocketState.DISCONNECTED:
            try:
                await ws.close()
            except (RuntimeError, OSError):
                pass
