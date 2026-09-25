"""Long-step observability (2026-09-25 fix): engine events must reach
events.jsonl ≤1s even MID-superstep. A step node's whole agent loop is ONE
langgraph superstep, so events used to sit buffered until the chunk arrived —
during a multi-minute model call nothing showed running anywhere and users
cancelled live runs as stuck. Also: branch nodes now leave node_enter/exit
footprints (they used to run silently and stay "pending" forever)."""

from __future__ import annotations

import time

import pytest

import studio_lib as L

pytestmark = pytest.mark.e2e


def test_slow_step_shows_running_mid_execution(client, monkeypatch):
    """A step whose model call takes seconds must flip to running (event +
    step status) WHILE it executes — not only after the superstep ends."""
    from ginno_runtime.workflows.nodes import builtin as wf_builtin

    real = wf_builtin.llm_invoke_with_timeout

    async def slow(coro, timeout=None):
        await __import__("asyncio").sleep(2.5)
        return await real(coro, timeout)

    monkeypatch.setattr(
        "ginno_runtime.workflows.nodes.builtin.llm_invoke_with_timeout", slow
    )

    wf = L.make_wf(client, L.dsl_linear(2))
    L.patch_model(monkeypatch, ["one", "two"])
    rid = L.run_wf(client, wf["id"])

    deadline = time.time() + 6
    seen_running = False
    while time.time() < deadline:
        run = L.get_run(client, rid)
        evs = L.evs(client, rid)
        if run and run["status"] == "running" and evs:
            enters = {e.get("node_id") for e in evs if e.get("kind") == "node_enter"}
            steps = L.steps(run)
            if "s1" in enters and steps.get("s1") == "running":
                seen_running = True
                break
        time.sleep(0.3)
    assert seen_running, "mid-step: node_enter(s1) + step running never visible while executing"

    run = L.await_run(client, rid)
    assert run["status"] == "done", L.evs(client, rid)


def test_branch_node_leaves_event_footprint(client, monkeypatch):
    wf = L.make_wf(client, L.dsl_branch("go"))
    L.patch_model(monkeypatch, [L.wj({"flag": "go"}), "route a"])
    rid = L.run_wf(client, wf["id"])
    assert L.await_run(client, rid)["status"] == "done"
    evs = L.evs(client, rid)
    # the branch node itself enters/exits (route recorded on the exit event)
    assert "gate" in L.enters(evs)
    exit_ev = next(
        e for e in evs if e.get("kind") == "node_exit" and e.get("node_id") == "gate"
    )
    assert exit_ev.get("route") == "go_a"

def test_hung_tool_fails_node_and_run_recovers(client, monkeypatch):
    """无界工具（全盘遍历型）→ 有界 await → 节点失败带归属 → run=failed。
    2026-09-25 事件（glob_files 从 "/" 可达根全盘遍历）的机制回归。"""
    import asyncio as _aio
    import time as _t

    import langgraph.prebuilt as _prebuilt
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call
    from ginno_runtime.workflows.nodes import base as wf_base

    class _ToolCallModel(ScriptedChatModel):
        def __init__(self):
            super().__init__(scripts=[
                script(tool_calls=[script_tool_call("todo_list")]),
                script(text="done"),
            ])

    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model", lambda *a, **k: _ToolCallModel()
    )
    monkeypatch.setattr(wf_base, "WORKFLOW_TOOL_TIMEOUT_S", 2.0)

    async def stuck_invoke(self, inp, config=None, **kw):
        await _aio.sleep(9999)  # an unbounded tool (whole-fs walk / dead mount)

    monkeypatch.setattr(_prebuilt.ToolNode, "ainvoke", stuck_invoke)

    wf = L.make_wf(client, {
        "name": "HungTool",
        "dsl": {
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "step", "agent": "dev", "goal": "g"},
                {"id": "s2", "type": "llm", "prompt": "tail"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        },
    })
    rid = L.run_wf(client, wf["id"])
    deadline = _t.time() + 20
    while _t.time() < deadline:
        run = L.get_run(client, rid)
        if run and run["status"] == "failed":
            break
        _t.sleep(0.5)
    run = L.get_run(client, rid)
    assert run["status"] == "failed", run
    assert "tool batch timed out" in (run.get("error") or ""), run.get("error")
    assert (run.get("error_detail") or {}).get("node_id") == "s1"
    evs = L.evs(client, rid, kind="error")
    assert evs and "tool batch timed out" in (evs[-1].get("error") or "")
