"""Context breakdown 单测：工具/技能/系统/历史的账本口径（2026-11-13 P0）。"""

from __future__ import annotations

from types import SimpleNamespace

from ginno_runtime import server_shared
from ginno_runtime.context_breakdown import build


class _FakeRegistry:
    def all_langchain_tools(self):
        return [
            SimpleNamespace(
                name="mcp_srv-a_read",
                description="read things",
                args={"type": "object", "properties": {"id": {"type": "string"}}},
            ),
            SimpleNamespace(
                name="mcp_srv-a_write",
                description="write things",
                args={"type": "object", "properties": {"id": {"type": "string"}}},
            ),
        ]

    def status(self):
        return [
            {"name": "srv-a", "connected": True, "enabled": True, "tools": 2, "error": None},
            {"name": "srv-b", "connected": False, "enabled": False, "tools": 0, "error": "boom"},
        ]


def test_breakdown_layers_tool_policy_and_history(monkeypatch):
    monkeypatch.setattr(server_shared, "_mcp", _FakeRegistry(), raising=False)
    monkeypatch.setattr(
        "ginno_runtime.usage.last_chat_input_tokens", lambda _sid: 1000
    )
    monkeypatch.setattr(
        "ginno_runtime.graph_prompts.build_stable_system",
        lambda *a, **k: "stable system prompt",
    )
    from ginno_runtime.world_state import SkillsSection

    monkeypatch.setattr(
        SkillsSection, "snapshot", lambda self, ctx: {"index": "skills", "names": ["a", "b"]}
    )

    session = {
        "project_slug": "default",
        "agent_id": "dev",
        "model_provider": "prov_x",
        "model_name": "model-y",
        "tool_policy": {
            "mode": "all",
            "mcp_tools_disabled": {"srv-a": ["mcp_srv-a_write"]},
        },
    }
    out = build("sid", session)

    assert out["ok"] is True
    assert out["used"] == 1000
    assert out["measured"] is True
    assert out["counter"] in {"tiktoken", "estimate"}
    srv = next(p for p in out["tools"] if p["server"] == "srv-a")
    assert srv["tools"] == 2
    assert srv["enabledTools"] == 1
    assert srv["tokens"] > 0
    assert srv["disabledTokens"] > 0
    assert {t["name"] for t in srv["byTool"]} == {"mcp_srv-a_read", "mcp_srv-a_write"}
    assert out["toolsTotal"] == srv["tokens"]
    assert out["skills"]["count"] == 2
    assert out["history"] == max(0, 1000 - out["fixed"])
    assert out["policy"]["mcp_tools_disabled"] == {"srv-a": ["mcp_srv-a_write"]}
    assert out["threshold"] <= out["window"]


def test_breakdown_without_usage_falls_back_to_fixed(monkeypatch):
    monkeypatch.setattr(server_shared, "_mcp", _FakeRegistry(), raising=False)
    monkeypatch.setattr(
        "ginno_runtime.usage.last_chat_input_tokens", lambda _sid: None
    )
    monkeypatch.setattr(
        "ginno_runtime.graph_prompts.build_stable_system",
        lambda *a, **k: "stable system prompt",
    )
    from ginno_runtime.world_state import SkillsSection

    monkeypatch.setattr(
        SkillsSection, "snapshot", lambda self, ctx: {"index": "skills", "names": []}
    )
    out = build("sid", {"project_slug": "default"})
    assert out["measured"] is False
    assert out["used"] == out["fixed"]
    assert out["history"] == 0

def test_endpoint_wiring(monkeypatch):
    """GET /api/sessions/{id}/context_breakdown 的落点与 meta 解析链。"""
    import asyncio

    from ginno_runtime import server_shared as shared
    from ginno_runtime.api import sessions as sessions_api
    from ginno_runtime.world_state import SkillsSection

    monkeypatch.setattr(shared, "_mcp", _FakeRegistry(), raising=False)
    monkeypatch.setattr(
        "ginno_runtime.session_meta._resolve_session_meta",
        lambda _sid: {"id": "sid", "provider": "prov_x", "model": "model-y",
                      "agent_id": "dev", "workspace": "/tmp/ws"},
    )
    monkeypatch.setattr(
        sessions_api, "_find_meta",
        lambda _sid: ({"id": "sid", "provider": "prov_x", "model": "model-y"}, "default"),
    )
    monkeypatch.setattr("ginno_runtime.usage.last_chat_input_tokens", lambda _sid: 500)
    monkeypatch.setattr(
        "ginno_runtime.graph_prompts.build_stable_system", lambda *a, **k: "sys"
    )
    monkeypatch.setattr(
        SkillsSection, "snapshot", lambda self, ctx: {"index": "skills", "names": []}
    )

    out = asyncio.run(sessions_api.get_context_breakdown("sid"))
    assert out["ok"] is True
    assert out["used"] == 500
    assert out["tools"][0]["server"] == "srv-a"
    assert out["window"] >= 1
