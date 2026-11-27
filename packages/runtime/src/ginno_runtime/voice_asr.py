"""DashScope duplex ASR for explicit microphone dictation."""

import asyncio
import re
import time
from urllib.parse import urlsplit
from uuid import uuid4

from websockets.asyncio.client import connect

from . import jsonx as json
from ._version import ginno_user_agent

MODEL = "qwen-audio-3.1-asr-flash-message"
_HOST = re.compile(r"[a-z0-9-]+\.(cn-beijing|ap-southeast-1|ap-northeast-1)\.maas\.aliyuncs\.com")


def endpoint(config):
    if config.get("kind") != "asr" or MODEL not in config.get("models", []) or not config.get("api_key"):
        return None
    try:
        url = urlsplit(config.get("base_url") or "")
        host = url.hostname or ""
        region = _HOST.fullmatch(host)
        if url.scheme == "https" and url.netloc == host and region:
            return f"wss://{host}/api-ws/v1/inference", region[1]
    except ValueError:
        pass
    return None


class _WorkspaceConnection(connect):
    def process_redirect(self, exc):
        # Keep workspace credentials and microphone audio on the selected host.
        return exc


def open_provider(config):
    return _WorkspaceConnection(endpoint(config)[0], additional_headers={
        "Authorization": f"Bearer {config['api_key']}", "User-Agent": ginno_user_agent(),
    }, open_timeout=5, close_timeout=1, max_size=256 * 1024)


class Task:
    def __init__(self, socket):
        self.socket = socket
        self.id = uuid4().hex

    async def command(self, action, payload):
        await self.socket.send(json.dumps({"header": {
            "action": action, "task_id": self.id, "streaming": "duplex",
        }, "payload": payload}))

    async def receive(self):
        while True:
            data = json.loads(await self.socket.recv())
            if data["header"]["task_id"] == self.id:
                return data

    async def start(self):
        await self.command("run-task", {
            "task_group": "audio", "task": "asr", "function": "recognition", "model": MODEL,
            "parameters": {
                "format": "pcm", "sample_rate": 16000,
                "intermediate_result_enabled": True, "disfluency_removal_enabled": False,
            },
            "input": {},
        })
        if (await self.receive())["header"]["event"] != "task-started":
            raise RuntimeError("ASR task did not start")

    async def finish(self):
        await self.command("finish-task", {"input": {}})


async def verify(config):
    started = time.monotonic()
    try:
        async with open_provider(config) as socket:
            await asyncio.wait_for(Task(socket).start(), 5)
        return {"ok": True, "latency_ms": round((time.monotonic() - started) * 1000)}
    except Exception:
        from .lang import t

        return {"ok": False, "error": t(
            "Could not start realtime ASR. Check the workspace URL, API key and model access.",
            "无法启动实时语音识别，请检查工作空间地址、API Key 和模型调用权限。",
        )}
