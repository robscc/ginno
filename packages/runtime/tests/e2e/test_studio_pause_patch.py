"""Studio e2e: manual pause + context_patch (方案B 阶段2).

Covers the manual-pause surface through the REAL API + engine (TestClient):
node-boundary parking via POST /pause, resume semantics, the P2 context_patch
fix (a patch sent with a manual resume must reach the resumed node's rendered
prompt — it used to be silently discarded), mid-step pauses via a tool seam,
and the endpoint guards / lifecycle bookkeeping around parking.

Determinism: pausing mid-run needs a LIVE run with node 1 still in flight.
``_recording_model`` builds a ScriptedChatModel whose FIRST model call signals
``started`` then blocks on ``go`` — off the event loop, via run_in_executor —
so the test thread can POST /pause while node 1 is mid-flight and the flag is
observed at node 2's entry (boundary pause, interrupt node_id = the NEXT node).
"""

from __future__ import annotations

import asyncio
import threading

import pytest
from langchain_core.tools import tool
from pydantic import PrivateAttr

import studio_lib as L
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_tool_call
from ginno_runtime.workflows import engine

pytestmark = pytest.mark.e2e


# --------------------------------------------------------------------------- #
# Model patching: recording + first-call gate
# --------------------------------------------------------------------------- #
def _recording_model(monkeypatch, turns: list, gate: tuple | None = None) -> list:
    """Patch ``api.workflows.build_model`` with a Recording ScriptedChatModel.

    Records every prompt it is invoked with (shared across the fresh instances
    each run/resume forks) and returns the list. ``gate=(started, go)``: the
    FIRST model call of the whole test sets ``started`` then blocks on ``go``
    inside an executor thread (never the event loop — the blocking must not
    starve the portal loop that serves POST /pause). Fresh instances after a
    resume are unarmed (the flag lives in the closure, not the instance).
    """
    msgs = [t if not isinstance(t, str) else script(text=t) for t in turns]
    prompts: list = []
    armed = {"on": gate is not None}

    class Recording(ScriptedChatModel):
        _prompts: list = PrivateAttr(default_factory=list)

        def __init__(self):
            super().__init__(scripts=list(msgs))
            self._prompts = prompts

        async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
            if armed["on"]:
                armed["on"] = False
                started, go = gate
                started.set()
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, go.wait, 15)
            return self._generate(messages, stop, run_manager, **kwargs)

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self._prompts.append("\n".join(str(getattr(m, "content", "")) for m in messages))
            return super()._generate(messages, stop, run_manager, **kwargs)

    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model", lambda *a, **k: Recording()
    )
    return prompts


def _park_at_boundary(client, monkeypatch, doc: dict, turns: list, pause_hook=None) -> tuple:
    """Deterministically park a run at a node boundary (before node 2).

    Gates node 1's model call, POSTs /pause while it is in flight (so the flag
    can only be observed at node 2's entry), releases the gate and awaits the
    parked run. Returns (run_id, recorded_prompts).
    """
    started, go = threading.Event(), threading.Event()
    prompts = _recording_model(monkeypatch, turns, gate=(started, go))
    wf = L.make_wf(client, doc)
    run_id = L.run_wf(client, wf["id"])
    assert started.wait(10), "node 1's model call never started"
    r = client.post(f"{L.API}/workflow_runs/{run_id}/pause")
    assert r.status_code == 200, r.text
    if pause_hook is not None:
        pause_hook()
    go.set()
    run = L.await_run(client, run_id)
    assert run["status"] == "paused", run
    assert (run.get("pending_interrupt") or {}).get("kind") == "manual"
    return run_id, prompts


def _pause_trigger_tool(calls: dict):
    """Flags the live run for pause on its FIRST invocation (the counter keeps
    the re-executed step after resume from pausing again) — the proven mid-step
    seam from tests/unit/test_workflow_manual_pause.py."""

    @tool
    def pause_trigger() -> str:
        """Request a manual pause of this run (test seam)."""
        calls["n"] += 1
        if calls["n"] == 1:
            rid = next(iter(engine._RUN_CONTROLS))
            engine.request_pause(rid)
        return "ok"

    return pause_trigger


def _step_doc(name: str, goal: str, context_initial: dict | None = None) -> dict:
    dsl: dict = {"entry": "s1", "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": goal}], "edges": []}
    if context_initial is not None:
        dsl["context"] = {"initial": context_initial}
    return {"name": name, "dsl": dsl}


# --------------------------------------------------------------------------- #
# 1-5: boundary pause, plain resume, the context_patch fix
# --------------------------------------------------------------------------- #
def test_01_boundary_pause_parks_before_next_node(client, monkeypatch):
    """Pause requested while s1 runs → parked at s2's entry: pending kind
    manual, interrupt event nature=manual attributed to the NEXT node s2."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    intrs = [e for e in L.evs(client, run_id) if e["kind"] == "interrupt"]
    assert len(intrs) == 1, intrs
    assert intrs[0].get("nature") == "manual"
    assert intrs[0].get("node_id") == "s2"


def test_02_plain_resume_executes_paused_node_exactly_once(client, monkeypatch):
    """resume {"decision":"continue"} → done; s2 ran once: enters == s1,s2,s3."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    r = L.resume(client, run_id, {"decision": "continue"})
    assert r.status_code == 200, r.text
    run = L.await_run(client, run_id)
    assert run["status"] == "done", run
    assert L.enters(L.evs(client, run_id)) == ["s1", "s2", "s3"]


def test_03_resume_context_patch_reaches_resumed_prompt(client, monkeypatch):
    """THE FIX: a context_patch on a manual resume merges into graph state —
    the paused node's rendered prompt shows the PATCHED value. Before the P2
    fix the patch was discarded and s2 rendered the stale (empty) value."""
    doc = L.dsl_template()  # s1 writes tag; s2 renders value={{context.tag}}
    run_id, prompts = _park_at_boundary(
        client, monkeypatch, doc, [L.wj({"tag": "seed"}), "s2 answer"]
    )
    r = L.resume(client, run_id, {"decision": "continue", "context_patch": {"tag": "zzz"}})
    assert r.status_code == 200, r.text
    assert L.await_run(client, run_id)["status"] == "done"
    assert any("value=zzz" in p for p in prompts), prompts


def test_04_patch_wins_over_extracted_value(client, monkeypatch):
    """s1's own write produced tag="old"; the resume patch {"tag":"new"} must
    overwrite it — s2 renders value=new, never value=old."""
    doc = L.dsl_template()
    run_id, prompts = _park_at_boundary(
        client, monkeypatch, doc, [L.wj({"tag": "old"}), "s2 answer"]
    )
    r = L.resume(client, run_id, {"decision": "continue", "context_patch": {"tag": "new"}})
    assert r.status_code == 200, r.text
    assert L.await_run(client, run_id)["status"] == "done"
    assert any("value=new" in p for p in prompts), prompts
    assert not any("value=old" in p for p in prompts), prompts


def test_05_non_dict_context_patch_does_not_crash(client, monkeypatch):
    """context_patch that is not a dict is tolerated: the replay branch ignores
    it (isinstance guard) and the run still completes."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(2), ["a", "b"])
    r = L.resume(client, run_id, {"decision": "continue", "context_patch": "not-a-dict"})
    assert r.status_code == 200, r.text
    assert L.await_run(client, run_id)["status"] == "done"


# --------------------------------------------------------------------------- #
# 6-7: mid-step pause (tool-iteration boundary) via the @tool seam
# --------------------------------------------------------------------------- #
def test_06_mid_step_pause_rewinds_step_on_resume(client, monkeypatch):
    """A pause flagged inside a step's tool iteration suspends mid-step
    (interrupt node_id = the step itself); resume re-executes the step from
    scratch — its node_enter appears again after the resume event."""
    calls = {"n": 0}
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_all_tools",
        lambda *a, **k: [_pause_trigger_tool(calls)],
    )
    L.patch_model(
        monkeypatch,
        [script(tool_calls=[script_tool_call("pause_trigger")]), script(text="done")],
    )
    wf = L.make_wf(client, _step_doc("Mid", "use the tool"))
    run_id = L.run_wf(client, wf["id"])
    run = L.await_run(client, run_id)
    assert run["status"] == "paused", run
    assert (run.get("pending_interrupt") or {}).get("kind") == "manual"
    evs = L.evs(client, run_id)
    intr = next(e for e in evs if e["kind"] == "interrupt")
    assert intr.get("nature") == "manual" and intr.get("node_id") == "s1"

    r = L.resume(client, run_id, {"decision": "continue"})
    assert r.status_code == 200, r.text
    assert L.await_run(client, run_id)["status"] == "done"
    evs2 = L.evs(client, run_id)
    res_idx = next(i for i, e in enumerate(evs2) if e["kind"] == "resume")
    post = evs2[res_idx + 1:]
    assert any(
        e["kind"] == "node_enter" and e.get("node_id") == "s1" for e in post
    ), L.kinds(post)


def test_07_mid_step_pause_patch_reaches_rerun_prompt(client, monkeypatch):
    """Mid-step pause + patch: the re-executed step's goal renders the PATCHED
    context — its recorded prompt contains "b" (seed was "a")."""
    calls = {"n": 0}
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_all_tools",
        lambda *a, **k: [_pause_trigger_tool(calls)],
    )
    prompts = _recording_model(
        monkeypatch,
        [script(tool_calls=[script_tool_call("pause_trigger")]), script(text="done")],
    )
    wf = L.make_wf(client, _step_doc("MidPatch", "work on {{context.tag}}", {"tag": "a"}))
    run_id = L.run_wf(client, wf["id"])
    assert L.await_run(client, run_id)["status"] == "paused"

    r = L.resume(client, run_id, {"decision": "continue", "context_patch": {"tag": "b"}})
    assert r.status_code == 200, r.text
    assert L.await_run(client, run_id)["status"] == "done"
    assert prompts and "a" in prompts[0], prompts  # seeded value rendered pre-pause
    assert any("b" in p for p in prompts[1:]), prompts  # patched value after resume


# --------------------------------------------------------------------------- #
# 8-14: endpoint guards
# --------------------------------------------------------------------------- #
def test_08_pause_on_paused_run_is_409(client, monkeypatch):
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(2), ["a", "b"])
    r = client.post(f"{L.API}/workflow_runs/{run_id}/pause")
    assert r.status_code == 409, r.text


def test_09_pause_unknown_run_is_404(client):
    assert client.post(f"{L.API}/workflow_runs/nope/pause").status_code == 404


def test_10_double_pause_still_parks_exactly_once(client, monkeypatch):
    """A second POST /pause while pausing returns 200 or 409 depending on
    timing — either way the run parks ONCE (a single interrupt manual event)."""
    started, go = threading.Event(), threading.Event()
    prompts = _recording_model(monkeypatch, ["a", "b", "c"], gate=(started, go))
    wf = L.make_wf(client, L.dsl_linear(3))
    run_id = L.run_wf(client, wf["id"])
    assert started.wait(10), "node 1's model call never started"
    r1 = client.post(f"{L.API}/workflow_runs/{run_id}/pause")
    assert r1.status_code == 200, r1.text
    r2 = client.post(f"{L.API}/workflow_runs/{run_id}/pause")
    assert r2.status_code in (200, 409), r2.text
    go.set()
    run = L.await_run(client, run_id)
    assert run["status"] == "paused", run
    intrs = [
        e for e in L.evs(client, run_id)
        if e["kind"] == "interrupt" and e.get("nature") == "manual"
    ]
    assert len(intrs) == 1, intrs


def test_11_double_resume_second_is_409(client, monkeypatch):
    """Resume parks→runs; once the run is no longer paused a second resume is
    a 409 (after _await the status is done, deterministically not paused)."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    assert L.resume(client, run_id, {"decision": "continue"}).status_code == 200
    assert L.await_run(client, run_id)["status"] == "done"
    r2 = L.resume(client, run_id, {"decision": "continue"})
    assert r2.status_code == 409, r2.text


def test_12_cancel_while_manual_paused_then_resume_is_409(client, monkeypatch):
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    c = client.post(f"{L.API}/workflow_runs/{run_id}/cancel")
    assert c.status_code == 200, c.text
    assert L.get_run(client, run_id)["status"] == "cancelled"
    r = L.resume(client, run_id, {"decision": "continue"})
    assert r.status_code == 409, r.text


def test_13_decide_endpoint_completes_a_manual_pause(client, monkeypatch):
    """The decision endpoint works on a manual pause too (decision itself is
    recorded nowhere for manual) — the run just completes."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    d = L.decide(client, run_id, "continue")
    assert d.status_code == 200, d.text
    assert L.await_run(client, run_id)["status"] == "done"


def test_14_pause_after_done_is_409(client, monkeypatch):
    """A finished run cannot be paused (nothing live to flag)."""
    L.patch_model(monkeypatch, ["a"])
    wf = L.make_wf(client, L.dsl_linear(1))
    run_id = L.run_wf(client, wf["id"])
    assert L.await_run(client, run_id)["status"] == "done"
    r = client.post(f"{L.API}/workflow_runs/{run_id}/pause")
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# 15-18: checkpoint survival, decision inbox, event audit, lifecycle
# --------------------------------------------------------------------------- #
def test_15_resume_completes_from_checkpoint_without_live_task(client, monkeypatch):
    """The parked run's task has already exited (_await returned) — resume
    must complete the run purely from its persisted checkpoint."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    r = L.resume(client, run_id, {"decision": "continue"})
    assert r.status_code == 200, r.text
    run = L.await_run(client, run_id)
    assert run["status"] == "done", run
    assert L.steps(run) and set(L.steps(run).values()) == {"done"}, L.steps(run)


def test_16_manual_pause_not_in_decision_inbox(client, monkeypatch):
    """supervisor_pending=true lists paused runs awaiting a HUMAN decision —
    a manual-paused run is deliberately excluded (a human-paused run is the
    positive control)."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(2), ["a", "b"])
    L.patch_model(monkeypatch, ["one"])
    hwf = L.make_wf(client, L.dsl_with_human())
    hrun = L.run_wf(client, hwf["id"])
    assert L.await_run(client, hrun)["status"] == "paused"
    assert (L.get_run(client, hrun).get("pending_interrupt") or {}).get("kind") == "human"

    listing = client.get(f"{L.API}/workflow_runs", params={"supervisor_pending": "true"})
    assert listing.status_code == 200, listing.text
    ids = [r["id"] for r in listing.json()]
    assert hrun in ids, ids  # positive control: the filter isn't just empty
    assert run_id not in ids, ids  # manual pause stays out of the inbox


def test_17_event_audit_order_on_resume(client, monkeypatch):
    """interrupt(manual) … paused … resume(manual) … node_enter(s2) …
    node_exit(s2) … done — in that relative order."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    assert L.resume(client, run_id, {"decision": "continue"}).status_code == 200
    assert L.await_run(client, run_id)["status"] == "done"
    evs = L.evs(client, run_id)

    def first(pred):
        return next(i for i, e in enumerate(evs) if pred(e))

    def last(pred):
        return max(i for i, e in enumerate(evs) if pred(e))

    i_intr = first(lambda e: e["kind"] == "interrupt" and e.get("nature") == "manual")
    i_paused = first(lambda e: e["kind"] == "paused")
    i_resume = first(lambda e: e["kind"] == "resume")
    i_enter = last(lambda e: e["kind"] == "node_enter" and e.get("node_id") == "s2")
    i_exit = last(lambda e: e["kind"] == "node_exit" and e.get("node_id") == "s2")
    i_done = first(lambda e: e["kind"] == "done")
    assert i_intr < i_paused < i_resume < i_enter < i_exit < i_done, L.kinds(evs)


def test_18_pending_interrupt_lifecycle(client, monkeypatch):
    """pending_interrupt is stamped kind=manual while parked and cleared (None)
    on the run JSON once the run is done."""
    run_id, _ = _park_at_boundary(client, monkeypatch, L.dsl_linear(3), ["a", "b", "c"])
    parked = L.get_run(client, run_id)
    assert (parked.get("pending_interrupt") or {}).get("kind") == "manual"

    assert L.resume(client, run_id, {"decision": "continue"}).status_code == 200
    done = L.await_run(client, run_id)
    assert done["status"] == "done", done
    assert not done.get("pending_interrupt"), done
