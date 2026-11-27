"""DashScope duplex ASR for explicit microphone dictation."""

import asyncio
import base64
import re
import time
from urllib.parse import urlsplit, urlunsplit

from websockets.asyncio.client import connect

from . import jsonx as json
from ._version import ginno_user_agent

MODEL = "qwen3-asr-flash-realtime"
_TRANSCRIPTION = "conversation.item.input_audio_transcription."
_HOST = re.compile(r"[a-z0-9-]+\.(cn-beijing|ap-southeast-1|ap-northeast-1)\.maas\.aliyuncs\.com")


def endpoint(config):
    if (config.get("kind") != "asr" or MODEL not in config.get("models", [])
            or not (config.get("api_key") or "").strip()):
        return None
    try:
        url = urlsplit(config.get("base_url") or "")
        if url.scheme != "https" or not url.hostname or url.username is not None or url.password is not None:
            return None
        _ = url.port  # Reject malformed ports before opening a socket.
        region = _HOST.fullmatch(url.hostname)
        return urlunsplit(("wss", url.netloc, "/api-ws/v1/realtime", f"model={MODEL}", "")), region[1] if region else ""
    except ValueError:
        return None


def unavailable_reason(config):
    if not config.get("enabled"):
        return "disabled"
    if MODEL not in config.get("models", []):
        return "unsupported_model"
    if not (config.get("api_key") or "").strip():
        return "missing_key"
    return None if endpoint(config) else "service_url"


class _GatewayConnection(connect):
    def process_redirect(self, exc):
        # Keep gateway credentials and microphone audio on the selected host.
        return exc


def open_provider(config):
    url, _ = endpoint(config)
    return _GatewayConnection(url, additional_headers={
        "Authorization": f"Bearer {config['api_key']}",
        "User-Agent": ginno_user_agent(), "OpenAI-Beta": "realtime=v1",
    }, open_timeout=5, close_timeout=1, max_size=256 * 1024)


class Task:
    def __init__(self, socket):
        self.socket = socket
        self.items = {}

    async def command(self, event, **body):
        await self.socket.send(json.dumps({"type": event, **body}))

    async def receive(self):
        event = json.loads(await self.socket.recv())
        if event["type"] in ("error", _TRANSCRIPTION + "failed"):
            raise RuntimeError("Realtime ASR rejected the request")
        return event

    async def start(self):
        await self.command("session.update", session={
            "modalities": ["text"], "input_audio_format": "pcm", "sample_rate": 16000,
            "turn_detection": {"type": "server_vad", "threshold": 0.2, "silence_duration_ms": 400},
        })
        while (await self.receive())["type"] != "session.updated":
            pass

    async def audio(self, chunk):
        await self.command("input_audio_buffer.append", audio=base64.b64encode(chunk).decode("ascii"))

    async def finish(self):
        await self.command("session.finish")

    async def results(self):
        while True:
            event = await self.receive()
            kind = event["type"]
            if kind == "session.finished":
                return
            if kind == "input_audio_buffer.committed":
                self.items.setdefault(event["item_id"], len(self.items))
            if kind == _TRANSCRIPTION + "text":
                text = (event.get("text") or "") + (event.get("stash") or "")
                state = "partial"
            elif kind == _TRANSCRIPTION + "completed":
                text = event.get("transcript")
                state = "final"
            else:
                continue
            if text:
                yield {"type": state, "id": self.items.setdefault(event["item_id"], len(self.items)), "text": text}


async def verify(config):
    started = time.monotonic()
    try:
        async with open_provider(config) as socket:
            await asyncio.wait_for(Task(socket).start(), 5)
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception:
        from .lang import t

        return {"ok": False, "error": t(
            "Could not start realtime ASR. Check the service URL, API key and model access.",
            "无法启动实时语音识别，请检查服务地址、API Key 和模型调用权限。",
        )}
