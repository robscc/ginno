"""Stability plan P3 API test: a parallel-loop workflow run end-to-end through
the HTTP surface (create → run → _await → events), with the runtime gate on."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def test_parallel_loop_run_completes_with_arrays(client, monkeypatch):
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    wf = store.create_def(
        {
            "name": "ParallelRun",
            "dsl": {
                "entry": "lp",
                "context": {
                    "schema": {"type": "object"},
                    "initial": {"items": ["a", "b", "c"]},
                },
                "nodes": [
                    {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
                     "body": "b", "max_iters": 10, "parallel": True},
                    {"id": "b", "type": "step", "agent": "dev", "goal": "analyze {{it}}",
                     "writes": {"reports": {"type": "array", "items": {"type": "string"}}}},
                ],
                "edges": [],
            },
        }
    )
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: ScriptedChatModel(scripts=[
            script(text='WRITE_JSON {"reports": "r0"}'),
            script(text='WRITE_JSON {"reports": "r1"}'),
            script(text='WRITE_JSON {"reports": "r2"}'),
        ]),
    )
    r = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]})
    run_id = r.json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    assert aw["run"]["status"] == "done", {"await": aw, "events": evs}
    # Step table has the loop + body but NO body__extract (parallel inlines it).
    step_ids = {s["id"] for s in aw["run"]["steps"]}
    assert "b" in step_ids and "b__extract" not in step_ids
    # Per-item parallel loop_iter events are persisted + pushed.
    par_iters = [e for e in evs if e["kind"] == "loop_iter" and e.get("parallel")]
    assert sorted(e["index"] for e in par_iters) == [0, 1, 2]
    # The assembled array was committed to context.
    cw = [e for e in evs if e["kind"] == "context_write" and "reports" in e.get("keys", [])]
    assert cw
