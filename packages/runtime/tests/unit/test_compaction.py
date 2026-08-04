"""Unit tests for history compaction (plan E3/E4)."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime.compaction import _copy_with_new_id, find_split_index, maybe_compact_history
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

    ai = AIMessage(content="x", tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}], id="oldai")
    ai2 = _copy_with_new_id(ai)
    assert ai2.tool_calls == ai.tool_calls and ai2.id != "oldai"

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


async def test_compaction_respects_disabled_flag(isolated_home):
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compaction_enabled": False, "compact_threshold_tokens": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(scripts=[script(text="r0")])
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": "comp-2", "project_slug": "default"}}
    await graph.ainvoke({"messages": [HumanMessage(content="hi")], "project_slug": "default"}, config)
    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-2"}
    assert await maybe_compact_history(session, config) is None


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
        await graph.ainvoke({"messages": [HumanMessage(content=q)], "project_slug": "default"}, config)

    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": "comp-3"}

    def factory():
        return SessionCtx(session_id="comp-3", project_slug="default", agent_id=None)

    stats = await maybe_compact_history(session, config, ctx_factory=factory)
    assert stats is not None
    assert stats["reinject"].startswith(REINJECT_MSG_PREFIX)
    assert "<environment>" in stats["reinject"]
