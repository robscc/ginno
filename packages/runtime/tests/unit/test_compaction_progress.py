"""Compaction phases, stop cleanup, and the persisted microcompact marker."""

from __future__ import annotations

import asyncio
import json

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime.compaction import maybe_compact_history

pytestmark = pytest.mark.unit


def _make_parts(isolated_home, thread: str, threshold: int = 10):
    from ginno_runtime.graph import build_graph
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    settings = {"context": {"compact_threshold_tokens": threshold, "compact_keep_turns": 1}}
    (isolated_home / "settings.json").write_text(json.dumps(settings))
    model = ScriptedChatModel(scripts=[script(text="r0"), script(text="r1"), script(text="s")])
    graph = build_graph(model=model, project_slug="default", workspace="/tmp/ws")
    config = {"configurable": {"thread_id": thread, "project_slug": "default"}}
    session = {"graph": graph, "model": model, "project_slug": "default", "session_id": thread}
    return session, config, model


async def _setup(isolated_home, thread: str, threshold: int = 10):
    session, config, model = _make_parts(isolated_home, thread, threshold)
    for q in ("q1", "q2"):
        await session["graph"].ainvoke(
            {"messages": [HumanMessage(content=q)], "project_slug": "default"},
            config,
        )
    return session, config, model


@pytest.mark.asyncio
async def test_registry_reports_summary_and_write_phases(isolated_home, monkeypatch):
    from ginno_runtime import server_shared

    session, config, model = await _setup(isolated_home, "comp-phases")
    sid = session["session_id"]
    published = []

    async def push(session_id, event, payload, turn_id):
        assert session_id == sid and event == "context.compacting"
        published.append(payload)

    monkeypatch.setattr(server_shared, "_push_session_event", push)
    generate = model._agenerate
    write = session["graph"].aupdate_state

    async def summary(*args, **kwargs):
        assert server_shared._COMPACTING[sid]["stage"] == "summarizing"
        return await generate(*args, **kwargs)

    async def update(*args, **kwargs):
        assert server_shared._COMPACTING[sid]["stage"] == "writing"
        return await write(*args, **kwargs)

    monkeypatch.setattr(model, "_agenerate", summary)
    monkeypatch.setattr(session["graph"], "aupdate_state", update)
    assert await maybe_compact_history(session, config) is not None
    assert published == [{"stage": "summarizing"}, {"stage": "writing"}, {"stage": "done"}]
    assert sid not in server_shared._COMPACTING


@pytest.mark.asyncio
async def test_compacting_registry_cleared_on_abort(isolated_home, monkeypatch):
    """P5：abort 路径也必须清 _COMPACTING（finally 兜底），不留脏注册。"""
    session, config, model = await _setup(isolated_home, "comp-hb-abort")

    from ginno_runtime.server_shared import _COMPACTING

    hang = asyncio.Event()
    real_agenerate = model._agenerate

    async def hung_agenerate(*a, **kw):
        if model._i >= 2:
            await hang.wait()
        return await real_agenerate(*a, **kw)

    monkeypatch.setattr(model, "_agenerate", hung_agenerate)

    stop = asyncio.Event()

    async def fire_stop_later():
        await asyncio.sleep(0.05)
        stop.set()

    asyncio.get_running_loop().create_task(fire_stop_later())
    stats = await maybe_compact_history(session, config, abort_event=stop)
    hang.set()

    assert stats is None, "abort must return None (no rewrite)"
    assert session["session_id"] not in _COMPACTING, "registry cleared on abort path"


def test_history_replay_surfaces_microcompact_marker():
    """P4：重放视图从 placeholder 特征重建清理提示行。"""
    from ginno_runtime.api.messages_ui import _messages_to_ui
    from ginno_runtime.microcompact import CLEARED_TOOL_OUTPUT

    msgs = [
        HumanMessage(content="q1", id="m1"),
        AIMessage(
            content="",
            tool_calls=[{"name": "bash", "args": {}, "id": "c1", "type": "tool_call"}],
            id="m2",
        ),
        ToolMessage(content=CLEARED_TOOL_OUTPUT, tool_call_id="c1", name="bash", id="m3"),
        AIMessage(content="done", id="m4"),
    ]
    ui = _messages_to_ui(msgs, "dev")
    assert ui, "replay produced nothing"
    first = ui[0]
    assert first["role"] == "system"
    blk = first["blocks"][0]
    assert blk["kind"] == "context"
    assert blk["i18n_key"] == "chat.context.microcompacted"
    assert blk["params"]["n"] == 1
    assert "Cleared 1" in blk["text"]
    # Checkpoint rewrites reassign message IDs, but preserve tool-call IDs.
    rewritten = [*msgs[:2], msgs[2].model_copy(update={"id": "rewritten"}), msgs[3]]
    assert _messages_to_ui(rewritten, "dev")[0]["id"] == first["id"]
    other_clear = [*msgs[:2], msgs[2].model_copy(update={"tool_call_id": "c2"}), msgs[3]]
    assert _messages_to_ui(other_clear, "dev")[0]["id"] != first["id"]

    # 没有被清理的输出 → 不插行
    clean = _messages_to_ui(
        [HumanMessage(content="q1", id="m1"), AIMessage(content="a", id="m2")], "dev"
    )
    assert all(m["role"] != "system" or m["blocks"][0].get("i18n_key") != "chat.context.microcompacted" for m in clean)
