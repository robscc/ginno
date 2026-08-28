"""Stability plan P1d/P1a API tests: the zero-LLM dry-run endpoint and the
doctor_warnings pass-through on summarize-from-session."""

from __future__ import annotations

import json
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.session_meta import _session_meta_upsert as _upsert

pytestmark = pytest.mark.api

_GOOD_DSL = {
    "name": "PR Review",
    "entry": "s1",
    "nodes": [
        {"id": "s1", "type": "step", "agent": "research", "goal": "list PRs"},
        {"id": "s2", "type": "step", "agent": "dev", "goal": "review each"},
    ],
    "edges": [{"from": "s1", "to": "s2"}],
}


def test_dry_run_ok_on_clean_dsl(client):
    r = client.post("/api/workflows/dry-run", json={"dsl": _GOOD_DSL})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["errors"] == []
    assert body["doctor_errors"] == []
    assert body["unreachable"] == []
    assert body["node_count"] == 2


def test_dry_run_reports_schema_errors(client):
    bad = dict(_GOOD_DSL)
    bad["entry"] = "nope"
    r = client.post("/api/workflows/dry-run", json={"dsl": bad})
    body = r.json()
    assert body["ok"] is False
    assert body["errors"]
    assert body["doctor_errors"] == []  # doctor ran but schema errors win


def test_dry_run_reports_doctor_errors(client):
    dirty = {
        "name": "dirty",
        "entry": "lp",
        "nodes": [
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
             "body": "body", "max_iters": 5},
            {"id": "body", "type": "step", "agent": "dev", "goal": "process {{it}}"},
        ],
        "edges": [],
    }
    r = client.post("/api/workflows/dry-run", json={"dsl": dirty})
    body = r.json()
    assert body["ok"] is False
    assert body["errors"] == []  # schema-clean…
    rules = {e.get("rule") for e in body["doctor_errors"]}
    assert "loop.over.no_source" in rules  # …but dataflow-dirty


def test_dry_run_detects_unreachable_nodes(client):
    dsl = {
        "name": "orphan",
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "step", "goal": "a"},
            {"id": "s2", "type": "step", "goal": "b"},
            {"id": "s3", "type": "step", "goal": "orphan"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }
    r = client.post("/api/workflows/dry-run", json={"dsl": dsl})
    body = r.json()
    assert body["ok"] is True  # unreachable is advisory, not fatal
    assert body["unreachable"] == ["s3"]


def test_dry_run_requires_dsl_object(client):
    r = client.post("/api/workflows/dry-run", json={})
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# summarize: doctor warnings pass through on success (P1a)
# --------------------------------------------------------------------------- #
class _RecordingModel:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list] = []

    async def ainvoke(self, messages, *a, **k):
        self.calls.append(list(messages))
        if self.replies:
            return self.replies.pop(0)
        return AIMessage(content="")


def _seed_session(slug: str, sid: str) -> None:
    _upsert(slug, {"id": sid, "title": "synth", "agent_id": "dev"})
    cp = FileCheckpointer(slug)
    state = {
        "messages": [
            HumanMessage(content="list the open PRs and review each"),
            AIMessage(content="done"),
        ],
        "workspace": "/tmp",
        "project_slug": slug,
        "agent_id": "dev",
        "active_skills": [],
        "pending_tool_calls": [],
    }
    checkpoint = {"id": str(uuid.uuid4()), "channel_values": state, "pending_sends": []}
    cp.put({"configurable": {"thread_id": sid}}, checkpoint, {}, {})


async def test_synthesis_result_carries_doctor_warnings(client, monkeypatch):
    """Async synthesis contract (2026-08-29 merge): the endpoint only ACKs
    (started + synthesis_id); the doctor findings ride the synthesis result /
    finished-event instead of the HTTP response."""
    from ginno_runtime.api.workflows import _run_synthesis

    sid = "sess-dry-warn"
    _seed_session("default", sid)
    # writes declared but never consumed → writes.unused warning, no errors
    warn_dsl = json.dumps(
        {
            "name": "PR Review",
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "step", "agent": "research", "goal": "list PRs",
                 "writes": {"report": {"type": "string"}}},
                {"id": "s2", "type": "step", "agent": "dev", "goal": "review"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        }
    )
    model = _RecordingModel([AIMessage(content=warn_dsl)])
    result = await _run_synthesis("trace", model, None)
    assert result["ok"] is True, result
    rules = {w.get("rule") for w in result.get("doctor_warnings") or []}
    assert "writes.unused" in rules

    # The HTTP surface now answers "started" and defers to synthesis.event.
    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", lambda *a, **k: model)
    r = client.post("/api/workflows/summarize-from-session", json={"session_id": sid})
    body = r.json()
    assert body["ok"] is True and body.get("status") == "started", body
    assert body.get("synthesis_id")


async def test_synthesis_doctor_failure_reports_messages():
    from ginno_runtime.api.workflows import _run_synthesis, _synth_error_text

    dirty = json.dumps(
        {
            "name": "dirty",
            "entry": "lp",
            "nodes": [
                {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
                 "body": "body", "max_iters": 5},
                {"id": "body", "type": "step", "agent": "dev", "goal": "x {{it}}"},
            ],
            "edges": [],
        }
    )
    model = _RecordingModel([AIMessage(content=dirty)] * 5)
    result = await _run_synthesis("trace", model, None)
    assert result["ok"] is False
    assert result["fail_stage"] == "doctor.loop.over.no_source"
    # The finished-event error text carries the doctor findings (not an empty
    # suffix).
    assert "loop.over.no_source" in _synth_error_text(result)
    assert len(model.calls) == 3  # bounded self-correction loop
