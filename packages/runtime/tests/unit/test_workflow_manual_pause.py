"""Manual pause / resume (workflow-ux-redesign #14): engine-level semantics.

A user-requested pause suspends the graph via the same interrupt()/checkpoint
mechanics as a human node — node-boundary pauses resume without re-execution,
mid-step (tool-iteration) pauses rewind the whole step.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call
from ginno_runtime.workflows import engine

pytestmark = pytest.mark.unit


def _two_llm_dsl():
    return {
        "name": "mp",
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "llm", "prompt": "a"},
            {"id": "s2", "type": "llm", "prompt": "b"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }


@pytest.mark.asyncio
async def test_pause_at_node_boundary_then_resume():
    """Pause requested after s1 completes → suspends BEFORE s2 starts; resume
    executes s2 (no re-run of s1)."""
    model = ScriptedChatModel(scripts=[script(text="a"), script(text="b")])
    agen = engine.run_workflow(
        _two_llm_dsl(), run_id="mp1", model=model, tools=[], project_slug="unit-mp"
    )
    events: list[dict] = []
    async for ev in agen:
        events.append(ev)
        if ev["kind"] == "node_exit" and ev.get("node_id") == "s1":
            assert engine.request_pause("mp1") is True
            break
    async for ev in agen:
        events.append(ev)
    kinds = [e["kind"] for e in events]
    intr = next(e for e in events if e["kind"] == "interrupt")
    assert intr["nature"] == "manual"
    assert intr["node_id"] == "s2"  # suspended before s2, after s1 committed
    assert kinds[-1] == "paused"
    assert "error" not in kinds

    events2: list[dict] = []
    async for ev in engine.resume_workflow(
        _two_llm_dsl(), run_id="mp1", model=model, tools=[],
        resume_value={"decision": "continue"}, project_slug="unit-mp",
        # the driver passes this from the run's pending_interrupt nature
        resume_nature="manual",
    ):
        events2.append(ev)
    kinds2 = [e["kind"] for e in events2]
    res = next(e for e in events2 if e["kind"] == "resume")
    assert res["nature"] == "manual" and res["node_id"] == "s2"
    assert kinds2[-1] == "done"
    entered = [e["node_id"] for e in events2 if e["kind"] == "node_enter"]
    assert entered == ["s2"]  # s1 was NOT re-executed


@pytest.mark.asyncio
async def test_pause_mid_step_rewinds_step_on_resume():
    """Pause flagged from inside a step's tool iteration → suspends mid-step;
    resume re-executes the step from scratch (checkpoint rewinds to the last
    committed superstep) and the cleared flag does not re-pause."""
    calls = {"n": 0}

    @tool
    def pause_trigger() -> str:
        """Request a manual pause of this run (test seam)."""
        calls["n"] += 1
        if calls["n"] == 1:
            rid = next(iter(engine._RUN_CONTROLS))
            engine.request_pause(rid)
        return "ok"

    dsl = {
        "name": "mpmid",
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "use the tool"}],
        "edges": [],
    }
    model = ScriptedChatModel(scripts=[
        script(tool_calls=[script_tool_call("pause_trigger")]),
        script(text="done"),
    ])
    events: list[dict] = []
    async for ev in engine.run_workflow(
        dsl, run_id="mp2", model=model, tools=[pause_trigger], project_slug="unit-mp"
    ):
        events.append(ev)
    kinds = [e["kind"] for e in events]
    assert "tool_call" in kinds and "tool_result" in kinds
    assert "node_exit" not in kinds  # the step never committed
    intr = next(e for e in events if e["kind"] == "interrupt")
    assert intr["nature"] == "manual" and intr["node_id"] == "s1"
    assert kinds[-1] == "paused"

    events2: list[dict] = []
    async for ev in engine.resume_workflow(
        dsl, run_id="mp2", model=model, tools=[pause_trigger],
        resume_value={"decision": "continue"}, project_slug="unit-mp",
        resume_nature="manual",
    ):
        events2.append(ev)
    kinds2 = [e["kind"] for e in events2]
    assert "resume" in kinds2  # engine-emitted manual resume event
    assert "node_enter" in kinds2  # the step re-executes from the start
    assert kinds2[-1] == "done"
    assert calls["n"] == 1  # the re-run took the text script, no re-pause


@pytest.mark.asyncio
async def test_request_pause_without_live_run_returns_false():
    assert engine.request_pause("nope") is False
    model = ScriptedChatModel(scripts=[script(text="a"), script(text="b")])
    async for _ in engine.run_workflow(
        _two_llm_dsl(), run_id="mp3", model=model, tools=[], project_slug="unit-mp"
    ):
        pass
    # the generator exhausted → its run_ctx was unregistered
    assert engine.request_pause("mp3") is False
    assert engine._RUN_CONTROLS == {}
