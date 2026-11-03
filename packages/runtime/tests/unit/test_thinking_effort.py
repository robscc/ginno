"""会话级思考强度（thinking-effort-design.md §2/§3）。

三层，全部无网络：

1. 助手纯函数 — normalize_effort / thinking_budgets / thinking_dialect。
2. build_model 映射 — 三种协议各出各的参数形状；auto（None）不注入任何
   thinking 参数（provider 配置的 enable_thinking 老路不受影响）。
3. 会话链路 — 创建解析链（req > agent > auto）、meta 落盘、PATCH 热切与
   非法值归一。
"""

from __future__ import annotations

import pytest

from ginno_runtime import providers as prov_mod
from ginno_runtime.models import (
    build_model,
    normalize_effort,
    thinking_budgets,
    thinking_dialect,
)

pytestmark = pytest.mark.unit


def _cfg(**over) -> dict:
    base = {
        "enabled": True,
        "protocol": "openai-compatible",
        "name": "t",
        "api_key": "sk-x",
        "base_url": "https://example.com/v1",
        "model": "some-model",
        "max_tokens": 100,
        "temperature": 0.7,
        "timeout_s": 60,
        "enable_search": False,
        "enable_thinking": False,
    }
    base.update(over)
    return base


def _patch(monkeypatch, cfg: dict, pid: str = "custom") -> None:
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {pid: cfg})


# ---- layer 1: 纯函数 ----


def test_normalize_effort():
    assert normalize_effort("HIGH") == "high"
    assert normalize_effort(" low ") == "low"
    # auto / 空 / None / 非法 → None（跟随 provider 配置）
    for v in ("auto", "", None, "extreme", 123):
        assert normalize_effort(v) is None


def test_thinking_budgets_defaults_and_override():
    assert thinking_budgets({}) == {"low": 4096, "medium": 16384, "high": 32768}
    out = thinking_budgets({"thinking_budgets": {"low": 2048, "high": "junk", "medium": 0}})
    assert out["low"] == 2048
    assert out["medium"] == 16384  # 非正整数不覆盖
    assert out["high"] == 32768  # 非数值不覆盖


def test_thinking_dialect():
    assert thinking_dialect({"base_url": ""}, "qwen3-max") == "qwen"
    assert thinking_dialect({"base_url": "https://dashscope.aliyuncs.com/v1"}, "foo") == "qwen"
    assert thinking_dialect({"base_url": ""}, "glm-4.6") == "glm"
    assert thinking_dialect({"base_url": "https://open.bigmodel.cn/api"}, "foo") == "glm"
    assert thinking_dialect({"base_url": ""}, "deepseek-reasoner") == "deepseek"
    assert thinking_dialect({"base_url": ""}, "gpt-4o") == "generic"


# ---- layer 2: build_model 三协议映射 ----


def test_compat_qwen_effort_sets_extra_body(monkeypatch):
    _patch(monkeypatch, _cfg(model="qwen3-max"))
    m = build_model("custom", thinking_effort="high")
    eb = getattr(m, "extra_body", None) or {}
    assert eb.get("enable_thinking") is True
    assert eb.get("thinking_budget") == 32768


def test_compat_qwen_off_disables_thinking(monkeypatch):
    # 配置开了 enable_thinking，档位 off 必须压掉（档位 > 配置）。
    _patch(monkeypatch, _cfg(model="qwen3-max", enable_thinking=True))
    m = build_model("custom", thinking_effort="off")
    eb = getattr(m, "extra_body", None) or {}
    assert eb.get("enable_thinking") is False


def test_compat_glm_effort_uses_thinking_switch(monkeypatch):
    _patch(monkeypatch, _cfg(model="glm-4.6"))
    m = build_model("custom", thinking_effort="medium")
    eb = getattr(m, "extra_body", None) or {}
    assert eb.get("thinking") == {"type": "enabled"}
    m2 = build_model("custom", thinking_effort="off")
    eb2 = getattr(m2, "extra_body", None) or {}
    assert eb2.get("thinking") == {"type": "disabled"}


def test_compat_generic_effort_passthrough_reasoning(monkeypatch):
    _patch(monkeypatch, _cfg(model="gpt-4o"))
    m = build_model("custom", thinking_effort="low")
    assert getattr(m, "reasoning_effort", None) == "low"
    m2 = build_model("custom", thinking_effort="off")
    assert getattr(m2, "reasoning_effort", None) == "minimal"


def test_compat_auto_keeps_legacy_enable_thinking(monkeypatch):
    # auto（None）：老路完全不变——配置的 enable_thinking 生效，无档位注入。
    _patch(monkeypatch, _cfg(model="qwen3-max", enable_thinking=True))
    m = build_model("custom")
    eb = getattr(m, "extra_body", None) or {}
    assert eb.get("enable_thinking") is True
    assert "thinking_budget" not in eb


def test_thinking_mode_none_gates_effort(monkeypatch):
    # provider 显式声明不支持 → 档位按 auto 处理（服务端兜底，§7）。
    _patch(monkeypatch, _cfg(model="qwen3-max", thinking_mode="none"))
    m = build_model("custom", thinking_effort="high")
    eb = getattr(m, "extra_body", None) or {}
    assert not eb.get("enable_thinking")
    assert "thinking_budget" not in eb


def test_anthropic_effort_budget_and_constraints(monkeypatch):
    cfg = _cfg(protocol="anthropic", model="claude-sonnet-4-5", max_tokens=8192)
    _patch(monkeypatch, cfg)
    m = build_model("custom", thinking_effort="medium")
    assert m.thinking == {"type": "enabled", "budget_tokens": 16384}
    # Anthropic 约束：开思考 temperature 必须 1；max_tokens 必须 > budget。
    assert float(m.temperature) == 1.0
    assert int((m.model_kwargs or {}).get("max_tokens") or m.max_tokens or 0) >= 16384 + 4096


def test_anthropic_off_disables_thinking(monkeypatch):
    cfg = _cfg(protocol="anthropic", model="claude-sonnet-4-5")
    _patch(monkeypatch, cfg)
    m = build_model("custom", thinking_effort="off")
    assert m.thinking == {"type": "disabled"}
    # off 不触 temperature/max_tokens 约束改写
    assert float(m.temperature) == 0.7


def test_anthropic_auto_no_thinking_param(monkeypatch):
    cfg = _cfg(protocol="anthropic", model="claude-sonnet-4-5")
    _patch(monkeypatch, cfg)
    m = build_model("custom")
    assert m.thinking is None


def test_responses_effort_maps_reasoning(monkeypatch):
    cfg = _cfg(protocol="openai-responses", model="o4-mini", api_key="sk-x")
    _patch(monkeypatch, cfg)
    m = build_model("custom", thinking_effort="high")
    assert m.reasoning_effort == "high"
    m2 = build_model("custom", thinking_effort="off")
    assert m2.reasoning_effort == "minimal"
    m3 = build_model("custom")
    assert m3.reasoning_effort is None


# ---- layer 3: 会话链路（创建解析 / meta 落盘 / PATCH 热切） ----


def test_session_create_stores_effort_and_resolves_chain(
    client, patch_build_model, monkeypatch
):
    """创建链：显式 req > agent 默认 > auto；meta 落盘归一值。"""
    from ginno_runtime import agents as agents_reg
    from ginno_runtime.agents.registry import AgentConfig

    # isolated home 里 provider 全 disabled——本测试只关心档位解析，模型用 fake
    patch_build_model(object())

    # 真 AgentConfig：provider 默认 "custom"（非 deliberate）→ 走全局默认解析
    fake = AgentConfig(id="dev", name="Dev", thinking_effort="medium")
    monkeypatch.setattr(
        agents_reg, "get_agent", lambda aid: fake if aid == "dev" else None
    )
    # 显式档位压过 agent 默认
    r = client.post(
        "/api/sessions",
        json={"project_slug": "default", "workspace": "/tmp/x", "agent_id": "dev",
              "thinking_effort": "high"},
    ).json()
    assert r.get("ok") is not False
    assert r["thinking_effort"] == "high"

    # 未显式 → agent 默认
    r2 = client.post(
        "/api/sessions",
        json={"project_slug": "default", "workspace": "/tmp/x", "agent_id": "dev"},
    ).json()
    assert r2["thinking_effort"] == "medium"

    # 无 agent 默认 → auto
    r3 = client.post(
        "/api/sessions",
        json={"project_slug": "default", "workspace": "/tmp/x"},
    ).json()
    assert r3["thinking_effort"] == "auto"


def test_patch_thinking_effort_hot_swap_and_normalize(client, monkeypatch):
    """PATCH 换档：归一落盘、原地热切、非法值回 auto；事件带 thinking_effort。"""
    import ginno_runtime.api.sessions as S

    built: list[tuple] = []

    def fake_build(pid, model=None, session_id=None, thinking_effort=None):
        built.append((pid, model, thinking_effort))
        return object()

    monkeypatch.setattr(S, "build_model", fake_build)
    sid = client.post(
        "/api/sessions", json={"project_slug": "default", "workspace": "/tmp/x"}
    ).json()["id"]
    built.clear()

    r = client.patch(f"/api/sessions/{sid}", json={"thinking_effort": "HIGH"}).json()
    assert r["ok"] is True
    assert r["session"]["thinking_effort"] == "high"
    # 热切：重建了模型对象并带上档位
    assert built and built[-1][2] == "high"
    # 条目里的落盘值同步（_ensure_session 重启重建读它）
    assert S._SESSIONS[sid]["thinking_effort"] == "high"

    # 回 auto：build 收到 None（跟随 provider 配置）
    r2 = client.patch(f"/api/sessions/{sid}", json={"thinking_effort": "auto"}).json()
    assert r2["session"]["thinking_effort"] == "auto"
    assert built[-1][2] is None

    # 非法值：normalize 成 auto，不 400（宽进严出）
    r3 = client.patch(f"/api/sessions/{sid}", json={"thinking_effort": "extreme"}).json()
    assert r3["ok"] is True
    assert r3["session"]["thinking_effort"] == "auto"
