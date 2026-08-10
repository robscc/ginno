"""Round A2 API: run↔session binding, pause/human, decide(resume), cancel,
manual pause/resume (workflow-ux-redesign #14)."""

from __future__ import annotations

import time

import pytest
from langchain_core.tools import tool

from ginno_runtime import server
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call
from ginno_runtime.workflows import engine, store

pytestmark = pytest.mark.api


def _human_wf():
    return {
        "name": "Ctrl",
        "dsl": {
            "entry": "h",
            "nodes": [
                {"id": "h", "type": "human", "question": "approve?"},
                {"id": "s", "type": "step", "agent": "dev", "goal": "after"},
            ],
            "edges": [{"from": "h", "to": "s"}],
        },
    }


def _patch_model(monkeypatch, scripts=None):
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(
            scripts=scripts if scripts is not None else [script(text='done\nWRITE_JSON {"z": 1}')]
        ),
    )


def test_create_run_stores_session_binding(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    r = client.post(
        "/api/workflow_runs",
        json={"workflow_id": wf["id"], "session_id": "sess-1", "present_in_session_id": "sess-1"},
    )
    assert r.status_code == 200
    run = r.json()["run"]
    assert run["session_id"] == "sess-1"
    assert run["present_in_session_id"] == "sess-1"


def test_human_run_pauses_then_decide_resumes_to_done(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]

    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "paused", aw

    # resume is rejected unless paused? (it IS paused) -> decide continues it
    d = client.post(f"/api/workflow_runs/{run_id}/decide", json={"decision": "continue"})
    assert d.status_code == 200
    aw2 = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw2["run"]["status"] == "done", aw2
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    kinds = [e["kind"] for e in evs]
    assert "interrupt" in kinds and "resume" in kinds and "done" in kinds


def test_resume_non_paused_run_is_409(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(
        {"name": "np", "dsl": {"entry": "s", "nodes": [{"id": "s", "type": "step", "agent": "dev", "goal": "g"}], "edges": []}}
    )
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # completes -> done
    assert client.post(f"/api/workflow_runs/{run_id}/resume", json={}).status_code == 409


def test_cancel_paused_run_marks_cancelled(client, monkeypatch):
    _patch_model(monkeypatch)
    wf = store.create_def(_human_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # pauses at human
    c = client.post(f"/api/workflow_runs/{run_id}/cancel")
    assert c.status_code == 200
    assert store.get_run(run_id)["status"] == "cancelled"


# --- manual pause / resume (workflow-ux-redesign #14) ---


def _step_wf():
    return {
        "name": "Pause",
        "dsl": {
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "use the tool"}],
            "edges": [],
        },
    }


def _pause_trigger_tool(calls: dict):
    """Flags the live run for pause on its FIRST invocation (the call counter
    keeps the re-executed step after resume from pausing again)."""

    @tool
    def pause_trigger() -> str:
        """Request a manual pause of this run (test seam)."""
        calls["n"] += 1
        if calls["n"] == 1:
            rid = next(iter(engine._RUN_CONTROLS))
            engine.request_pause(rid)
        return "ok"

    return pause_trigger


def test_manual_pause_via_tool_then_resume_to_done(client, monkeypatch):
    """A pause flagged mid-step suspends the run (pending_interrupt.kind =
    manual); decide(resume) re-executes the rewound step and finishes."""
    calls = {"n": 0}
    _patch_model(monkeypatch, scripts=[
        script(tool_calls=[script_tool_call("pause_trigger")]),
        script(text="done"),
    ])
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_all_tools",
        lambda *a, **k: [_pause_trigger_tool(calls)],
    )
    wf = store.create_def(_step_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]

    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "paused", aw
    assert (aw["run"].get("pending_interrupt") or {}).get("kind") == "manual"
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    intr = next(e for e in evs if e["kind"] == "interrupt")
    assert intr.get("nature") == "manual" and intr.get("node_id") == "s1"
    assert "tool_call" in [e["kind"] for e in evs]  # paused mid-step, not at entry

    d = client.post(f"/api/workflow_runs/{run_id}/decide", json={"decision": "continue"})
    assert d.status_code == 200
    aw2 = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw2["run"]["status"] == "done", aw2
    evs2 = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    kinds2 = [e["kind"] for e in evs2]
    assert "resume" in kinds2 and "done" in kinds2
    # The resume driver re-builds the model, so the re-executed step replays
    # its scripted tool call once (calls=2); the counter kept it from
    # re-pausing — the rewind-then-rerun semantics of a mid-step pause.
    assert calls["n"] == 2


def test_pause_endpoint_on_live_run(client, monkeypatch):
    """POST /pause against a running run sets the flag; the step observes it
    and the run transitions to paused; decide resumes it to done."""
    started = {"on": False}

    @tool
    def wait_pause() -> str:
        """Block until a manual pause is requested (test seam)."""
        started["on"] = True
        for _ in range(500):
            if any(c.get("pause_requested") for c in engine._RUN_CONTROLS.values()):
                return "pause observed"
            time.sleep(0.02)
        return "timeout"

    _patch_model(monkeypatch, scripts=[
        script(tool_calls=[script_tool_call("wait_pause")]),
        script(text="done"),
    ])
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_all_tools", lambda *a, **k: [wait_pause]
    )
    wf = store.create_def(_step_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]

    # wait until the step's tool is actually blocking (run is live)
    for _ in range(500):
        if started["on"]:
            break
        time.sleep(0.02)
    assert started["on"], "the step never started its tool"

    p = client.post(f"/api/workflow_runs/{run_id}/pause")
    assert p.status_code == 200 and p.json()["status"] == "pausing"

    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "paused", aw
    assert (aw["run"].get("pending_interrupt") or {}).get("kind") == "manual"

    client.post(f"/api/workflow_runs/{run_id}/decide", json={"decision": "continue"})
    aw2 = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw2["run"]["status"] == "done", aw2


def test_pause_endpoint_guards(client, monkeypatch):
    _patch_model(monkeypatch)
    # unknown run
    assert client.post("/api/workflow_runs/nope/pause").status_code == 404
    # done run is not pausable
    wf = store.create_def(_step_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id}/_await")  # completes -> done
    assert client.post(f"/api/workflow_runs/{run_id}/pause").status_code == 409
    # already paused (human) run is not pausable
    wf2 = store.create_def(_human_wf())
    run_id2 = client.post("/api/workflow_runs", json={"workflow_id": wf2["id"]}).json()["run"]["id"]
    client.post(f"/api/workflow_runs/{run_id2}/_await")  # pauses at human
    assert client.post(f"/api/workflow_runs/{run_id2}/pause").status_code == 409
    # "running" record with no live execution loop (never spawned / crashed)
    orphan = store.create_run(store.create_def(_step_wf()))
    r = client.post(f"/api/workflow_runs/{orphan['id']}/pause")
    assert r.status_code == 409 and "live execution loop" in r.json()["detail"]
