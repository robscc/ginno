"""Instincts L2（prefilter / observer 位点与判定）单测——LLM 全 mock。"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.instincts import observe
from ginno_runtime.instincts.store import observations_path
from ginno_runtime.instincts import observer as obs_mod
from ginno_runtime.instincts.prefilter import correction_reason

pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_l2(isolated_home, monkeypatch):
    monkeypatch.setattr("ginno_runtime.instincts.types._read_settings", lambda: {})
    from ginno_runtime.instincts.store import instincts_root

    instincts_root().mkdir(parents=True, exist_ok=True)
    return isolated_home


def _enable_observer(monkeypatch):
    monkeypatch.setattr(
        "ginno_runtime.instincts.types._read_settings",
        lambda: {"instincts": {"observer_model": "mock"}},
    )


def _fake_model(monkeypatch, payload: dict):
    class R:
        content = json.dumps(payload)
    class M:
        async def ainvoke(self, msgs):
            return R()
    monkeypatch.setattr(obs_mod, "build_model", lambda p, m=None: M())
    monkeypatch.setattr(obs_mod.usage_mod, "record_message", lambda *a, **k: None)


def test_prefilter_correction_words_and_after_deny():
    assert correction_reason("不对，应该用 B 方案", []) == "correction-word"
    assert correction_reason("please stop and undo", []) == "correction-word"
    assert correction_reason("帮我继续", [{"type": "tool_denied", "tool": "bash"}]) == "after-deny"
    assert correction_reason("帮我继续", []) is None
    assert correction_reason("", []) is None


def test_capture_correction_signal_after_deny(isolated_l2):
    observe({"type": "tool_denied", "tool": "bash", "args_repr": "rm x",
             "project": "demo"})
    assert obs_mod.capture_correction_signal("不对，换个思路", "s1") is True
    lines = observations_path().read_text().strip().splitlines()
    ev = json.loads(lines[-1])
    assert ev["type"] == "user_correction"
    assert ev["reason"] == "correction-word"
    assert "tool bash" in ev["before"]


async def test_observer_correction_threshold_and_model_error(isolated_l2):
    """分型门槛（P0）：纠偏 ≥3 即触发（不再共用 20）；无 provider →
    model-error 且位点不前进（配置修好可重试，不再静默跳过）。"""
    r = await obs_mod.run_observer("s")
    assert r["skipped"] == "no-observations"
    for _ in range(2):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "x", "after": "y"})
    r2 = await obs_mod.run_observer("s")
    assert r2["skipped"] == "few-observations"  # 2 < 纠偏门槛 3
    observe({"type": "user_correction", "reason": "correction-word",
             "before": "x", "after": "y"})
    r3 = await obs_mod.run_observer("s")
    assert r3["skipped"] == "model-error"  # 隔离环境无默认 provider
    assert obs_mod._load_state()["offset"] == 0


async def test_observer_follows_main_model_when_unset(isolated_l2, monkeypatch):
    """observer_model 空 = 跟随主模型（设计 §10.1 语义，2026-11-15 恢复）。"""
    monkeypatch.setattr(
        "ginno_runtime.providers.get_default_provider", lambda *a, **k: "main-prov"
    )
    seen = {}

    class R:
        content = json.dumps({"is_rule": True, "trigger": "派发任务前",
                              "behavior": "先确认范围", "domain": "workflow",
                              "scope": "global"})

    class M:
        async def ainvoke(self, msgs):
            return R()

    def fake_build(p, m=None):
        seen["prov"] = p
        return M()

    monkeypatch.setattr(obs_mod, "build_model", fake_build)
    monkeypatch.setattr(obs_mod.usage_mod, "record_message", lambda *a, **k: None)
    for _ in range(3):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "b", "after": "a"})
    r = await obs_mod.run_observer("s")
    assert r["ok"] and r["created"] == 1
    assert seen["prov"] == "main-prov"


async def test_observer_force_bypasses_throttle(isolated_l2, monkeypatch):
    _enable_observer(monkeypatch)
    _fake_model(monkeypatch, {
        "is_rule": True, "trigger": "force 触发", "behavior": "force 行为",
        "domain": "workflow", "scope": "project",
    })
    observe({"type": "user_correction", "reason": "correction-word",
             "before": "b", "after": "a", "project": "demo"})
    r = await obs_mod.run_observer("s")
    assert r["skipped"] == "few-observations"  # 1 < 3
    r2 = await obs_mod.run_observer("s", force=True)
    assert r2["ok"] and r2["created"] == 1
    # 成功后节流窗口内再跑 → gap；force 仍可跳过
    for _ in range(3):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "b", "after": "a", "project": "demo"})
    r3 = await obs_mod.run_observer("s")
    assert r3["skipped"] == "gap"
    r4 = await obs_mod.run_observer("s", force=True)
    assert r4["ok"]


def test_prefilter_skips_system_injected():
    """子代理 brief/结果回注/长角色 prompt 不是用户纠偏（P0 降噪）。"""
    from ginno_runtime.instincts.prefilter import is_system_injected

    brief = "你是实现型子代理：在 goal 约定的范围内直接改代码。" + "边界纪律：不要碰范围外文件。" * 20
    assert is_system_injected(brief)
    assert correction_reason(brief, []) is None
    result_block = '<ginno_subagent_result session="s">不要改文件</ginno_subagent_result>'
    assert correction_reason(result_block, []) is None
    # worker 回注 / 任务状态回注同口径（2026-11-15 真实观察流里发现的形态）
    assert correction_reason('<ginno_worker_assign from="x">不要停</ginno_worker_assign>', []) is None
    assert correction_reason('[task stopped by user] "build" (t_1) after 3s', []) is None
    # 真实用户纠偏不受影响
    assert correction_reason("后续不要帮我决定了", []) == "correction-word"


async def test_capture_correction_signal_skips_subagent_brief(isolated_l2):
    brief = "你是实现型子代理：直接改代码。" + "不要碰范围外文件。" * 30
    assert obs_mod.capture_correction_signal(brief, "s1") is False


async def test_observer_skips_system_injected_observations(isolated_l2, monkeypatch):
    """存量噪声观察不喂 LLM（捕获侧漏过的，分析侧同口径兜底）。"""
    _enable_observer(monkeypatch)
    calls = []

    class R:
        content = json.dumps({"is_rule": True, "trigger": "t", "behavior": "b"})

    class M:
        async def ainvoke(self, msgs):
            calls.append(1)
            return R()

    monkeypatch.setattr(obs_mod, "build_model", lambda p, m=None: M())
    monkeypatch.setattr(obs_mod.usage_mod, "record_message", lambda *a, **k: None)
    for _ in range(3):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "", "after": "<ginno_subagent_result>不要改</ginno_subagent_result>"})
    r = await obs_mod.run_observer("s")
    assert r["ok"] and r["created"] == 0 and not calls


async def test_observer_llm_judgment_creates_pending(isolated_l2, monkeypatch):
    _enable_observer(monkeypatch)
    _fake_model(monkeypatch, {
        "is_rule": True, "trigger": "生成 SQL 前", "behavior": "先 EXPLAIN 给用户看",
        "domain": "code-style", "scope": "project",
    })
    for _ in range(20):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "直接跑了 delete", "after": "不对，先给我看影响行数",
                 "project": "demo"})
    r = await obs_mod.run_observer("s")
    assert r["ok"] and r["created"] == 1
    from ginno_runtime.instincts import list_instincts
    inst = [i for i in list_instincts() if i.source == "user-correction"][0]
    assert inst.status == "pending"
    assert inst.behavior.startswith("先 EXPLAIN")
    # 位点前进：再跑 → few-observations
    r2 = await obs_mod.run_observer("s")
    assert r2["skipped"] == "few-observations"


async def test_observer_matching_reinforces_not_duplicating(isolated_l2, monkeypatch):
    _enable_observer(monkeypatch)
    from ginno_runtime.instincts.store import save_instinct
    from ginno_runtime.instincts.types import Instinct
    save_instinct(Instinct(id="e1", trigger="生成 SQL 前", behavior="先 EXPLAIN",
                           confidence=0.5, status="pending", scope="project",
                           project_slug="demo"))
    _fake_model(monkeypatch, {
        "is_rule": True, "trigger": "生成 SQL 前", "behavior": "另一个行为",
        "domain": "code-style", "scope": "project",
    })
    for _ in range(20):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "b", "after": "a", "project": "demo"})
    r = await obs_mod.run_observer("s")
    assert r["ok"] and r["reinforced"] >= 1 and r["created"] == 0
    from ginno_runtime.instincts import list_instincts
    assert len([i for i in list_instincts() if i.source == "user-correction"]) == 0


def test_observer_state_and_analyze_endpoints(isolated_l2, monkeypatch):
    """P0 可见化：observer-state 状态行 + analyze 立即分析（force）。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ginno_runtime.api import instincts as api_mod

    app = FastAPI()
    app.include_router(api_mod.router)
    client = TestClient(app)

    st = client.get("/api/instincts/observer-state").json()
    # 路由顺序守卫：observer-state 不能被 /{instinct_id} 吞掉
    assert "resolved_provider" in st
    assert st["total"] == 0 and st["fresh"] == 0 and st["enabled"] is True

    r = client.post("/api/instincts/analyze").json()
    assert r["skipped"] == "no-observations"

    _enable_observer(monkeypatch)
    _fake_model(monkeypatch, {
        "is_rule": True, "trigger": "api 触发", "behavior": "api 行为",
        "domain": "workflow", "scope": "project",
    })
    for _ in range(3):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "b", "after": "a", "project": "demo"})
    r2 = client.post("/api/instincts/analyze").json()
    assert r2["ok"] and r2["created"] == 1
    st2 = client.get("/api/instincts/observer-state").json()
    assert st2["offset"] == 3 and st2["fresh"] == 0


async def test_observer_vague_correction_not_rule(isolated_l2, monkeypatch):
    _enable_observer(monkeypatch)
    _fake_model(monkeypatch, {"is_rule": False})
    for _ in range(20):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "b", "after": "a"})
    r = await obs_mod.run_observer("s")
    assert r["ok"] and r["created"] == 0
