"""A2A opt-in 门控（world_state.a2a_enabled + build_a2a_tools 注册闸）。

照 external_agents_enabled 先例：默认 False、GINNO_A2A env 覆盖、
settings.context.a2a_enabled 用户开关；禁用即不注册工具。
"""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths, world_state
from ginno_runtime.a2a.peers import PeerRecord, PeerStore
from ginno_runtime.tools.a2a_tools import build_a2a_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GINNO_A2A", raising=False)


def _write_settings(context: dict) -> None:
    p = paths.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"context": context}), encoding="utf-8")


def test_default_off():
    # isolated_home：无 settings、无 env → 默认关
    assert world_state.a2a_enabled() is False


def test_default_registered_in_context_defaults():
    # 不登记进 _CONTEXT_DEFAULTS 的键用户永远改不动（cap=8 事故同型）
    assert world_state._CONTEXT_DEFAULTS["a2a_enabled"] is False


def test_env_override_on(monkeypatch):
    for value in ("1", "true", "yes", "on", "TRUE"):
        monkeypatch.setenv("GINNO_A2A", value)
        assert world_state.a2a_enabled() is True, value


def test_env_override_off_beats_settings(monkeypatch):
    _write_settings({"a2a_enabled": True})
    monkeypatch.setenv("GINNO_A2A", "0")
    assert world_state.a2a_enabled() is False


def test_settings_enable():
    _write_settings({"a2a_enabled": True})
    assert world_state.a2a_enabled() is True


def test_build_returns_empty_when_disabled(tmp_path):
    store = PeerStore(tmp_path / "peers.json")
    store.add(PeerRecord(slug="expense-bot", endpoint="https://bot.example/a2a"))
    assert build_a2a_tools(store=store) == []


def test_build_registers_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_A2A", "1")
    store = PeerStore(tmp_path / "peers.json")
    store.add(PeerRecord(slug="expense-bot", endpoint="https://bot.example/a2a"))
    tools = build_a2a_tools(store=store)
    assert {t.name for t in tools} == {
        "a2a_expense_bot_delegate", "a2a_expense_bot_task",
    }
