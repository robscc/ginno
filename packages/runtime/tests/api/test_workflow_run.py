"""API test for the P2 workflow execution path (trigger + background run + events)."""

from __future__ import annotations

import pytest

from ginno_runtime import server
from ginno_runtime.testing.fake_model import script
from ginno_runtime.workflows import store

pytestmark = pytest.mark.api


def test_trigger_run_executes_and_writes_context(client, monkeypatch):
    # dev agent is seeded by the lifespan; the workflow's step references it.
    wf = store.create_def(
        {
            "name": "RunMe",
            "dsl": {
                "entry": "s1",
                "nodes": [
                    {"id": "s1", "type": "step", "agent": "dev", "goal": "compute"},
                    {"id": "s2", "type": "step", "agent": "dev", "goal": "finalize"},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            },
        }
    )
    wid = wf["id"]
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: __import__(
            "ginno_runtime.testing.fake_model", fromlist=["ScriptedChatModel"]
        ).ScriptedChatModel(
            scripts=[script(text='a\nWRITE_JSON {"x": 1}'), script(text='b\nWRITE_JSON {"y": 2}')]
        ),
    )

    r = client.post("/api/workflow_runs", json={"workflow_id": wid})
    assert r.status_code == 200
    run_id = r.json()["run"]["id"]

    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    assert aw["run"]["status"] == "done", {"await": aw, "events": evs}
    run_id = run_id  # keep

    kinds = [e["kind"] for e in evs]
    assert kinds.count("node_enter") == 2
    assert "done" in kinds
    written = {k for e in evs if e["kind"] == "context_write" for k in e["keys"]}
    assert written == {"x", "y"}


def test_trigger_run_404_for_unknown_workflow(client):
    assert client.post("/api/workflow_runs", json={"workflow_id": "nope"}).status_code == 404


def test_run_llm_usage_recorded_as_source_workflow(client, monkeypatch):
    """Every LLM call inside a workflow run lands in the global usage log with
    source=workflow (usage-stats-design §3.6), attributed to the run via
    turn_id; overview aggregates split it from chat usage."""
    from ginno_runtime import usage_store

    wf = store.create_def(
        {
            "name": "Metered",
            "dsl": {
                "entry": "s1",
                "nodes": [
                    {"id": "s1", "type": "step", "agent": "dev", "goal": "a"},
                    {"id": "s2", "type": "step", "agent": "dev", "goal": "b"},
                ],
                "edges": [{"from": "s1", "to": "s2"}],
            },
        }
    )
    usage = {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110,
             "input_token_details": {"cache_read": 40, "cache_creation": 0}}
    monkeypatch.setattr(
        "ginno_runtime.api.workflows.build_model",
        lambda *a, **k: __import__(
            "ginno_runtime.testing.fake_model", fromlist=["ScriptedChatModel"]
        ).ScriptedChatModel(scripts=[script(text="a", usage=usage), script(text="b", usage=usage)]),
    )

    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    assert aw["run"]["status"] == "done", aw

    # request log: one row per LLM call, tagged workflow, attributed to the run
    req = client.get("/api/usage/requests?source=workflow").json()
    assert req["total"] == 2, req
    for row in req["rows"]:
        assert row["source"] == "workflow"
        assert row["turn_id"] == run_id
        # langchain 1.x shape: input_tokens already whole-prompt, passes through
        assert row["input_tokens"] == 100
        assert row["output_tokens"] == 10
        assert row["cache_read_tokens"] == 40
    assert client.get("/api/usage/requests?source=chat").json()["total"] == 0

    # overview splits sources while totals stay whole-account
    ov = client.get("/api/usage/overview?days=7").json()
    srcs = {s["source"]: s for s in ov["sources"]}
    assert srcs["workflow"]["calls"] == 2
    assert srcs["workflow"]["input_tokens"] == 200
    assert ov["totals"]["input_tokens"] == 200

    # headless run (no present_in session) is not attributed to any session
    assert all(row["session_id"] is None for row in req["rows"])

    # node_exit events keep their per-step usage too
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    exits = [e for e in evs if e["kind"] == "node_exit" and e.get("usage")]
    assert len(exits) == 2
    assert all(e["usage"]["input_tokens"] == 100 for e in exits)

    usage_store.reset_cache()


class _BoomModel:
    """Engine-path failure: the step node's model raises on first invoke."""

    def bind_tools(self, *a, **k):
        return self

    async def ainvoke(self, *a, **k):
        raise RuntimeError("kaboom")


def test_failed_run_exposes_error_detail(client, monkeypatch):
    """An engine-level failure must expose structured localization data: the
    failing node id + a trimmed traceback, on the run record AND the events."""
    wf = store.create_def(
        {
            "name": "Boom",
            "dsl": {
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "x"}],
                "edges": [],
            },
        }
    )
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: _BoomModel())

    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    run = aw["run"]
    assert run["status"] == "failed", aw
    # one-line error semantics unchanged
    assert "kaboom" in (run.get("error") or "")
    # structured companion for localization
    detail = run.get("error_detail") or {}
    assert detail.get("node_id") == "s1"
    assert "kaboom" in (detail.get("traceback") or "")
    assert "Traceback" in (detail.get("traceback") or "")

    # GET /api/workflow_runs/{id} exposes the same field
    got = client.get(f"/api/workflow_runs/{run_id}").json()["run"]
    assert (got.get("error_detail") or {}).get("node_id") == "s1"

    # the persisted error event carries node_id + traceback too
    evs = client.get(f"/api/workflow_runs/{run_id}/events").json()["events"]
    err = next(e for e in evs if e["kind"] == "error")
    assert err.get("node_id") == "s1"
    assert "kaboom" in (err.get("traceback") or "")
    # incremental flush kept the failing node's footprint
    assert any(e["kind"] == "node_enter" and e.get("node_id") == "s1" for e in evs)


def test_dep_failure_error_detail_has_traceback_no_node(client, monkeypatch):
    """Driver-level failure (agent fork / model build) has no node — node_id is
    None but a traceback is still captured via _mark_run_failed."""
    wf = store.create_def(
        {
            "name": "Ghost",
            "dsl": {
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "ghost", "goal": "x"}],
                "edges": [],
            },
        }
    )
    run_id = client.post("/api/workflow_runs", json={"workflow_id": wf["id"]}).json()["run"]["id"]
    aw = client.post(f"/api/workflow_runs/{run_id}/_await").json()
    run = aw["run"]
    assert run["status"] == "failed", aw
    detail = run.get("error_detail") or {}
    assert detail.get("node_id") is None
    assert detail.get("traceback"), "driver failures must still capture a traceback"
    assert "Traceback" in detail["traceback"]


def test_run_failed_error_names_agent_and_fix_when_provider_unavailable(
    client, monkeypatch, isolated_home
):
    """2026-11-05 国际新闻工作流实录：节点未绑 agent → 回退 dev →
    dev.provider='' 且默认 provider 不可用 → 裸 "unknown provider: " 无从下手。
    修复：报错必须点名 workflow / 回退到的 agent / 修法（绑 agent 或配 provider）。
    """
    import ginno_runtime.api.workflows as wf_api
    import ginno_runtime

    # 无任何启用 provider：build_model 抛 unknown provider
    monkeypatch.setattr(
        "ginno_runtime.providers.get_default_provider", lambda: ""
    )
    monkeypatch.setattr(
        ginno_runtime.api.workflows, "build_model",
        lambda provider, *a, **k: (_ for _ in ()).throw(ValueError(f"unknown provider: {provider}")),
    )
    r = client.post(
        "/api/workflows",
        json={"name": "wf-noagent", "dsl": {
            "entry": "s1",
            "nodes": [{"id": "s1", "type": "step", "goal": "g"}],
            "edges": [],
        }},
    )
    assert r.status_code == 200
    wf_id = r.json()["workflow"]["id"]
    r2 = client.post("/api/workflow_runs", json={"workflow_id": wf_id})
    assert r2.status_code == 200
    run_id = r2.json()["run"]["id"]
    # bg driver marks the run failed with the actionable message（_await 收口）
    import time as _t
    from ginno_runtime.workflows import store as wf_store_mod

    for _ in range(50):
        row = wf_store_mod.get_run(run_id) or {}
        if row.get("status") == "failed":
            break
        _t.sleep(0.1)
    err = str(row.get("error") or "")
    assert "wf-noagent" in err
    assert "agent" in err
    assert "provider" in err
    assert "unknown provider" in err  # 原始错误保留
