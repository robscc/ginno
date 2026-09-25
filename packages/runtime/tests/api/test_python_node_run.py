"""API-level run of a workflow containing `python` nodes (no LLM in the loop).

Proves the packaged server path: DSL with python nodes validates, the run
completes `done`, context_write events carry the deterministic output, and
the injected extract node takes the WRITE_JSON fast path (the scripted model
is consumed ONLY by the remaining agent step).
"""

from __future__ import annotations

import pytest

from ginno_runtime.api import workflows as _wf_api
from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import store as wf_store

pytestmark = pytest.mark.e2e


def _dsl() -> dict:
    return {
        "name": "PyNodeApiRun",
        "entry": "cmp",
        "context": {
            "schema": {"type": "object"},
            "initial": {
                "aliyun_records": [
                    {"usage_tokens": 2000, "original": 10, "discount": 4, "payable": 6}
                ],
                "volc_records": [
                    {"usage_tokens": 2000, "original": 10, "discount": 5, "payable": 5}
                ],
            },
        },
        "nodes": [
            {"id": "cmp", "type": "python", "entry": "normalize_and_compare",
             "args": {"model_name": "m", "aliyun_records": "{{context.aliyun_records}}",
                      "volc_records": "{{context.volc_records}}", "list_prices": {}},
             "writes": {"comparison_summary": {"type": "object"}}},
            {"id": "rep", "type": "step", "agent": "writer",
             "goal": "write {{context.comparison_summary}}"},
        ],
        "edges": [{"from": "cmp", "to": "rep"}],
    }


def test_run_with_python_node_completes(client, monkeypatch):
    wf = wf_store.create_def({"name": "PyNodeApiRun", "dsl": _dsl()})
    # exactly one model call: the writer step
    monkeypatch.setattr(_wf_api, "build_model", lambda *a, **k: ScriptedChatModel(
        scripts=[script(text="report done")]
    ))
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["ok"] is True and aw["run"]["status"] == "done", aw

    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    cw = [e for e in evs if e["kind"] == "context_write"]
    assert [c["method"] for c in cw] == ["python", "write_json"]
    enters = [e for e in evs if e["kind"] == "node_enter"]
    assert [e.get("node_type") for e in enters] == ["python", "extract", "step"]
    assert not [e for e in evs if e["kind"] == "error"]


def test_create_def_rejects_unknown_python_entry(client):
    bad = _dsl()
    bad["nodes"][0]["entry"] = "not_registered"
    # Since the Studio P2 fix the endpoint answers 400 with the store's
    # validation error instead of letting the ValueError escape (was: raises
    # through TestClient / 500 on a raw server).
    r = client.post("/api/workflows", json={"name": "Bad", "dsl": bad})
    assert r.status_code == 400
    assert "unknown entry 'not_registered'" in r.json()["detail"]
