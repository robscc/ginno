"""Issue #7: workflow resume/continue 继承原 run 的 agent-call 预算并跨段
累计——续跑段不能从 0 重新起表（否则预算闸被 pause→resume / failure→
continue 绕过）。"""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import engine
from ginno_runtime.workflows.budget import AgentCallBudget

pytestmark = pytest.mark.unit


def _two_llm_dsl():
    return {
        "name": "bud",
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "llm", "prompt": "a"},
            {"id": "s2", "type": "llm", "prompt": "b"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }


@pytest.mark.asyncio
async def test_resume_carries_spent_and_exceeds_budget():
    """pause→resume：第一段花 1 次后暂停；续跑段带 spent=1 + limit=2，
    第二次调用后恰好耗尽，第三次 BudgetExceeded——跨段累计生效。"""
    model = ScriptedChatModel(scripts=[script(text="a"), script(text="b"), script(text="c")])
    out: dict = {}
    agen = engine.run_workflow(
        _two_llm_dsl(), run_id="bud1", model=model, tools=[],
        project_slug="unit-bud", max_agent_calls=2, budget_state_out=out,
    )
    events: list[dict] = []
    async for ev in agen:
        events.append(ev)
        if ev["kind"] == "node_exit" and ev.get("node_id") == "s1":
            assert engine.request_pause("bud1") is True
            break
    async for ev in agen:
        events.append(ev)
    assert events[-1]["kind"] == "paused"
    assert out == {"spent": 1, "limit": 2}

    out2: dict = {}
    budget_seen: dict = {}

    # 在续跑段内窥视 engine 建的预算对象：spent 必须从 1 起表。
    orig = engine._make_agent_budget

    def spy(dsl, max_agent_calls, spent=0):
        b = orig(dsl, max_agent_calls, spent)
        budget_seen["limit"] = b.limit
        budget_seen["spent0"] = b.spent
        return b

    engine._make_agent_budget = spy
    try:
        events2 = []
        async for ev in engine.resume_workflow(
            _two_llm_dsl(), run_id="bud1", model=model, tools=[],
            resume_value={"decision": "continue"}, project_slug="unit-bud",
            resume_nature="manual",
            max_agent_calls=2, agent_calls_spent=out["spent"],
            budget_state_out=out2,
        ):
            events2.append(ev)
    finally:
        engine._make_agent_budget = orig

    assert budget_seen["limit"] == 2 and budget_seen["spent0"] == 1
    assert events2 and events2[-1]["kind"] == "done"
    assert out2 == {"spent": 2, "limit": 2}  # s2 又花 1 次 → 累计 2


@pytest.mark.asyncio
async def test_resume_exhausted_budget_refuses_next_call():
    """跨段超限：spent=2/limit=2 时续跑段的下一个 agent 调用被拒
    （BudgetExceeded 以 error 事件落地，节点不执行）。"""
    model = ScriptedChatModel(scripts=[script(text="a"), script(text="never")])
    agen = engine.run_workflow(
        _two_llm_dsl(), run_id="bud2", model=model, tools=[],
        project_slug="unit-bud2", max_agent_calls=2,
    )
    events = []
    async for ev in agen:
        events.append(ev)
        if ev["kind"] == "node_exit" and ev.get("node_id") == "s1":
            assert engine.request_pause("bud2") is True
            break
    async for ev in agen:
        events.append(ev)
    assert events[-1]["kind"] == "paused"

    events2 = []
    async for ev in engine.resume_workflow(
        _two_llm_dsl(), run_id="bud2", model=model, tools=[],
        resume_value={"decision": "continue"}, project_slug="unit-bud2",
        resume_nature="manual",
        max_agent_calls=1, agent_calls_spent=1,  # 续跑段首笔即超限
    ):
        events2.append(ev)
    kinds = [e["kind"] for e in events2]
    assert "error" in kinds
    err = next(e for e in events2 if e["kind"] == "error")
    assert "budget" in str(err.get("error", "")).lower()
    assert "s2" in str(err.get("error", ""))


@pytest.mark.asyncio
async def test_continue_inherits_budget():
    """failure→continue（retry-from-checkpoint）：段参数接续 spent。"""
    model = ScriptedChatModel(scripts=[script(text="z")])
    seen: dict = {}
    orig = engine._make_agent_budget

    def spy(dsl, max_agent_calls, spent=0):
        b = orig(dsl, max_agent_calls, spent)
        seen["limit"], seen["spent0"] = b.limit, b.spent
        return b

    engine._make_agent_budget = spy
    out: dict = {}
    try:
        async for _ in engine.continue_workflow(
            _two_llm_dsl(), run_id="bud3", model=model, tools=[],
            project_slug="unit-bud3",
            max_agent_calls=5, agent_calls_spent=3, budget_state_out=out,
        ):
            pass
    finally:
        engine._make_agent_budget = orig
    assert seen["limit"] == 5 and seen["spent0"] == 3


def test_make_agent_budget_rejects_bad_spent() -> None:
    b = engine._make_agent_budget({}, None, 0)
    assert isinstance(b, AgentCallBudget) and b.limit == 0
