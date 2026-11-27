from contextlib import asynccontextmanager

import asyncio
import base64
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import settings_store, voice_asr

pytestmark = pytest.mark.unit


def test_realtime_asr_settings_and_stream(client, monkeypatch):
    config = {
        "id": "asr", "name": "Voice", "kind": "asr", "enabled": True,
        "base_url": "https://gateway.example.com/compatible-mode/v1",
        "api_key": "test-secret", "models": ["qwen-audio-3.0-asr-flash", voice_asr.MODEL],
        "default_model": "qwen-audio-3.0-asr-flash",
    }
    settings_store.update(lambda _: {"model_configs": [config, {
        **config, "id": "file", "models": ["qwen-audio-3.0-asr-flash"],
        "base_url": "https://gateway.example.com/v1", "default_model": "qwen-audio-3.0-asr-flash",
    }, {**config, "id": "old", "models": ["qwen-audio-3.1-asr-flash-message"],
        "default_model": "qwen-audio-3.1-asr-flash-message"}]})
    frames = []

    class Provider:
        def __init__(self):
            self.events = asyncio.Queue()

        async def send(self, data):
            frames.append(data)
            command = json.loads(data)
            if command["type"] == "session.update":
                await self.event("session.created")
                await self.event("session.updated")
            elif command["type"] == "input_audio_buffer.append":
                await self.event("input_audio_buffer.committed", item_id="speech-item")
                await self.event("conversation.item.input_audio_transcription.text",
                                 item_id="speech-item", text="临", stash="时")
            else:
                assert command["type"] == "session.finish"
                await self.event("conversation.item.input_audio_transcription.completed",
                                 item_id="speech-item", transcript="最终文字")
                await self.event("session.finished")
                await asyncio.sleep(0.01)  # Provider can finish before the send yields back.

        async def event(self, event, **fields):
            await self.events.put(json.dumps({"type": event, **fields}))

        async def recv(self):
            return await self.events.get()

    @asynccontextmanager
    async def open_provider(cfg):
        assert cfg["id"] == "asr"
        assert voice_asr.endpoint(cfg) == (
            "wss://gateway.example.com/api-ws/v1/realtime?model=qwen3-asr-flash-realtime", "",
        )
        yield Provider()

    monkeypatch.setattr(voice_asr, "open_provider", open_provider)
    response = client.get("/api/voice/settings")
    assert "test-secret" not in response.text
    assert "gateway.example.com" not in response.text
    assert response.json()["unavailable"] == [{
        "id": "file", "name": "Voice", "models": ["qwen-audio-3.0-asr-flash"],
        "reason": "unsupported_model",
    }, {
        "id": "old", "name": "Voice", "models": ["qwen-audio-3.1-asr-flash-message"],
        "reason": "unsupported_model",
    }]
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
    assert json.loads(frames[0])["session"]["turn_detection"]["type"] == "server_vad"
    assert base64.b64decode(json.loads(frames[1])["audio"]) == b"\x00" * 3200
    assert json.loads(frames[2]) == {"type": "session.finish"}


def test_voice_settings_explains_excluded_asr_configs(client):
    base = {
        "name": "Voice", "kind": "asr", "enabled": True,
        "base_url": "https://test-workspace.cn-beijing.maas.aliyuncs.com",
        "api_key": "test-secret", "models": [voice_asr.MODEL],
    }
    settings_store.update(lambda _: {"model_configs": [
        {**base, "id": "file", "models": ["qwen-audio-3.0-asr-flash"]},
        {**base, "id": "url", "base_url": "http://gateway.example/v1"},
        {**base, "id": "disabled", "enabled": False},
        {**base, "id": "no-key", "api_key": ""},
    ]})
    response = client.get("/api/voice/settings")
    assert response.status_code == 200
    assert response.json()["models"] == []
    assert {c["id"]: c["reason"] for c in response.json()["unavailable"]} == {
        "file": "unsupported_model", "url": "service_url", "disabled": "disabled", "no-key": "missing_key",
    }
    assert "test-secret" not in response.text
    assert "gateway.example" not in response.text
