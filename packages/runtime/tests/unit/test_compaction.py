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
