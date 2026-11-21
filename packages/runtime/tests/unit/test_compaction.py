"""Unit tests for history compaction (plan E3/E4)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime import jsonx as json
from ginno_runtime.compaction import (
    _copy_with_new_id,
    _parse_summary_output,
    find_split_index,
    maybe_compact_history,
)
from ginno_runtime.world_state import REINJECT_MSG_PREFIX, SUMMARY_MSG_PREFIX

pytestmark = pytest.mark.unit


def _msgs(n_turns: int):
    out = []
    for i in range(n_turns):
        out.append(HumanMessage(content=f"q{i}", id=f"h{i}"))
        out.append(AIMessage(content=f"a{i}", id=f"ai{i}"))
    return out


# --------------------------------------------------------------------------- #
# split point
# --------------------------------------------------------------------------- #
def test_split_too_short_history():
    assert find_split_index(_msgs(2), keep_turns=3) == 0


def test_split_keeps_requested_turns():
    msgs = _msgs(5)
    split = find_split_index(msgs, keep_turns=2)
    # kept tail starts at the 4th user turn (index 6): q3 a3 q4 a4
    assert isinstance(msgs[split], HumanMessage)
    assert msgs[split].content == "q3"


def test_split_with_tool_messages_between():
    call = {"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}
    msgs = [
        HumanMessage(content="q0", id="h0"),
        AIMessage(content="", tool_calls=[call], id="ai0"),
        ToolMessage(content="out", tool_call_id="c1", id="t0"),
        HumanMessage(content="q1", id="h1"),
        AIMessage(content="a1", id="ai1"),
    ]
    split = find_split_index(msgs, keep_turns=1)
    assert msgs[split].content == "q1"


# --------------------------------------------------------------------------- #
# 审计 E（2026-11-21）：机器注入的 HumanMessage 不计用户轮
# --------------------------------------------------------------------------- #
def test_split_skips_machine_context_messages():
    """[turn context]/[world state update]/[conversation summary]/[compact]
    等系统脚手架不计入 keep_turns——切点落在真实用户消息上，保留窗口
    不被机器注入顶得前滑。"""
    from ginno_runtime.world_state import ALL_CONTEXT_PREFIXES, MID_COMPACT_PREFIX

    assert MID_COMPACT_PREFIX in ALL_CONTEXT_PREFIXES  # 新前缀已登记
    msgs = [
        HumanMessage(content="[turn context]\nDate: 2026-11-21"),
        HumanMessage(content="第一问"),
        AIMessage(content="答1"),
        HumanMessage(content="[world state update]\nworld changed"),
        HumanMessage(content="第二问"),
        AIMessage(content="答2"),
        HumanMessage(content="[conversation summary]\n此前摘要……"),
        HumanMessage(content=f"{MID_COMPACT_PREFIX} mid 压缩摘要……"),
        HumanMessage(content="第三问"),
    ]
    # 3 个真实用户轮、keep 2 → 切点应是「第二问」；旧实现会把
    # [compact] 摘要（倒数第二条 Human）当轮边界，窗口前滑一整轮。
    split = find_split_index(msgs, keep_turns=2)
    assert msgs[split].content == "第二问"
    # 全部是机器注入时无用户轮可切
    machine_only = [
        HumanMessage(content="[turn context]\nx"),
        HumanMessage(content=f"{MID_COMPACT_PREFIX} y"),
    ]
    assert find_split_index(machine_only, keep_turns=1) == 0


# --------------------------------------------------------------------------- #
# 审计 D（2026-11-21）：机器上下文消息豁免 1500 截断
# --------------------------------------------------------------------------- #
def test_msg_line_machine_context_exempt_from_1500_truncation():
    """二次压缩时旧 [conversation summary]（常 2-6k）不能被 1500 腰斩；
    普通消息照旧硬截。"""
    from ginno_runtime.compaction import _msg_line

    old_summary = "[conversation summary]\n" + "摘" * 5000
    line = _msg_line(HumanMessage(content=old_summary))
    assert "摘" * 4900 in line  # 全文带过，未截断
    compact = "[compact] 前段摘要\n" + "压" * 3000
    line2 = _msg_line(HumanMessage(content=compact))
    assert "压" * 2900 in line2
    plain = "x" * 5000
    line3 = _msg_line(HumanMessage(content=plain))
    assert len(line3) <= 1510 and line3.endswith("…")  # 普通消息维持 1500
    # 超过 20000 的机器消息仍截（防失控巨型注入）
    huge = "[compact] " + "z" * 30000
    line4 = _msg_line(HumanMessage(content=huge))
    assert len(line4) <= 20010 and line4.endswith("…")


# --------------------------------------------------------------------------- #
# copies
# --------------------------------------------------------------------------- #
def test_copy_with_new_id_preserves_content_and_changes_id():
    h = HumanMessage(content="hello", id="old")
    h2 = _copy_with_new_id(h)
    assert h2.content == "hello" and h2.id != "old"

    ai = AIMessage(
        content="x",
        tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}],
        id="oldai",
    )
    ai2 = _copy_with_new_id(ai)
    assert ai2.tool_calls == ai.tool_calls and ai2.id != "oldai"

    # additional_kwargs must survive: carries the agent_id attribution tag
    # (graph.py) — losing it would drop per-turn attribution after compaction.
    tagged = AIMessage(content="y", additional_kwargs={"agent_id": "research"}, id="tagged")
    tagged2 = _copy_with_new_id(tagged)
    assert tagged2.additional_kwargs.get("agent_id") == "research" and tagged2.id != "tagged"

    tm = ToolMessage(content="r", tool_call_id="c1", name="bash", id="oldt")
    tm2 = _copy_with_new_id(tm)
    assert tm2.tool_call_id == "c1" and tm2.id != "oldt"


# --------------------------------------------------------------------------- #
# end-to-end (graph level, no server)
# --------------------------------------------------------------------------- #
async def test_maybe_compact_rewrites_history(isolated_home):
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    # Tiny threshold so compaction fires immediately; keep the last turn only.
    settings = {"context": {"compact_threshold_tokens": 10, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))

    model = ScriptedChatModel(
        scripts=[
            script(text="r0"),
            script(text="r1"),
            # third script is consumed by the summarizer call
            script(text="这是前情摘要。"),
        ]
    )
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-1", "project_slug": "default"}}

    for q in ("第一个问题", "第二个问题"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )

    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-1"}
    stats = await maybe_compact_history(session, config, ctx_factory=None)
    assert stats is not None
    assert stats["compacted_messages"] >= 2
    assert stats["kept_messages"] >= 2
    assert stats["summary_chars"] > 0

    state = await graph.aget_state(config)
    contents = [getattr(m, "content", "") for m in state.values["messages"]]
    joined = "\n".join(str(c) for c in contents)
    assert SUMMARY_MSG_PREFIX in joined
    assert "这是前情摘要" in joined
    # kept tail survived, in order AFTER the summary
    idx_summary = next(i for i, c in enumerate(contents) if SUMMARY_MSG_PREFIX in str(c))
    idx_q1 = next(i for i, c in enumerate(contents) if "第二个问题" in str(c))
    assert idx_summary < idx_q1
    # the compacted prefix is gone
    assert "第一个问题" not in joined


async def test_force_compact_bypasses_threshold_and_disabled_flag(isolated_home):
    """/compact's force path: runs even with auto compaction OFF and an
    unreachable threshold (manual = explicit user intent)."""
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {
        "context": {
            "compaction_enabled": False,
            "compact_threshold_tokens": 999_999_999,
            "compact_keep_turns": 1,
        }
    }
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(
        scripts=[script(text="r0"), script(text="r1"), script(text="手动压缩摘要。")]
    )
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-force", "project_slug": "default"}}
    for q in ("问题一", "问题二"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )
    session = {
        "graph": graph,
        "model": model,
        "project_slug": "default",
        "session_id": "comp-force",
    }

    # auto path stays blocked (disabled flag + threshold)
    assert await maybe_compact_history(session, config) is None
    # force path compacts anyway
    stats = await maybe_compact_history(session, config, force=True)
    assert stats is not None
    assert stats["compacted_messages"] >= 2
    state = await graph.aget_state(config)
    joined = "\n".join(str(getattr(m, "content", "")) for m in state.values["messages"])
    assert "手动压缩摘要" in joined
    assert "问题一" not in joined  # compacted prefix is gone
    assert "问题二" in joined  # kept tail survives


async def test_compaction_summarizer_records_usage(isolated_home):
    """压缩摘要的模型调用必须记账（source="compaction"，会话归属）——
    记账点挂在 _stream_graph 消息流上，compaction 直连 model.ainvoke
    此前完全绕过（2026-10-12「压缩的也要算进去」）。"""
    from ginno_runtime import usage_store
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {
        "context": {
            "compaction_enabled": False,
            "compact_threshold_tokens": 999_999_999,
            "compact_keep_turns": 1,
        }
    }
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(
        scripts=[
            script(text="r0"),
            script(text="r1"),
            script(
                text="摘要。",
                usage={
                    "input_tokens": 54617,
                    "output_tokens": 206,
                    "total_tokens": 54823,  # AIMessage 的 usage_metadata 必填
                    "input_token_details": {"cache_read": 30000},
                },
            ),
        ]
    )
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-usage", "project_slug": "default"}}
    for q in ("问题一", "问题二"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )
    session = {
        "graph": graph,
        "model": model,
        "project_slug": "default",
        "session_id": "comp-usage",
    }
    assert await maybe_compact_history(session, config, force=True) is not None

    st = usage_store.session_totals("comp-usage")
    assert st is not None
    # 摘要调用的 tokens 落进了该会话的用量（graph 转身的两次调用来自
    # ScriptedChatModel 的默认 usage——只断言压缩那笔在场且字段正确）。
    assert st["input_tokens"] >= 54617
    assert st["output_tokens"] >= 206
    assert st["cache_read_tokens"] >= 30000
    rows = usage_store.query_requests(session_id="comp-usage", page_size=10)
    assert any(
        r.get("source") == "compaction"
        and r.get("input_tokens") == 54617
        for r in rows["rows"]
    )


async def test_compaction_respects_disabled_flag(isolated_home):
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compaction_enabled": False, "compact_threshold_tokens": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(scripts=[script(text="r0")])
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-2", "project_slug": "default"}}
    await graph.ainvoke(
        {"messages": [HumanMessage(content="hi")], "project_slug": "default"}, config
    )
    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-2"}
    assert await maybe_compact_history(session, config) is None


async def test_on_summary_start_brackets_the_summarizer_call(isolated_home):
    """on_summary_start fires exactly once per compaction that actually runs,
    and never on the cheap early-return paths (below threshold / disabled)."""
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compact_threshold_tokens": 999_999_999, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(scripts=[script(text="r0"), script(text="r1"), script(text="s")])
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-cb", "project_slug": "default"}}
    for q in ("q1", "q2"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )
    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-cb"}

    calls: list[str] = []

    async def on_start() -> None:
        calls.append("start")

    # Below threshold → no compaction, callback silent.
    assert await maybe_compact_history(session, config, on_summary_start=on_start) is None
    assert calls == []

    # force path compacts → callback fired exactly once (before the summary).
    stats = await maybe_compact_history(session, config, force=True, on_summary_start=on_start)
    assert stats is not None
    assert calls == ["start"]

    # A raising callback must not break compaction (status is best-effort).
    async def boom() -> None:
        raise RuntimeError("ui hiccup")

    # second run: fresh history is now short, force again to be sure
    stats2 = await maybe_compact_history(session, config, force=True, on_summary_start=boom)
    assert stats2 is None or stats2["summary_chars"] >= 0  # no exception escaped


async def test_stop_event_aborts_the_summarizer_call(isolated_home, monkeypatch):
    """⏹ during the summarizer LLM call aborts compaction promptly: the call
    is cancelled, nothing is rewritten, None returns (2026-10-27 session
    150bcced — stop waited out a minutes-long summary before taking effect)."""
    import asyncio

    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compact_threshold_tokens": 10, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(scripts=[script(text="r0"), script(text="r1"), script(text="s")])
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-abort", "project_slug": "default"}}
    for q in ("q1", "q2"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )
    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-abort"}

    # Summarizer "hangs" until the test fires the stop event.
    hang = asyncio.Event()
    real_agenerate = model._agenerate

    async def slow_agenerate(*a, **kw):
        idx = model._i  # capture which call this is
        if idx >= 2:  # r0=0, r1=1, the summarizer is the 3rd call
            await hang.wait()
        return await real_agenerate(*a, **kw)

    monkeypatch.setattr(model, "_agenerate", slow_agenerate)

    stop = asyncio.Event()

    async def fire_stop_later():
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.get_running_loop().create_task(fire_stop_later())

    t0 = asyncio.get_event_loop().time()
    stats = await maybe_compact_history(session, config, abort_event=stop)
    elapsed = asyncio.get_event_loop().time() - t0
    hang.set()  # release anything still waiting (should be nothing)

    assert stats is None  # aborted → no compaction, no rewrite
    assert elapsed < 3, "abort must not wait for the summarizer to finish"
    # History untouched: still 4 messages (2 turns × human+ai).
    state = await graph.aget_state(config)
    assert len(state.values["messages"]) == 4


async def test_stop_event_set_before_summary_skips_compaction(isolated_home):
    import asyncio

    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compact_threshold_tokens": 10, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(scripts=[script(text="r0"), script(text="r1")])
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-abort2", "project_slug": "default"}}
    for q in ("q1", "q2"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )
    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-abort2"}

    stop = asyncio.Event()
    stop.set()
    # force=True + pre-set event: even the manual path respects an armed stop
    # (defensive; turn path never passes force with an event).
    assert await maybe_compact_history(session, config, force=True, abort_event=stop) is None
    state = await graph.aget_state(config)
    assert len(state.values["messages"]) == 4


async def test_compaction_reinjects_world_state(isolated_home):
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script
    from ginno_runtime.world_state import SessionCtx

    settings = {"context": {"compact_threshold_tokens": 10, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(
        scripts=[script(text="r0"), script(text="r1"), script(text="摘要。")]
    )
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-3", "project_slug": "default"}}
    for q in ("q-one", "q-two"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )

    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-3"}

    def factory():
        return SessionCtx(session_id="comp-3", project_slug="default", agent_id=None)

    stats = await maybe_compact_history(session, config, ctx_factory=factory)
    assert stats is not None
    assert stats["reinject"].startswith(REINJECT_MSG_PREFIX)
    assert "<environment>" in stats["reinject"]


# --------------------------------------------------------------------------- #
# summary output parsing (structured template upgrade)
# --------------------------------------------------------------------------- #
def test_parse_summary_extracts_tag_and_drops_analysis():
    raw = "<analysis>private thinking</analysis>\n<summary>1. Primary Request: X</summary>"
    assert _parse_summary_output(raw) == "1. Primary Request: X"


def test_parse_summary_plain_text_fallback():
    # Models that ignore the requested format pass through unchanged.
    assert _parse_summary_output("plain summary") == "plain summary"


def test_parse_summary_strips_analysis_without_summary_tag():
    assert _parse_summary_output("<analysis>x</analysis>\nbody text") == "body text"


def test_parse_summary_empty():
    assert _parse_summary_output("") == ""


async def test_compact_strips_analysis_block_from_state(isolated_home):
    """The <analysis> draft never lands in the checkpoint — only the
    <summary> body does."""
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compact_threshold_tokens": 10, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))

    model = ScriptedChatModel(
        scripts=[
            script(text="r0"),
            script(text="r1"),
            script(text="<analysis>SECRET_TRACE</analysis>\n<summary>这是结构化摘要。</summary>"),
        ]
    )
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-parse", "project_slug": "default"}}
    for q in ("问题A", "问题B"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )

    session = {
        "graph": graph,
        "model": model,
        "project_slug": "default",
        "session_id": "comp-parse",
    }
    stats = await maybe_compact_history(session, config, ctx_factory=None)
    assert stats is not None and stats["summary_chars"] > 0

    state = await graph.aget_state(config)
    joined = "\n".join(str(getattr(m, "content", "")) for m in state.values["messages"])
    assert "这是结构化摘要。" in joined  # summary body landed
    assert "SECRET_TRACE" not in joined  # analysis discarded
    assert "<analysis>" not in joined and "<summary>" not in joined


def test_effective_threshold_follows_model_window(isolated_home):
    """窗口 < 默认阈值时收紧到窗口×0.75（会话 1a0c1d77 死锁：254k/500k
    不压、前端占比 127%）。显式 compact_threshold_tokens 永远是上限。"""
    import json

    from ginno_runtime.compaction import _effective_compact_threshold
    from ginno_runtime.paths import settings_path

    settings_path().write_text(
        json.dumps(
            {
                "default_provider": "custom",
                "providers": {
                    "custom": {
                        "enabled": True,
                        "protocol": "openai-compatible",
                        "api_key": "k",
                        "base_url": "http://127.0.0.1:1",
                        "model": "test-model",
                        "context_window": 200000,
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    assert _effective_compact_threshold({}) == 150000  # 200k × 0.75
    assert _effective_compact_threshold({"compact_threshold_tokens": 600000}) == 150000
    assert _effective_compact_threshold({"compact_threshold_tokens": 100000}) == 100000


def test_effective_threshold_unknown_window_keeps_default(isolated_home):
    """窗口未知（0）时维持原行为——不误伤 1M 窗口模型。"""
    from ginno_runtime.compaction import _effective_compact_threshold

    assert _effective_compact_threshold({}) == 500000
# --------------------------------------------------------------------------- #
# threshold molecule: real usage over local estimate (2026-11-07, session
# 1a0c1d77 — ring showed 81% while the estimator sat ~20% under the line,
# so auto-compaction never fired and zero traces landed in sidecar.log).
# --------------------------------------------------------------------------- #
async def _tiny_graph(isolated_home, thread_id: str):
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    model = ScriptedChatModel(
        scripts=[script(text="r")] * 20 + [script(text="前情摘要。")]
    )
    _placeholder = (
    )
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": thread_id}}
    for q in ("q1", "q2", "q3"):
        await graph.ainvoke({"messages": [HumanMessage(content=q)]}, config)
    return {"graph": graph, "model": model, "session_id": thread_id}, config


def _threshold_settings(isolated_home, threshold: int = 3000) -> None:
    settings = {"context": {"compact_threshold_tokens": threshold, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))


async def test_threshold_uses_real_usage_molecule(isolated_home):
    """真实分子优先：估算远小于阈值，但最近一次对话调用的全量 prompt
    过线 → 触发（与 TopBar 圆环同口径）。"""
    _threshold_settings(isolated_home)  # threshold=3000: 高于估算、低于 usage 分子
    from ginno_runtime import usage_store

    session, config = await _tiny_graph(isolated_home, "mol-1")
    # 最近一次「对话调用」的全量 prompt 过线（fake 调用的小 input 在它之前）
    usage_store.record(
        input_tokens=5000, output_tokens=1, provider="p", model="m",
        source="chat", session_id="mol-1",
    )
    stats = await maybe_compact_history(session, config)
    assert stats is not None, "usage molecule (5000) must cross threshold=100 despite tiny estimate"


async def test_threshold_ignores_sideband_usage(isolated_home):
    """sideband 记录（压缩摘要等）不是会话上下文：只有它时按估算判断
    → 估算 < 阈值 → 不触发。"""
    _threshold_settings(isolated_home)  # threshold=3000: 高于估算、低于 usage 分子
    from ginno_runtime import usage_store

    usage_store.record(
        input_tokens=999_999, output_tokens=1, provider="p", model="m",
        source="compaction", session_id="mol-sb",
    )
    session, config = await _tiny_graph(isolated_home, "mol-sb")
    stats = await maybe_compact_history(session, config)
    assert stats is None, "sideband input must not count as the context molecule"


async def test_threshold_falls_back_to_estimate_without_usage(isolated_home):
    """无 usage 记录（新会话）时回退本地估算（原行为不回归）。"""
    _threshold_settings(isolated_home)  # threshold=3000: 高于估算、低于 usage 分子
    session, config = await _tiny_graph(isolated_home, "mol-est")
    # 估算（q1/q2/q3 + r 回复）远低于 100 → 不触发
    assert await maybe_compact_history(session, config) is None


def test_last_chat_input_tokens_skips_sideband(isolated_home):
    from ginno_runtime import usage_store
    from ginno_runtime.usage import last_chat_input_tokens

    import time as _time

    _t0 = _time.time()
    usage_store.record(
        input_tokens=1111, output_tokens=1, provider="p", model="m",
        source="chat", session_id="lcit-1", ts=_t0 - 1,
    )
    usage_store.record(
        input_tokens=999_999, output_tokens=1, provider="p", model="m",
        source="compaction", session_id="lcit-1", ts=_t0,
    )
    # chat 在 sideband 之前（ts 更早）→ 扫描窗口必须跨过 sideband 找到它
    assert last_chat_input_tokens("lcit-1") == 1111
    assert last_chat_input_tokens("lcit-none") is None


def test_effective_threshold_follows_session_model(isolated_home):
    """P1：阈值窗口按**会话**的模型配置解析，而非全局默认配置。"""
    from ginno_runtime.compaction import _effective_compact_threshold

    (isolated_home / "settings.json").write_text(
        json.dumps(
            {
                "default_config": "big",
                "model_configs": [
                    {"id": "big", "protocol": "openai-compatible", "context_window": 600000,
                     "models": ["m1"], "default_model": "m1"},
                    {"id": "small", "protocol": "openai-compatible", "context_window": 200000,
                     "models": ["m2"], "default_model": "m2"},
                ],
            }
        )
    )
    # 无 session 信息 → 全局默认（big 600k）→ 450k
    assert _effective_compact_threshold({}) == 450_000
    # 会话指定 small（200k）→ 150k——不再被全局默认的 600k 遮蔽
    assert (
        _effective_compact_threshold({}, {"model_provider": "small", "model_name": "m2"})
        == 150_000
    )


# --------------------------------------------------------------------------- #
# 写回竞态自愈（2026-11-21 会话 7978b…：summarizer 分钟级窗口内并发 turn
# 推进 checkpoint → 旧快照的 RemoveMessage 引用不存在 id → langgraph
# add_messages 严格删除校验抛 ValueError，压缩白跑）。
# --------------------------------------------------------------------------- #
async def _race_setup(isolated_home, thread_id: str, scripts: list):
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compact_threshold_tokens": 10, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(scripts=[script(text=t) for t in scripts])
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": thread_id, "project_slug": "default"}}
    for q in ("第一个问题", "第二个问题"):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"}, config
        )
    return graph, config, model


async def test_compact_write_race_retry_preserves_concurrent(isolated_home, monkeypatch):
    graph, config, model = await _race_setup(
        isolated_home, "comp-race-1", ["r0", "r1", "这是前情摘要。"]
    )
    orig = graph.aupdate_state
    calls: list[int] = []

    async def racing_update(cfg, values, as_node=None):
        calls.append(1)
        if len(calls) == 1:
            # 模拟并发 turn 在 summarizer 窗口内推进 checkpoint（追加新消息）
            await orig(
                {"configurable": dict(cfg["configurable"])},
                {"messages": [HumanMessage(content="并发回帖", id="race1")]},
                as_node="agent",
            )
            raise ValueError(
                "Attempting to delete a message with an ID that doesn't exist ('keep_x')"
            )
        return await orig(cfg, values, as_node=as_node)

    monkeypatch.setattr(graph, "aupdate_state", racing_update)
    session = {"graph": graph, "model": model, "project_slug": "default",
               "session_id": "comp-race-1"}
    stats = await maybe_compact_history(session, config, ctx_factory=None)
    assert stats is not None and stats["summary_chars"] > 0
    assert len(calls) == 2  # 首次竞态炸 + 交集重试成功

    state = await graph.aget_state(config)
    msgs = state.values["messages"]
    # 审计 F（2026-11-21）：重试批次改为「全删 + 依序重建」——落盘顺序
    # [summary] → keep 副本 → 并发消息副本（并发消息被复制换新 id 追加到
    # 尾部，原 id 不复存在）；旧实现的 [并发消息, summary, keep] 时序倒置
    # 已废除。
    contents = [str(getattr(m, "content", "")) for m in msgs]
    summary_pos = next(
        i for i, c in enumerate(contents) if SUMMARY_MSG_PREFIX in c
    )
    keep_pos = next(i for i, c in enumerate(contents) if "第二个问题" in c)
    race_pos = next(i for i, c in enumerate(contents) if "并发回帖" in c)
    assert summary_pos < keep_pos < race_pos  # 新时序：摘要在最前
    assert sum(1 for c in contents if SUMMARY_MSG_PREFIX in c) == 1
    assert "第一个问题" not in "\n".join(contents)  # 旧前缀仍被删净
    assert "第二个问题" in "\n".join(contents)  # 尾窗保留


async def test_compact_write_race_already_applied_skips_retry(isolated_home, monkeypatch):
    graph, config, model = await _race_setup(
        isolated_home, "comp-race-2", ["r0", "r1", "这是前情摘要。"]
    )
    orig = graph.aupdate_state
    calls: list[int] = []

    async def applied_then_raise(cfg, values, as_node=None):
        calls.append(1)
        ret = await orig(cfg, values, as_node=as_node)  # 批次真实落盘
        raise ValueError("branch-route reapply boom")  # 收尾路由阶段才炸

    monkeypatch.setattr(graph, "aupdate_state", applied_then_raise)
    session = {"graph": graph, "model": model, "project_slug": "default",
               "session_id": "comp-race-2"}
    stats = await maybe_compact_history(session, config, ctx_factory=None)
    assert stats is not None  # 已生效 → 按成功返回
    assert len(calls) == 1  # 绝不重放（重放会叠加重复摘要）

    state = await graph.aget_state(config)
    msgs = state.values["messages"]
    assert sum(1 for m in msgs if SUMMARY_MSG_PREFIX in str(m.content)) == 1


async def test_compact_records_context_baseline(isolated_home):
    from ginno_runtime.usage import last_chat_input_tokens
    from ginno_runtime import usage_store

    graph, config, model = await _race_setup(
        isolated_home, "comp-base-1", ["r0", "r1", "这是前情摘要。"]
    )
    session = {"graph": graph, "model": model, "project_slug": "default",
               "session_id": "comp-base-1"}
    stats = await maybe_compact_history(session, config, ctx_factory=None)
    assert stats is not None

    rows = usage_store.query_requests(session_id="comp-base-1", page=1, page_size=10)["rows"]
    base = [r for r in rows if r.get("source") == "context_baseline"]
    assert base, "压缩成功后必须落 context_baseline 基线"
    # usage 口径即时校正：last_chat_input_tokens 不再回读压缩前巨值
    assert last_chat_input_tokens("comp-base-1") == int(base[0]["input_tokens"]) > 0
