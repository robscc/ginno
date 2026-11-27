from contextlib import asynccontextmanager

import asyncio
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import settings_store, voice_asr

pytestmark = pytest.mark.unit


def test_realtime_asr_settings_and_stream(client, monkeypatch):
    config = {
        "id": "asr", "name": "Voice", "kind": "asr", "enabled": True,
        "base_url": "https://test-workspace.cn-beijing.maas.aliyuncs.com",
        "api_key": "test-secret", "models": [voice_asr.MODEL],
    }
    settings_store.update(lambda _: {"model_configs": [config, {
        **config, "id": "file", "models": ["qwen3-asr-flash"],
    }]})
    frames = []

    class Provider:
        def __init__(self):
            self.events = asyncio.Queue()

        async def send(self, data):
            frames.append(data)
            if isinstance(data, bytes):
                await self.event("result-generated", {"output": {"sentence": {
                    "sentence_id": 0, "text": "临时", "sentence_end": False,
                }}})
                return
            command = json.loads(data)
            self.id = command["header"]["task_id"]
            if command["header"]["action"] == "run-task":
                await self.event("task-started", {})
            else:
                await self.event("result-generated", {"output": {"sentence": {
                    "sentence_id": 0, "text": "最终文字", "sentence_end": True,
                }}})
                await self.event("task-finished", {})

        async def event(self, event, payload):
            await self.events.put(json.dumps({"header": {"event": event, "task_id": self.id}, "payload": payload}))

        async def recv(self):
            return await self.events.get()

    @asynccontextmanager
    async def open_provider(cfg):
        assert cfg["id"] == "asr"
        yield Provider()

    monkeypatch.setattr(voice_asr, "open_provider", open_provider)
    response = client.get("/api/voice/settings")
    assert "test-secret" not in response.text
    assert [model["id"] for model in response.json()["models"]] == ["asr"]
    assert client.put("/api/voice/settings", json={"selected": "file", "shortcut": "fn"}).status_code == 400
    assert client.put("/api/voice/settings", json={"selected": "asr", "shortcut": "fn"}).status_code == 200
    with client.websocket_connect("/api/voice/dictation/ws") as ws:
        ws.send_json({"type": "start"})
        assert ws.receive_json() == {"type": "ready"}
        ws.send_bytes(b"\x00" * 3200)
        assert ws.receive_json() == {"type": "partial", "id": 0, "text": "临时"}
        ws.send_text("stop")
        assert ws.receive_json() == {"type": "final", "id": 0, "text": "最终文字"}
        assert ws.receive_json() == {"type": "done"}
    assert json.loads(frames[0])["payload"]["parameters"]["intermediate_result_enabled"] is True
    assert json.loads(frames[2])["header"]["action"] == "finish-task"
