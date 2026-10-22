"""Run 级 LLM 调用预算 + 跨 run 并发闸(odw-comparison P1-③)。"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime.workflows.budget import AgentCallBudget, BudgetExceeded, resolve_limit

pytestmark = pytest.mark.unit


def test_budget_zero_means_unlimited() -> None:
    b = AgentCallBudget(0)
    assert not b.enabled
    for _ in range(50):
        b.spend("n1")
    assert b.spent == 50


def test_budget_spend_refuses_over_limit() -> None:
    b = AgentCallBudget(3)
    b.spend("n1")
    b.spend("n1")
    b.spend("n2")
    assert b.spent == 3
    with pytest.raises(BudgetExceeded) as ei:
        b.spend("n3")
    assert ei.value.limit == 3 and ei.value.node_id == "n3"
    assert "budget" in str(ei.value).lower()


def test_resolve_limit_priority_run_over_dsl_over_setting() -> None:
    assert resolve_limit(5, {"budget": {"maxAgentCalls": 9}}, 7) == 5
    assert resolve_limit(None, {"budget": {"maxAgentCalls": 9}}, 7) == 9
    assert resolve_limit(None, {}, 7) == 7
    assert resolve_limit(None, {}, None) == 0
    assert resolve_limit("bad", {"budget": {"maxAgentCalls": "x"}}, "y") == 0


def test_budget_exceeded_not_retried_by_resilient_wrapper() -> None:
    """BudgetExceeded 走 retry 边界的控制流直通:on_error=continue 也不吃它
    在 retry 层稀释(预算错误的『重试』只会再花一次计数)。"""
    from ginno_runtime.workflows.nodes.base import BaseNode

    calls = {"n": 0}

    class _Probe(BaseNode):
        type = "probe"

        @staticmethod
        async def execute(node, cctx, state, config, eff):
            calls["n"] += 1
            from ginno_runtime.workflows.budget import BudgetExceeded

            raise BudgetExceeded(node_id=node["id"], spent=99, limit=98)

    budget = AgentCallBudget(1)
    budget.spent = 98  # 已近耗尽
    run_ctx: dict = {"events": [], "agent_budget": budget}
    cctx = {"run_ctx": run_ctx, "dsl": {}}
    node = {"id": "p1", "retry": {"max_attempts": 5}, "on_error": "continue"}
    with pytest.raises(BudgetExceeded):
        asyncio.run(_Probe._execute_resilient(node, cctx, {}, None, {}))
    assert calls["n"] == 1  # 没有重试


def test_agent_node_spends_budget() -> None:
    """AgentNode 执行入口 spend:超限抛 BudgetExceeded(执行体不跑)。"""
    from ginno_runtime.workflows.nodes.builtin import AgentNode

    ran = {"n": 0}

    async def fake_agent_turn(*a, **k):
        ran["n"] += 1
        return "out", {}, None

    budget = AgentCallBudget(1)
    budget.spent = 1  # 已满
    run_ctx: dict = {"events": [], "run_id": "r1", "agent_budget": budget}

    import ginno_runtime.workflows.nodes.builtin as B

    orig = B._run_agent_turn
    B._run_agent_turn = fake_agent_turn
    try:
        state = {"context": {}, "loop_vars": {}}
        with pytest.raises(BudgetExceeded):
            asyncio.run(AgentNode.execute({"id": "a1"}, {"run_ctx": run_ctx}, state, None, {}))
        assert ran["n"] == 0
    finally:
        B._run_agent_turn = orig
