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


async def test_observer_gap_and_min_observations(isolated_l2):
    r = await obs_mod.run_observer("s")
    assert r["skipped"] == "no-observations"
    for _ in range(5):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "x", "after": "y"})
    r2 = await obs_mod.run_observer("s")
    assert r2["skipped"] == "few-observations"  # 5 < 默认 20


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


async def test_observer_vague_correction_not_rule(isolated_l2, monkeypatch):
    _enable_observer(monkeypatch)
    _fake_model(monkeypatch, {"is_rule": False})
    for _ in range(20):
        observe({"type": "user_correction", "reason": "correction-word",
                 "before": "b", "after": "a"})
    r = await obs_mod.run_observer("s")
    assert r["ok"] and r["created"] == 0
