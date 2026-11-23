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

    model = model or AsyncMock()
    factory = gmod._compact_check_node_factory(lambda: model)
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
    node = _compact_check_node_factory(lambda: model)

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
    node = _compact_check_node_factory(lambda: model)

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
    node = _compact_check_node_factory(lambda: model)

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


def test_forecast_growth_skips_context_baseline(isolated_home):
    """毒化回归（2026-11-21 实测定罪）：压缩落下的 context_baseline 行
    （无 turn_id、input=压后全量）不得被当成「上一 turn 增量」。真实 turn
    35k/2k + 基线 60k → growth 必须是 37000（修复前 60000，小窗口模型下
    压完下个 turn 必再触发全量压缩）；而 last_chat_input_tokens 仍须看到
    基线（f5e441b 有意行为，圆环/E3 分子即时校正）。"""
    import time as _time

    from ginno_runtime import usage_store
    from ginno_runtime.usage import last_chat_input_tokens, last_turn_growth_tokens

    t0 = _time.time()
    usage_store.record(
        input_tokens=35_000, output_tokens=2_000, provider="p", model="m",
        source="chat", session_id="audit-growth", turn_id="t1", ts=t0 - 3,
    )
    usage_store.record(
        input_tokens=30_000, output_tokens=1_500, provider="p", model="m",
        source="chat", session_id="audit-growth", turn_id="t1", ts=t0 - 2,
    )
    # 压缩成功后落下的水位基线：最新一条、无 turn_id、output=0
    usage_store.record(
        input_tokens=60_000, output_tokens=0, provider="", model="",
        source="context_baseline", session_id="audit-growth", ts=t0 - 1,
    )
    assert last_turn_growth_tokens("audit-growth") == 35_000 + 2_000
    assert last_chat_input_tokens("audit-growth") == 60_000


def test_forecast_growth_baseline_only_returns_none(isolated_home):
    """只有基线行（刚压缩完、尚无新对话记录）：growth=None（不外推），
    last_chat_input_tokens 仍回基线水位。"""
    from ginno_runtime import usage_store
    from ginno_runtime.usage import last_chat_input_tokens, last_turn_growth_tokens

    usage_store.record(
        input_tokens=60_000, output_tokens=0, provider="", model="",
        source="context_baseline", session_id="audit-base-only",
    )
    assert last_turn_growth_tokens("audit-base-only") is None
    assert last_chat_input_tokens("audit-base-only") == 60_000


@pytest.mark.asyncio
async def test_integration_long_turn_triggers_midcompact(isolated_home, monkeypatch):
    """graph 级集成：低软限 + 多轮工具循环 → mid 压缩发生且 turn 不失败，
    尾窗与摘要消息共存于终态 messages。"""
    from langchain_core.messages import HumanMessage

    import ginno_runtime.compaction as cmod
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import build_graph
    from ginno_runtime.server_shared import _SESSIONS, make_session_model_getter
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

    old_model = ScriptedChatModel(scripts=[])
    sid = "t-midcompact"
    graph = build_graph(
        model=old_model,
        model_getter=make_session_model_getter(sid, old_model),
        project_slug="default",
        workspace="/tmp",
        mcp_tools=[],
        all_tools=build_skill_tools("default"),
    )
    cfg = {
        "configurable": {
            "thread_id": sid,
            "project_slug": "default",
            "agent_id": "dev",
        }
    }
    monkeypatch.setitem(_SESSIONS, sid, {"model": model})
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
    # 审计 B/C 定罪钉：节点必须真的收到 langgraph 注入的 config（此前
    # config 参数注解 dict|None 被 RunnableCallable 拒绝注入，恒为 None
    # → 节流 key/事件 sid 全空）。真实路径下 key = thread_id。
    import ginno_runtime.graph as gmod

    assert gmod._MID_STATE.get("t-midcompact") == (1, 1.25)
    assert "" not in gmod._MID_STATE
    gmod.reset_mid_compact_state("t-midcompact")


# --------------------------------------------------------------------------- #
# 审计 A（2026-11-21 P0）：尾窗切分不得制造孤儿 ToolMessage
# --------------------------------------------------------------------------- #
def _mid_settings(**kw):
    base = {"compact_mid_turn": True}
    base.update(kw)
    return base


@pytest.mark.asyncio
async def test_tail_boundary_backs_off_orphan_toolmessage(monkeypatch):
    """切点落在 ToolMessage 上 → 回退到拥有它的 AIMessage（一起留在尾窗），
    尾窗永不以孤儿 ToolMessage 开头（配对 owner 被删 → 下一次模型调用
    400，已实测复现：t0 在尾窗首位、a0 在窗外）。"""
    import ginno_runtime.compaction as cmod
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import _compact_check_node_factory

    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="SUM"))
    node = _compact_check_node_factory(lambda: model)
    monkeypatch.setattr(cmod, "estimate_messages_tokens", lambda m: 900_000)
    monkeypatch.setattr(cmod, "_effective_compact_threshold", lambda s, sess: 500_000)
    monkeypatch.setattr(
        ws, "context_settings", lambda: _mid_settings(compact_mid_tail_messages=4)
    )

    # 非 system 序列（9 条，keep_tail=4）：
    #   [h0, h1, h2, h3, a0(tool_calls→t0), t0, a1, h4, a2]
    # 原始 cut=5 恰落在 t0（ToolMessage）→ 回退到 4：a0 与 t0 一起留在
    # 尾窗（修复前 a0 进摘要段被删，t0 成孤儿）。摘要段 = h0..h3。
    call = {"name": "bash", "args": {}, "id": "c0", "type": "tool_call"}
    msgs = [
        HumanMessage(content="old-0", id="h0"),
        HumanMessage(content="old-1", id="h1"),
        HumanMessage(content="old-2", id="h2"),
        HumanMessage(content="old-3", id="h3"),
        AIMessage(content="", tool_calls=[call], id="a0"),
        ToolMessage(content="tool output", tool_call_id="c0", id="t0"),
        AIMessage(content="latest answer", id="a1"),
        HumanMessage(content="newest question", id="h4"),
        AIMessage(content="final", id="a2"),
    ]
    out = await node(
        {"messages": msgs}, {"configurable": {"thread_id": "t-orphan"}}
    )
    from langchain_core.messages import RemoveMessage

    removes = [x for x in out["messages"] if isinstance(x, RemoveMessage)]
    removed_ids = {r.id for r in removes}
    # 摘要段恰为 h0..h3；a0/t0 配对保留在尾窗（不删其一）
    assert removed_ids == {"h0", "h1", "h2", "h3"}
    # a0 是尾窗首位（owner 领着 t0），不存在「t0 在窗、a0 在窗外」
    tail = [m for m in msgs if m.id not in removed_ids and m.type != "system"]
    assert tail[0].id == "a0"
    assert any(m.id == "t0" for m in tail)


@pytest.mark.asyncio
async def test_all_tool_chain_skips_compaction(monkeypatch):
    """切点回退到 0（整段一条工具链）→ old 为空直接放行，不动 state。"""
    import ginno_runtime.compaction as cmod
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import _compact_check_node_factory

    model = AsyncMock()
    node = _compact_check_node_factory(lambda: model)
    monkeypatch.setattr(cmod, "estimate_messages_tokens", lambda m: 900_000)
    monkeypatch.setattr(cmod, "_effective_compact_threshold", lambda s, sess: 500_000)
    monkeypatch.setattr(
        ws, "context_settings", lambda: _mid_settings(compact_mid_tail_messages=4)
    )
    # 非 system 5 条（keep_tail=4）→ cut=1 落在 t0 → 回退 0 → 放行
    c0 = {"name": "bash", "args": {}, "id": "c0", "type": "tool_call"}
    c1 = {"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}
    msgs = [
        AIMessage(content="", tool_calls=[c0], id="a0"),
        ToolMessage(content="o0", tool_call_id="c0", id="t0"),
        AIMessage(content="", tool_calls=[c1], id="a1"),
        ToolMessage(content="o1", tool_call_id="c1", id="t1"),
        AIMessage(content="tail", id="a2"),
    ]
    out = await node({"messages": msgs}, {"configurable": {"thread_id": "t-chain"}})
    assert out == {}
    model.ainvoke.assert_not_called()


# --------------------------------------------------------------------------- #
# 审计 B（2026-11-21 P0）：节流状态模块级持久 + 入口重置
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_throttle_persists_across_node_calls_and_resets(monkeypatch):
    """模块级 _MID_STATE：同 thread 连续 3 次超限，第 3 次被 ≤2 次/turn
    拦下；reset_mid_compact_state 归零后恢复压缩（ContextVar 版三步后
    计数仍 1，节流失效——已实测定罪）。"""
    import ginno_runtime.compaction as cmod
    import ginno_runtime.graph as gmod
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import _compact_check_node_factory

    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="SUM"))
    node = _compact_check_node_factory(lambda: model)
    monkeypatch.setattr(cmod, "estimate_messages_tokens", lambda m: 900_000)
    monkeypatch.setattr(cmod, "_effective_compact_threshold", lambda s, sess: 500_000)
    monkeypatch.setattr(ws, "context_settings", lambda: _mid_settings())
    cfg = {"configurable": {"thread_id": "t-throttle"}}

    gmod.reset_mid_compact_state("t-throttle")
    msgs = _msgs(20, 6)
    for m in msgs:
        m.id = f"m-{id(m)}"
    await node({"messages": msgs}, cfg)
    await node({"messages": msgs}, cfg)
    # 两次后：计数 2、软限抬升 1.5625 —— 跨调用持久（模块 dict）
    assert gmod._MID_STATE["t-throttle"] == (2, 1.25**2)
    out3 = await node({"messages": msgs}, cfg)
    assert out3 == {}  # 第 3 次放行兜底
    assert model.ainvoke.await_count == 2
    # turn 入口重置 → 恢复压缩
    gmod.reset_mid_compact_state("t-throttle")
    assert "t-throttle" not in gmod._MID_STATE
    await node({"messages": msgs}, cfg)
    assert gmod._MID_STATE["t-throttle"] == (1, 1.25)
    assert model.ainvoke.await_count == 3
    gmod.reset_mid_compact_state("t-throttle")


def test_reset_mid_compact_state_empty_id_is_noop():
    import ginno_runtime.graph as gmod

    gmod.reset_mid_compact_state("")  # 空 id no-op，不抛
    gmod.reset_mid_compact_state("never-exists")  # 未命中也 no-op
    assert "" not in gmod._MID_STATE


# --------------------------------------------------------------------------- #
# 审计 C（2026-11-21 P1）：事件路由 sid = thread_id
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_events_routed_by_thread_id(monkeypatch):
    """config 只有 thread_id（无 session_id）时事件必须带 thread_id 路由
    ——旧读法恒空 sid，事件全被 _push_session_event 早退丢掉。"""
    import ginno_runtime.compaction as cmod
    import ginno_runtime.server_shared as sshared
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import _compact_check_node_factory

    model = AsyncMock()
    model.ainvoke = AsyncMock(return_value=AIMessage(content="SUM"))
    node = _compact_check_node_factory(lambda: model)
    monkeypatch.setattr(cmod, "estimate_messages_tokens", lambda m: 900_000)
    monkeypatch.setattr(cmod, "_effective_compact_threshold", lambda s, sess: 500_000)
    monkeypatch.setattr(ws, "context_settings", lambda: _mid_settings())

    events: list[tuple] = []

    async def fake_push(sid, event, data, turn_id=None):
        events.append((sid, event, data))

    monkeypatch.setattr(sshared, "_push_session_event", fake_push)
    msgs = _msgs(10, 6)
    for m in msgs:
        m.id = f"m-{id(m)}"
    await node({"messages": msgs}, {"configurable": {"thread_id": "t-route"}})
    sids = {e[0] for e in events}
    stages = [e[2].get("stage") for e in events]
    assert sids == {"t-route"}  # 不是 ""，也不是 "s-mid" 之类的 session_id 读法
    assert "summarizing" in stages and "done" in stages


# --------------------------------------------------------------------------- #
# 审计 G②（2026-11-21 P3）：摘要 usage 记账 + 压后 context_baseline
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_midcompact_records_usage_and_baseline(monkeypatch):
    from langchain_core.messages import SystemMessage

    import ginno_runtime.compaction as cmod
    import ginno_runtime.usage_store as ustore
    import ginno_runtime.world_state as ws
    from ginno_runtime.graph import _compact_check_node_factory
    from ginno_runtime.server_shared import _SESSIONS, make_session_model_getter

    sid = "t-usage"
    old_model = AsyncMock()
    model = AsyncMock()
    session = {"model": old_model, "model_provider": "old", "model_name": "old"}
    monkeypatch.setitem(_SESSIONS, sid, session)
    node = _compact_check_node_factory(make_session_model_getter(sid, old_model))
    session.update(model=model, model_provider="current", model_name="summarizer")

    async def summarize(_messages):
        # In-flight requests are billed to the model they started with.
        session.update(model=old_model, model_provider="other", model_name="replacement")
        return AIMessage(
            content="SUM",
            usage_metadata={
                "input_tokens": 120,
                "output_tokens": 7,
                "total_tokens": 127,
            },
        )

    model.ainvoke = AsyncMock(side_effect=summarize)
    orig_est = cmod.estimate_messages_tokens
    msgs = (
        [SystemMessage(content="world facts")]
        + _msgs(14, 4)  # 非 system 18 条 > 默认尾窗 12，确保有摘要段
    )
    for m in msgs:
        m.id = f"m-{id(m)}"

    def fake_est(ms):
        # 只有阈值判定的整份输入报巨值；基线（protected+tail+summary）
        # 走真实估算，便于精确断言
        if len(ms) == len(msgs) and ms[0] is msgs[0]:
            return 900_000
        return orig_est(ms)

    monkeypatch.setattr(cmod, "estimate_messages_tokens", fake_est)
    monkeypatch.setattr(cmod, "_effective_compact_threshold", lambda s, sess: 500_000)
    monkeypatch.setattr(ws, "context_settings", lambda: _mid_settings())

    records: list[dict] = []
    monkeypatch.setattr(
        ustore, "record", lambda **kw: records.append(kw)
    )
    out = await node({"messages": msgs}, {"configurable": {"thread_id": "t-usage"}})

    from langchain_core.messages import RemoveMessage

    removes = [x for x in out["messages"] if isinstance(x, RemoveMessage)]
    removed = {r.id for r in removes}
    summary_msg = [x for x in out["messages"] if isinstance(x, HumanMessage)][0]
    tail = [m for m in msgs if m.type != "system" and m.id not in removed]
    protected = [m for m in msgs if m.type == "system"]
    expected_baseline = orig_est(protected + tail + [summary_msg])

    compaction_rows = [r for r in records if r["source"] == "compaction"]
    baseline_rows = [r for r in records if r["source"] == "context_baseline"]
    assert len(compaction_rows) == 1
    assert compaction_rows[0]["input_tokens"] == 120
    assert compaction_rows[0]["output_tokens"] == 7
    assert compaction_rows[0]["session_id"] == sid
    assert compaction_rows[0]["provider"] == "current"
    assert compaction_rows[0]["model"] == "summarizer"
    model.ainvoke.assert_awaited_once()
    old_model.ainvoke.assert_not_called()
    assert len(baseline_rows) == 1
    assert baseline_rows[0]["input_tokens"] == expected_baseline
    assert baseline_rows[0]["session_id"] == "t-usage"
