"""Repro: a HEADLESS workflow run (the kind TODO-sync triggers, present_in=None)
must surface in the client's Workflow panel immediately — not only after it
finishes.

The panel refreshes whenever the session WS receives ``workflows.changed``.
Previously that event was only pushed when a headless run *completed*, so a slow
todo-pull/todo-push run sat invisible in the Workflow tab for its whole life and
only materialised on some later refresh (which users perceived as "after I switch
sessions"). The fix announces headless runs at START as well.
"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import server
from ginno_runtime.api import sessions as _sessions_api
from ginno_runtime.api import workflows as _wf_api
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import engine as wf_engine
from ginno_runtime.workflows import store as wf_store

pytestmark = pytest.mark.e2e


def _patch(model):
    server.build_model = lambda *a, **k: model
    _sessions_api.build_model = lambda *a, **k: model
    _wf_api.build_model = lambda *a, **k: model


def _step_wf():
    return {
        "name": "HeadlessOneStep",
        "dsl": {
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "do it"}],
            "edges": [],
        },
    }


def test_headless_run_announced_at_start(client, create_session, ws_conv, monkeypatch):
    """A headless run must push workflows.changed while still RUNNING, so the
    panel can list it before it finishes."""
    sid = create_session(ScriptedChatModel(scripts=[script(text="hi")]))
    _patch(ScriptedChatModel(scripts=[script(text="hi")]))
    wf = wf_store.create_def(_step_wf())

    async def _hang(
        dsl, *, run_id, model, tools, context_override=None, project_slug="default", usage_attr=None
    ):
        await asyncio.sleep(30)  # keep the run "running" for the whole test
        yield {"run_id": run_id, "kind": "done"}

    monkeypatch.setattr(wf_engine, "run_workflow", _hang)

    run_id = None
    try:
        with ws_conv(sid) as conv:
            rr = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]})
            run_id = rr.json()["run"]["id"]
            assert rr.json()["run"]["present_in_session_id"] in (None, "")

            # The start-time announcement must arrive while the run is still running.
            saw = False
            for _ in range(30):
                ev = conv.recv()
                if ev.get("event") == "workflows.changed":
                    saw = True
                    break
            assert saw, "session WS never received workflows.changed at headless run START"

            # And the run is already listed (as running) for the panel fetch.
            run = next(r for r in client.get("/api/workflow_runs").json() if r["id"] == run_id)
            assert run["status"] == "running"
    finally:
        if run_id is not None:
            task = server._WF_RUN_TASKS.get(run_id)
            if task is not None and not task.done():
                task.cancel()


def test_headless_run_pushes_workflows_changed_on_completion(client, create_session, ws_conv):
    """The pre-existing completion push still fires, so the panel sees the terminal
    state too."""
    sid = create_session(ScriptedChatModel(scripts=[script(text="hi")]))
    _patch(ScriptedChatModel(scripts=[script(text='done\nWRITE_JSON {"x": 1}')]))
    wf = wf_store.create_def(_step_wf())

    with ws_conv(sid) as conv:
        rr = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]})
        run_id = rr.json()["run"]["id"]
        assert rr.json()["run"]["present_in_session_id"] in (None, "")

        aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
        assert aw["run"]["status"] == "done", aw

        saw = False
        for _ in range(30):
            try:
                ev = conv.recv()
            except Exception:
                break
            if ev.get("event") == "workflows.changed":
                saw = True
                break
        assert saw, "session WS never received workflows.changed for the headless run"

    ids = [r["id"] for r in client.get("/api/workflow_runs").json()]
    assert run_id in ids
