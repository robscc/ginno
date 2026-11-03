"""出网请求统一 User-Agent: ginno/<version>（_version.ginno_user_agent 单一来源）。

覆盖 settings 页 ping 探测三协议（_verify_config）、模型列表拉取（_list_models）、
classifier（laya/jev 的 _post）、ASR 转写（transcribe_audio）。build_model
三协议的 UA 断言在 tests/unit/test_models.py，此处不重复。全部无网络。
"""

from __future__ import annotations

import pytest

from ginno_runtime import providers as prov_mod

pytestmark = pytest.mark.unit

UA_PREFIX = "ginno/"


def _assert_ua(headers: dict) -> None:
    assert headers["User-Agent"].startswith(UA_PREFIX)


# ---- _verify_config：settings 页「测试连接」ping 探测 ----


def test_verify_anthropic_probe_sends_ginno_ua(monkeypatch):
    import anthropic

    captured: dict = {}

    class _FakeMessages:
        def create(self, **kw):
            captured["create"] = kw

    class _FakeAnthropic:
        def __init__(self, **kw):
            captured["init"] = kw
            self.messages = _FakeMessages()

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    r = prov_mod._verify_config({"protocol": "anthropic", "api_key": "sk-x"})
    assert r["ok"] is True
    _assert_ua(captured["init"]["default_headers"])


def test_verify_openai_compat_probe_sends_ginno_ua(monkeypatch):
    import openai

    captured: dict = {}

    class _FakeModels:
        def list(self):
            return []

    class _FakeOpenAI:
        def __init__(self, **kw):
            captured["init"] = kw
            self.models = _FakeModels()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    r = prov_mod._verify_config(
        {"protocol": "openai-compatible", "api_key": "sk-x",
         "base_url": "https://e.com/v1"}
    )
    assert r["ok"] is True
    _assert_ua(captured["init"]["default_headers"])


def test_verify_openai_responses_probe_sends_ginno_ua(monkeypatch):
    import openai

    captured: dict = {}

    class _FakeResponses:
        def create(self, **kw):
            captured["create"] = kw

    class _FakeOpenAI:
        def __init__(self, **kw):
            captured["init"] = kw
            self.responses = _FakeResponses()

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    r = prov_mod._verify_config(
        {"protocol": "openai-responses", "api_key": "sk-x"}
    )
    assert r["ok"] is True
    _assert_ua(captured["init"]["default_headers"])
    # 探测本体：最小生成，input 就是那个 ping
    assert captured["create"]["input"] == "ping"


# ---- _list_models：「从 API 拉取模型」 ----


def test_list_models_sends_ginno_ua(monkeypatch):
    import httpx

    captured: dict = {}

    class _Resp:
        status_code = 200

        def json(self):
            return {"data": [{"id": "m-1"}]}

    def fake_get(url, headers=None, timeout=None):
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", fake_get)
    r = prov_mod._list_models(
        {"protocol": "openai-compatible", "api_key": "sk-x",
         "base_url": "https://e.com/v1"}
    )
    assert r["ok"] is True
    _assert_ua(captured["headers"])
    # 鉴权头不受影响
    assert captured["headers"]["Authorization"] == "Bearer sk-x"


# ---- classifier：laya / jev 的 _post ----


async def test_classifier_post_sends_ginno_ua(monkeypatch):
    import httpx

    from ginno_runtime.classifier.jev import JevDecisionsModel
    from ginno_runtime.classifier.laya import LayaSystemOneModel

    captured: list[dict] = []

    class _Resp:
        status_code = 200

        def json(self):
            return {}

    class _FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            captured.append(headers)
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    for cls in (LayaSystemOneModel, JevDecisionsModel):
        m = object.__new__(cls)
        m._api_key = "k"
        m._url = "https://e.com/api"
        m._timeout_s = 5.0
        await m._post({})
    assert len(captured) == 2
    for h in captured:
        _assert_ua(h)
        assert h["Authorization"] == "Bearer k"


# ---- ASR：语音转写 ----


async def test_asr_transcribe_sends_ginno_ua():
    from ginno_runtime.asr import transcribe_audio

    captured: dict = {}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"message": {"content": " hi "}}]}

    class _FakeHttp:
        async def post(self, url, headers=None, json=None):
            captured["headers"] = headers
            return _Resp()

    text = await transcribe_audio(
        b"x", fmt="wav", base_url="https://e.com/v1",
        api_key="k", model="m", http=_FakeHttp(),
    )
    assert text == "hi"
    _assert_ua(captured["headers"])
    assert captured["headers"]["Authorization"] == "Bearer k"
