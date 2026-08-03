"""Round A2: human node pauses the run (interrupt) and resume_workflow continues it."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import engine

pytestmark = pytest.mark.unit


def _dsl():
    return {
        "name": "h",
        "entry": "h",
        "nodes": [
            {"id": "h", "type": "human", "question": "approve?"},
            {"id": "s", "type": "step", "agent": "dev", "goal": "after"},
        ],
        "edges": [{"from": "h", "to": "s"}],
    }


@pytest.mark.asyncio
async def test_human_pauses_then_resume_continues():
    model = ScriptedChatModel(scripts=[script(text='done\nWRITE_JSON {"z": 1}')])
    events = []
    async for ev in engine.run_workflow(_dsl(), run_id="hr1", model=model, tools=[], project_slug="unit-a2"):
        events.append(ev)
    kinds = [e["kind"] for e in events]
    assert "interrupt" in kinds
    assert kinds[-1] == "paused"  # suspended, not an error

    # resume with a decision + context patch; the step after the human then runs
    events2 = []
    async for ev in engine.resume_workflow(
        _dsl(), run_id="hr1", model=model, tools=[], resume_value={"decision": "continue", "context_patch": {"ok": True}}, project_slug="unit-a2"
    ):
        events2.append(ev)
    kinds2 = [e["kind"] for e in events2]
    assert "resume" in kinds2
    assert "node_enter" in kinds2
    assert kinds2[-1] == "done"
    written = {k for e in events2 if e["kind"] == "context_write" for k in e["keys"]}
    assert written == {"z"}


@pytest.mark.asyncio
async def test_run_state_reports_paused():
    model = ScriptedChatModel(scripts=[script(text="x")])
    async for _ in engine.run_workflow(_dsl(), run_id="hr2", model=model, tools=[], project_slug="unit-a2"):
        pass
    st = await engine.run_state("hr2", _dsl(), model, [], project_slug="unit-a2")
    assert st["paused"] is True
