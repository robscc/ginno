"""Web-search passthrough: ``enable_search`` → request body ``extra_body``.

No network: we monkeypatch ``load_providers`` and only *construct* the chat
model (construction does not call the API), then assert the field langchain
forwards verbatim into the request body. The actual search behaviour depends on
the gateway and is left for the user-triggered 测试联网 button.
"""

from __future__ import annotations

import pytest

from ginno_runtime import providers as prov_mod
from ginno_runtime.models import build_model

pytestmark = pytest.mark.unit


def _cfg(**over) -> dict:
    base = {
        "enabled": True,
        "protocol": "openai-compatible",
        "name": "t",
        "api_key": "sk-x",
        "base_url": "https://example.com/v1",
        "model": "qwen-plus",
        "max_tokens": 100,
        "temperature": 0.7,
        "timeout_s": 60,
        "enable_search": False,
    }
    base.update(over)
    return base


def _es(model) -> bool:
    return bool((getattr(model, "extra_body", None) or {}).get("enable_search"))


def test_enable_search_override_sets_extra_body(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    assert _es(build_model("custom", enable_search=True)) is True


def test_enable_search_follows_config_when_no_override(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg(enable_search=True)})
    assert _es(build_model("custom")) is True


def test_no_search_by_default(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    assert _es(build_model("custom")) is False


def test_override_false_beats_config(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg(enable_search=True)})
    assert _es(build_model("custom", enable_search=False)) is False


def test_search_probe_unknown_provider(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg()})
    r = prov_mod.search_probe("missing")
    assert r["ok"] is False and "unknown" in r["error"]


def test_search_probe_disabled_provider(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"custom": _cfg(enabled=False)})
    r = prov_mod.search_probe("custom")
    assert r["ok"] is False
