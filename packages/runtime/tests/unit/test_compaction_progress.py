"""压缩过程硬化（2026-11-16 P3/P4/P5）：

* P3 — 摘要期间的进度心跳：on_heartbeat 每 ``_HEARTBEAT_S`` 推一帧时间轴
  pct（有 predicted_s 时），单调且封顶 90（writing=92 由事件接管）。
* P5 — server_shared._COMPACTING 生命周期：摘要飞行中登记、结束（含
  abort 路径）必清；turn.state 探针据此保住前端「压缩中」镜像。
* P4 — microcompact 的持久痕迹：历史重放从 ``[old tool output`` placeholder
  特征重建一行系统行（live 的 context.microcompacted 只落前端内存 store，
  后台会话/断线场景此前完全无感知）。
"""

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
async def test_heartbeat_ticks_and_compacting_registry(isolated_home, monkeypatch):
    """P3+P5：慢摘要期间心跳按时间轴推进且 _COMPACTING 在册；结束必清。"""
    session, config, model = await _setup(isolated_home, "comp-hb")

    import ginno_runtime.compaction as C
    from ginno_runtime.server_shared import _COMPACTING

    monkeypatch.setattr(C, "_HEARTBEAT_S", 0.05)
    # 固定预估 10s → 心跳 pct = 8 + elapsed/10*82，帧间 0.05s → 每帧 ~+0.4%
    from ginno_runtime import compaction_stats
    monkeypatch.setattr(compaction_stats, "predict", lambda n: 10.0)

    real_agenerate = model._agenerate

    async def slow_agenerate(*a, **kw):
        if model._i >= 2:  # the summarizer is the 3rd scripted call
            await asyncio.sleep(0.22)
        return await real_agenerate(*a, **kw)

    monkeypatch.setattr(model, "_agenerate", slow_agenerate)

    ticks: list[int] = []
    registry_seen: list[bool] = []

    async def on_heartbeat(pct: int) -> None:
        ticks.append(pct)
        registry_seen.append(session["session_id"] in _COMPACTING)

    stats = await maybe_compact_history(
        session, config, on_heartbeat=on_heartbeat
    )
    assert stats is not None, "slow-but-successful summarize must still compact"
    assert len(ticks) >= 2, f"expected ≥2 heartbeat ticks in 0.22s, got {ticks}"
    assert ticks == sorted(ticks), f"pct must be monotonic, got {ticks}"
    assert all(p < 92 for p in ticks), "heartbeat caps below the writing stage"
    assert all(registry_seen), "_COMPACTING must hold the session while summarizing"
    assert session["session_id"] not in _COMPACTING, "registry cleared after compaction"


@pytest.mark.asyncio
async def test_compacting_registry_cleared_on_abort(isolated_home, monkeypatch):
    """P5：abort 路径也必须清 _COMPACTING（finally 兜底），不留脏注册。"""
    session, config, model = await _setup(isolated_home, "comp-hb-abort")

    import ginno_runtime.compaction as C
    from ginno_runtime.server_shared import _COMPACTING

    monkeypatch.setattr(C, "_HEARTBEAT_S", 0.05)
    from ginno_runtime import compaction_stats
    monkeypatch.setattr(compaction_stats, "predict", lambda n: 10.0)

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

    # 没有被清理的输出 → 不插行
    clean = _messages_to_ui(
        [HumanMessage(content="q1", id="m1"), AIMessage(content="a", id="m2")], "dev"
    )
    assert all(m["role"] != "system" or m["blocks"][0].get("i18n_key") != "chat.context.microcompacted" for m in clean)
