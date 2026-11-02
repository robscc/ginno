"""Workflow transcript sessions (workflows/transcript.py) — visibility and
one-per-run invariants.

2026-11-02 incident: an agent inside a SCHEDULED shadow session called the
workflow_run tool; the run failed (401) and a follow-up workflow_run_status
probe re-triggered the stream's adoption path off a STALE RUN_CACHE snapshot
("running" from creation time), spawning a second driver for the same run.
Each driver created its own transcript session → two type=workflow rows landed
in「我的对话」. Covered here:

* the transcript id is pinned on the run row and reused, never forked;
* terminal runs refuse re-drive (no second transcript, status untouched);
* transcripts triggered from hidden-kind sessions (scheduled shadow) carry
  parent_session_id so the frontend's transitive hiding keeps them out;
* scheduler-origin (headless) runs get type="scheduled" — hidden by class.
"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime.api.workflows_runs import _run_workflow_bg
from ginno_runtime.session_meta import _session_meta_list
from ginno_runtime.workflows import store
from ginno_runtime.workflows.transcript import begin_run_session

pytestmark = pytest.mark.api


def _step_wf() -> dict:
    return {
        "name": "Tx",
        "dsl": {
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "do it"}],
            "edges": [],
        },
    }


def _workflow_transcripts() -> list[dict]:
    return [m for m in _session_meta_list("default") if m.get("workflow_run_id")]


def _mk_session(client, **over) -> str:
    body = {"project_slug": "default", "workspace": "", "title": "s"}
    body.update(over)
    r = client.post("/api/sessions", json=body).json()
    assert r.get("ok") is not False, r
    return r["id"]


def test_run_pins_single_transcript_session(client, patch_build_model):
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text='done\nWRITE_JSON {"x": 1}')]))
    wf = store.create_def(_step_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw

    run = store.get_run(run_id) or {}
    assert run.get("transcript_session"), run
    tx = _workflow_transcripts()
    assert len(tx) == 1, tx
    assert tx[0]["id"] == run["transcript_session"]
    assert tx[0]["type"] == "workflow"
    assert not tx[0].get("parent_session_id")


def test_terminal_run_refuses_redrive_no_second_transcript(client, patch_build_model):
    """The incident shape: the run reaches a terminal state and a stale
    adoption re-spawns the driver. The terminal guard must refuse; the pinned
    transcript session is reused, never forked."""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text='done\nWRITE_JSON {"x": 1}')]))
    wf = store.create_def(_step_wf())
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw
    assert len(_workflow_transcripts()) == 1

    # Re-drive attempt (what the stale RUN_CACHE snapshot used to cause).
    asyncio.run(_run_workflow_bg(run_id, wf["id"], None, None))

    run = store.get_run(run_id) or {}
    assert run.get("status") == "done", run  # untouched, NOT re-driven
    assert len(_workflow_transcripts()) == 1, _workflow_transcripts()


def test_transcript_hangs_off_scheduled_trigger_session(client, patch_build_model):
    """A run triggered inside a scheduled shadow session stamps the shadow as
    parent → hiddenSessionIds' transitive propagation covers the transcript."""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    shadow = _mk_session(client, title="⏰ shadow", type="scheduled")

    wf = store.create_def(_step_wf())
    sid = asyncio.run(begin_run_session(wf, {"id": "r1"}, present_in=shadow))
    assert sid
    meta = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert meta["parent_session_id"] == shadow
    assert meta["type"] == "workflow"
    assert meta["workflow_run_id"] == "r1"


def test_transcript_normal_trigger_stays_top_level(client, patch_build_model):
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    normal = _mk_session(client, title="chat")

    wf = store.create_def(_step_wf())
    sid = asyncio.run(begin_run_session(wf, {"id": "r2"}, present_in=normal))
    assert sid
    meta = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert not meta.get("parent_session_id")
    assert meta["type"] == "workflow"


def test_scheduler_origin_transcript_is_scheduled_type(client, patch_build_model):
    """Headless scheduler-triggered runs (origin="schedule") have no trigger
    session to hang from — the transcript hides with the scheduled class."""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    wf = store.create_def(_step_wf())
    sid = asyncio.run(begin_run_session(wf, {"id": "r3", "origin": "schedule"}))
    assert sid
    meta = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert meta["type"] == "scheduled"
    assert meta["workflow_run_id"] == "r3"
