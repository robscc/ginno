"""Local settings and a single active dictation stream per runtime."""

import asyncio

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from pydantic import BaseModel

from .. import providers, settings_store, voice_asr
from ..session_meta import _find_meta

router = APIRouter(prefix="/api/voice")
_active = False
_MAX_BYTES = 60 * 32000


def _available(settings):
    return {cfg["id"]: cfg for cfg in providers.load_configs(settings)
            if cfg.get("enabled") and voice_asr.endpoint(cfg)}


@router.get("/settings")
def get_voice_settings():
    settings = settings_store.load_doc().data
    return {
        "selected": settings.get("voice_asr_config_id", ""),
        "shortcut": settings.get("voice_shortcut", "fn"),
        "models": [{"id": cid, "name": cfg.get("name") or cid,
                    "model": voice_asr.MODEL, "region": voice_asr.endpoint(cfg)[1]}
                   for cid, cfg in _available(settings).items()],
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


async def _audio(ws, task):
    total = 0
    async with asyncio.timeout(65):
        while True:
            packet = await ws.receive()
            if packet["type"] == "websocket.disconnect":
                return False
            if packet.get("text") == "stop":
                await task.finish()
                return True
            if packet.get("text") == "cancel":
                return False
            chunk = packet.get("bytes")
            if not chunk or len(chunk) > 3200 or len(chunk) % 2:
                raise ValueError("Invalid PCM frame")
            total += len(chunk)
            if total > _MAX_BYTES:
                raise ValueError("Audio exceeds maximum duration")
            await asyncio.wait_for(task.socket.send(chunk), 2)


async def _results(ws, task):
    while True:
        data = await task.receive()
        event = data["header"]["event"]
        if event == "task-failed":
            raise RuntimeError("ASR task failed")
        if event == "task-finished":
            return
        if event == "result-generated":
            sentence = data["payload"].get("output", {}).get("sentence", {})
            if not sentence.get("heartbeat") and sentence.get("text"):
                await ws.send_json({
                    "type": "final" if sentence.get("sentence_end") else "partial",
                    "id": sentence["sentence_id"], "text": sentence["text"],
                })


async def _relay(ws, task):
    audio = asyncio.create_task(_audio(ws, task))
    results = asyncio.create_task(_results(ws, task))
    try:
        done, _ = await asyncio.wait((audio, results), return_when=asyncio.FIRST_COMPLETED)
        if results in done:
            await results
            if not audio.done():
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
            async with voice_asr.open_provider(config) as socket:
                task = voice_asr.Task(socket)
                await asyncio.wait_for(task.start(), 5)
                await ws.send_json({"type": "ready"})
                await _relay(ws, task)
    except WebSocketDisconnect:
        return
    except Exception:
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
