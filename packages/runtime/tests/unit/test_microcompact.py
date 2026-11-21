"""Unit tests for microcompaction — stale tool-output clearing (rung below E3)."""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import tool

from ginno_runtime import jsonx as json
from ginno_runtime.microcompact import (
    _cleared_content,
    _clearable_positions,
    maybe_microcompact_history,
    rewrite_with_cleared,
)

pytestmark = pytest.mark.unit

BIG = "x" * 1200  # > default microcompact_min_chars (500)
HUGE = "y" * 3000  # > default microcompact_head_chars (1200)
HEAD_PREFIX = "[old tool output cleared, head kept]\n"


@tool
def echo(text: str) -> str:
    """Echo the input — output content is fully controlled by call args."""
    return text


def _graph_and_config(model, thread_id: str):
    from ginno_runtime.graph import build_graph

    graph = build_graph(
        model=model, project_slug="default", workspace="/tmp/ws", mcp_tools=[], all_tools=[echo]
    )
    config = {"configurable": {"thread_id": thread_id, "project_slug": "default"}}
    return graph, config


def _scripts(n_tool_turns: int, outputs: list[str]):
    """n_tool_turns × (tool-call + text reply), then one plain text turn."""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call

    scripts = []
    for i in range(n_tool_turns):
        scripts.append(script(tool_calls=[script_tool_call("echo", {"text": outputs[i]})]))
        scripts.append(script(text=f"done{i}"))
    scripts.append(script(text="final"))
    return ScriptedChatModel(scripts=scripts)


async def _seed_turns(graph, config, n: int):
    for i in range(n):
        await graph.ainvoke(
            {"messages": [HumanMessage(content=f"q{i}")], "project_slug": "default"}, config
        )


async def test_marker_text():
    # Two marker generations, one idempotency prefix (startswith, not ==).
    assert "[old tool output cleared]".startswith("[old tool output")
    assert (HEAD_PREFIX + "abc").startswith("[old tool output")


def test_cleared_content_keeps_head_and_strips_trailing_whitespace():
    # cut inside the content → head excerpt only
    assert _cleared_content(HUGE, 1200) == HEAD_PREFIX + "y" * 1200
    # content shorter than head → kept whole
    assert _cleared_content("abcdef", 1200) == HEAD_PREFIX + "abcdef"
    # cut lands mid-whitespace → tail stripped, marker still prefix-recognizable
    assert _cleared_content("abc\n\n  ", 5) == HEAD_PREFIX + "abc"
    # empty content → marker alone (still starts with the prefix)
    assert _cleared_content("", 1200) == "[old tool output cleared, head kept]"


# --------------------------------------------------------------------------- #
# selection
# --------------------------------------------------------------------------- #
def test_clearable_positions_skip_small_and_cleared():
    call = {"name": "echo", "args": {}, "id": "c0", "type": "tool_call"}
    msgs = [
        HumanMessage(content="q0", id="h0"),
        AIMessage(content="", tool_calls=[call], id="a0"),
        ToolMessage(content=BIG, tool_call_id="c0", id="t0"),  # clearable
        ToolMessage(content="ok", tool_call_id="c0", id="t1"),  # too small
        # already cleared (head-kept marker, 1232 chars > min_chars) — the
        # prefix check, not the length check, is what keeps it idempotent
        ToolMessage(content=HEAD_PREFIX + BIG, tool_call_id="c0", id="t2"),
        HumanMessage(content="q1", id="h1"),  # split (keep_turns=1)
        AIMessage(content="", tool_calls=[dict(call, id="c1")], id="a1"),
        ToolMessage(content=BIG, tool_call_id="c1", id="t3"),  # kept window
    ]
    assert _clearable_positions(msgs, split=5, min_chars=500) == {2}


def test_rewrite_preserves_order_and_pairs():
    call = {"name": "echo", "args": {}, "id": "c0", "type": "tool_call"}
    msgs = [
        HumanMessage(content="q0", id="h0"),
        AIMessage(content="", tool_calls=[call], id="a0"),
        ToolMessage(content=BIG, tool_call_id="c0", name="echo", id="t0"),
    ]
    out = rewrite_with_cleared(msgs, {2})
    # RemoveMessage batch first, then the original order re-added
    assert len(out) == 6
    human, ai, tm = out[3], out[4], out[5]
    # ALL re-added messages get new ids (add_messages cancels same-id
    # RemoveMessage pairs → in-place update, no re-ordering)
    assert human.content == "q0" and human.id != "h0"
    assert ai.tool_calls == [call] and ai.id != "a0"
    assert tm.id != "t0"
    # marker + head excerpt (BIG is exactly 1200 chars → kept whole)
    assert tm.content == HEAD_PREFIX + BIG
    assert tm.tool_call_id == "c0" and tm.name == "echo"  # pairing survives


# --------------------------------------------------------------------------- #
# end-to-end (graph level, no server)
# --------------------------------------------------------------------------- #
async def test_old_tool_outputs_cleared_recent_kept(isolated_home):
    # 4 user turns: 3 with tool calls, then one plain. keep_turns default 3 →
    # only turn 0 lies outside the keep window. Turn 0's output is HUGE
    # (> head) so the clear actually frees chars.
    model = _scripts(3, [HUGE, BIG, BIG])
    graph, config = _graph_and_config(model, "mc-1")
    await _seed_turns(graph, config, 4)

    session = {"graph": graph, "project_slug": "default", "session_id": "mc-1"}
    stats = await maybe_microcompact_history(session, config)
    assert stats is not None
    assert stats["cleared_tool_outputs"] == 1
    assert stats["chars_freed"] > 0

    state = await graph.aget_state(config)  # reads back through the checkpointer
    msgs = state.values["messages"]
    tools = [m for m in msgs if isinstance(m, ToolMessage)]
    assert len(tools) == 3
    # the stale one: marker + head excerpt (only the first 1200 of HUGE)
    assert tools[0].content.startswith(HEAD_PREFIX)
    assert tools[0].content == HEAD_PREFIX + HUGE[:1200]
    assert tools[1].content == BIG and tools[2].content == BIG  # keep window intact
    # user turns untouched and in order
    humans = [m.content for m in msgs if isinstance(m, HumanMessage)]
    assert humans == ["q0", "q1", "q2", "q3"]
    # tool_call pairing intact for the cleared message
    ai_with_calls = [m for m in msgs if isinstance(m, AIMessage) and m.tool_calls]
    assert tools[0].tool_call_id == ai_with_calls[0].tool_calls[0]["id"]


async def test_second_run_is_noop(isolated_home):
    model = _scripts(3, [BIG, BIG, BIG])
    graph, config = _graph_and_config(model, "mc-2")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-2"}

    assert await maybe_microcompact_history(session, config) is not None
    # already-cleared markers are skipped → nothing left to do
    assert await maybe_microcompact_history(session, config) is None


async def test_keep_turns_override_widens_eligible_prefix(isolated_home):
    # keep_turns=1 → only the last user turn is kept; turns 0-2 (all three
    # tool outputs) become eligible.
    settings = {"context": {"compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = _scripts(3, [BIG, BIG, BIG])
    graph, config = _graph_and_config(model, "mc-3")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-3"}

    stats = await maybe_microcompact_history(session, config)
    assert stats is not None and stats["cleared_tool_outputs"] == 3


async def test_small_outputs_kept(isolated_home):
    model = _scripts(3, ["ok", "fine", BIG])
    graph, config = _graph_and_config(model, "mc-4")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-4"}

    # turn 0's "ok" is below microcompact_min_chars → nothing worth clearing
    assert await maybe_microcompact_history(session, config) is None
    state = await graph.aget_state(config)
    tools = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert tools[0].content == "ok"


async def test_min_chars_override(isolated_home):
    settings = {"context": {"microcompact_min_chars": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = _scripts(3, ["ok", "fine", BIG])
    graph, config = _graph_and_config(model, "mc-5")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-5"}

    stats = await maybe_microcompact_history(session, config)
    assert stats is not None and stats["cleared_tool_outputs"] == 1  # the "ok"


async def test_disabled_flag(isolated_home):
    settings = {"context": {"microcompact_enabled": False}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = _scripts(3, [BIG, BIG, BIG])
    graph, config = _graph_and_config(model, "mc-6")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-6"}
    assert await maybe_microcompact_history(session, config) is None


async def test_no_tool_history_is_noop(isolated_home):
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    model = ScriptedChatModel(scripts=[script(text=f"r{i}") for i in range(4)])
    graph, config = _graph_and_config(model, "mc-7")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-7"}
    assert await maybe_microcompact_history(session, config) is None


# --------------------------------------------------------------------------- #
# head-excerpt contract (P1: don't starve the E3 summarizer of raw material)
# --------------------------------------------------------------------------- #
async def test_long_output_keeps_head_excerpt(isolated_home):
    # 3000-char output, default head 1200 → marker + first 1200 chars, and
    # chars_freed accounts for the kept head (net, not gross).
    model = _scripts(3, [HUGE, BIG, BIG])
    graph, config = _graph_and_config(model, "mc-8")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-8"}

    stats = await maybe_microcompact_history(session, config)
    assert stats is not None
    assert stats["cleared_tool_outputs"] == 1
    state = await graph.aget_state(config)
    tools = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert tools[0].content == HEAD_PREFIX + HUGE[:1200]
    assert stats["chars_freed"] == len(HUGE) - len(tools[0].content)


async def test_short_output_untouched(isolated_home):
    # ≤ microcompact_min_chars stays cheap-to-keep state ("ok") — never
    # rewritten, not even into a marker + head.
    model = _scripts(3, ["ok", BIG, BIG])
    graph, config = _graph_and_config(model, "mc-9")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-9"}

    assert await maybe_microcompact_history(session, config) is None
    state = await graph.aget_state(config)
    tools = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert tools[0].content == "ok"


async def test_second_clear_idempotent_head_not_retruncated(isolated_home):
    # After the first clear the replacement is 1232 chars — still way above
    # min_chars (500). Only the marker prefix keeps a second run from
    # re-truncating the head excerpt (and double-wrapping the marker).
    model = _scripts(3, [HUGE, BIG, BIG])
    graph, config = _graph_and_config(model, "mc-10")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-10"}

    first = await maybe_microcompact_history(session, config)
    assert first is not None
    state = await graph.aget_state(config)
    cleared_once = [
        m.content
        for m in state.values["messages"]
        if isinstance(m, ToolMessage) and m.content.startswith("[old tool output")
    ]
    assert cleared_once == [HEAD_PREFIX + HUGE[:1200]]

    assert await maybe_microcompact_history(session, config) is None
    state = await graph.aget_state(config)
    cleared_twice = [
        m.content
        for m in state.values["messages"]
        if isinstance(m, ToolMessage) and m.content.startswith("[old tool output")
    ]
    assert cleared_twice == cleared_once  # byte-identical, no re-processing


async def test_head_chars_override(isolated_home, monkeypatch):
    # The setting key flows from context_settings() to the excerpt length.
    # (Patched at the module boundary: world_state's _CONTEXT_DEFAULTS
    # registry entry for the key is wired separately from this module.)
    import ginno_runtime.microcompact as mc

    monkeypatch.setattr(
        mc, "context_settings", lambda: {"microcompact_head_chars": 50}
    )
    model = _scripts(3, [HUGE, BIG, BIG])
    graph, config = _graph_and_config(model, "mc-11")
    await _seed_turns(graph, config, 4)
    session = {"graph": graph, "project_slug": "default", "session_id": "mc-11"}

    stats = await maybe_microcompact_history(session, config)
    assert stats is not None
    state = await graph.aget_state(config)
    tools = [m for m in state.values["messages"] if isinstance(m, ToolMessage)]
    assert tools[0].content == HEAD_PREFIX + HUGE[:50]
