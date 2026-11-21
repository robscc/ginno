"""mid-turn compaction（turn 中压缩，2026-11-21 P0+P1）单测。

P0：外推充裕化（compaction.maybe_compact_history 的 forecast 分支）。
P1：compact_check 图内节点（软限判定/尾窗切分/RemoveMessage 写回/节流）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _mk_node(model=None, settings=None, est_ret=None, boost=1.0):
    """构造 compact_check 节点 + 可控依赖。"""
    from ginno_runtime import graph as gmod

    factory = gmod._compact_check_node_factory(model or AsyncMock())
    node = factory
    return node


class _FakeMsgs:
    pass


def _msgs(n_head: int, n_turn_tail: int):
    """历史轮次 n_head 条 + 当前 turn 尾部 n_turn_tail 条（均非 system）。"""
    out = [HumanMessage(content=f"old-{i} " + "x" * 200) for i in range(n_head)]
    out += [AIMessage(content=f"tail-{i}") for i in range(n_turn_tail)]
    return out


@pytest.mark.asyncio
async def test_under_soft_limit_passthrough(monkeypatch):
    """未超软限：直通（无 state 更新、无 LLM 调用）。"""
    from ginno_runtime.compaction import estimate_messages_tokens
    from ginno_runtime.graph import _compact_check_node_factory

    model = AsyncMock()
    node = _compact_check_node_factory(model)

    def fake_settings():
        return {"compact_mid_turn": True, "compact_mid_headroom_tokens": 32768}

    monkeypatch.setattr(
        "ginno_runtime.compaction.estimate_messages_tokens", lambda m: 1000
    )
    import ginno_runtime.compaction as cmod

    monkeypatch.setattr(
        cmod, "_effective_compact_threshold", lambda s, sess: 500_000
    )
    import ginno_runtime.world_state as ws

    monkeypatch.setattr(ws, "context_settings", fake_settings)
    msgs = _msgs(6, 6)
    out = await node(
        {"messages": msgs}, {"configurable": {"session_id": "s-mid"}}
    )
    assert out == {}
    model.ainvoke.assert_not_called()


@pytest.mark.asyncio
async def test_over_soft_limit_compacts_with_tail_kept(monkeypatch):
    """超软限：摘录旧段（RemoveMessage）+ 摘要消息；尾窗原样保留。"""
    import ginno_runtime.compaction as cmod
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import _compact_check_node_factory

    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="SUMMARY-TEXT"))
    node = _compact_check_node_factory(model)

    def fake_settings():
        return {"compact_mid_turn": True}

    monkeypatch.setattr(cmod, "estimate_messages_tokens", lambda m: 900_000)
    monkeypatch.setattr(
        cmod, "_effective_compact_threshold", lambda s, sess: 500_000
    )
    monkeypatch.setattr(ws, "context_settings", fake_settings)

    msgs = _msgs(10, 6)
    for m in msgs:  # 消息 id（RemoveMessage 需要）
        m.id = f"m-{id(m)}"
    out = await node(
        {"messages": msgs}, {"configurable": {"session_id": "s-mid"}}
    )
    from langchain_core.messages import RemoveMessage

    removes = [x for x in out["messages"] if isinstance(x, RemoveMessage)]
    summaries = [x for x in out["messages"] if isinstance(x, HumanMessage)]
    # 尾窗语义 = 消息尾部 12 条（跨 turn 边界，不必识别 turn 起点——
    # 最近 K 条即工作记忆）：16 条消息的尾 12 被保留，old 只有前 4 进
    # 摘要 → 4 个 Remove + 1 摘要。
    assert len(removes) == 4
    assert len(summaries) == 1
    assert "SUMMARY-TEXT" in summaries[0].content
    assert "compact" in summaries[0].content


@pytest.mark.asyncio
async def test_summary_failure_passthrough(monkeypatch):
    """摘要失败：放行（空更新），turn 不被破坏。"""
    import ginno_runtime.compaction as cmod
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import _compact_check_node_factory

    model = AsyncMock()
    model.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
    node = _compact_check_node_factory(model)

    def fake_settings():
        return {"compact_mid_turn": True}

    monkeypatch.setattr(cmod, "estimate_messages_tokens", lambda m: 900_000)
    monkeypatch.setattr(
        cmod, "_effective_compact_threshold", lambda s, sess: 500_000
    )
    monkeypatch.setattr(ws, "context_settings", fake_settings)
    out = await node({"messages": _msgs(5, 5)}, {"configurable": {"session_id": "s"}})
    assert out == {}


def test_forecast_growth_tokens_grouping(monkeypatch):
    """P0 增量口径：最近 turn 分组的 max(input)+max(output)。"""
    from ginno_runtime import usage

    rows = [
        {"turn_id": "t2", "input_tokens": 100, "output_tokens": 30,
         "source": "chat"},
        {"turn_id": "t2", "input_tokens": 150, "output_tokens": 20,
         "source": "chat"},
        {"turn_id": "t1", "input_tokens": 60, "output_tokens": 10,
         "source": "chat"},
    ]
    import ginno_runtime.usage_store as ustore

    monkeypatch.setattr(
        ustore, "query_requests", lambda **kw: {"rows": rows}
    )
    assert usage.last_turn_growth_tokens("s") == 150 + 30
    monkeypatch.setattr(ustore, "query_requests", lambda **kw: {"rows": []})
    assert usage.last_turn_growth_tokens("s") is None


@pytest.mark.asyncio
async def test_integration_long_turn_triggers_midcompact(isolated_home, monkeypatch):
    """graph 级集成：低软限 + 多轮工具循环 → mid 压缩发生且 turn 不失败，
    尾窗与摘要消息共存于终态 messages。"""
    from langchain_core.messages import HumanMessage

    import ginno_runtime.compaction as cmod
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import script_tool_call
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    def fake_settings():
        return {"compact_mid_turn": True, "world_state": False}

    monkeypatch.setattr(ws, "context_settings", fake_settings)
    # 软限压到极低：任何消息量都超 → 首轮循环即触发
    monkeypatch.setattr(
        cmod, "_effective_compact_threshold", lambda s, sess: 4096 + 100
    )
    monkeypatch.setattr(
        cmod,
        "estimate_messages_tokens",
        lambda msgs: 99_999_999,
    )

    class _Summarizer(ScriptedChatModel):
        pass

    # 调用序列：agent(tool_call) → compact_check(摘要=第2次 ainvoke)
    # → agent(final=第3次)——scripts 按序供三条。
    model = ScriptedChatModel(
        scripts=[
            script(
                tool_calls=[
                    script_tool_call(
                        "use_skill", {"name": "browser", "request": "x"}
                    )
                ]
            ),
            script(text="SUMMARY-OF-OLD"),
            script(text="done after compact"),
        ]
    )
    from ginno_runtime.tools.skill_tools import build_skill_tools

    graph = build_graph(
        model=model,
        project_slug="default",
        workspace="/tmp",
        mcp_tools=[],
        all_tools=build_skill_tools("default"),
    )
    cfg = {
        "configurable": {
            "thread_id": "t-midcompact",
            "project_slug": "default",
            "agent_id": "dev",
        }
    }
    result = await graph.ainvoke(
        {
            "messages": (
                [HumanMessage(content="跑个长任务")]
                + [HumanMessage(content=f"历史消息{i}" + "y" * 120) for i in range(14)]
            ),
            "workspace": "/tmp",
            "project_slug": "default",
            "agent_id": "dev",
            "active_skills": [],
            "pending_tool_calls": [],
        },
        config=cfg,
    )
    msgs = result["messages"]
    # turn 完成且末条是最终文本
    assert "done after compact" in str(msgs[-1].content)
    # 摘要消息在场（mid 压缩发生过；fake model 的 ainvoke 也被节点复用
    # 产生摘要——断言存在 compact 标记）
    assert any("[compact]" in str(m.content) for m in msgs)
    assert any("SUMMARY-OF-OLD" in str(m.content) for m in msgs)
