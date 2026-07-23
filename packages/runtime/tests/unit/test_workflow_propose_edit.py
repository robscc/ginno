"""Unit tests for the workflow_propose_edit tool's apply/reject logic, with the
LangGraph interrupt() monkeypatched to a fixed decision (so we test the tool body
without a live graph/WS). The interrupt->event->WS path is covered by the e2e test."""

from __future__ import annotations

import json

import pytest

from ginno_runtime import workflows as wf_store
from ginno_runtime.tools import workflow_tools as wt

pytestmark = pytest.mark.unit


def _make_wf() -> str:
    wf = wf_store.create_def(
        {
            "name": "E",
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
    return wf["id"]


def _new_dsl_with_extra(cur: dict) -> dict:
    d = json.loads(json.dumps(cur))
    d["nodes"].append({"id": "s3", "type": "step", "agent": "dev", "goal": "c"})
    d["edges"].append({"from": "s2", "to": "s3"})
    return d


def test_propose_edit_apply_creates_version(isolated_home, monkeypatch):
    wid = _make_wf()
    new_dsl = _new_dsl_with_extra(wf_store.get_def(wid)["dsl"])
    monkeypatch.setattr(wt, "interrupt", lambda value: {"decision": "allow"})
    out = wt.workflow_propose_edit.invoke(
        {"workflow_id": wid, "new_dsl_json": json.dumps(new_dsl), "rationale": "add s3"}
    )
    assert "version 2" in out
    assert wf_store.get_def(wid)["version"] == 2
    assert len(wf_store.get_def(wid)["dsl"]["nodes"]) == 3


def test_propose_edit_reject_keeps_version(isolated_home, monkeypatch):
    wid = _make_wf()
    new_dsl = _new_dsl_with_extra(wf_store.get_def(wid)["dsl"])
    monkeypatch.setattr(wt, "interrupt", lambda value: {"decision": "deny"})
    out = wt.workflow_propose_edit.invoke(
        {"workflow_id": wid, "new_dsl_json": json.dumps(new_dsl), "rationale": "x"}
    )
    assert "rejected" in out
    assert wf_store.get_def(wid)["version"] == 1


def test_propose_edit_invalid_dsl_returns_error_no_interrupt(isolated_home, monkeypatch):
    wid = _make_wf()
    called = {"n": 0}

    def fake_interrupt(value):
        called["n"] += 1
        return {"decision": "allow"}

    monkeypatch.setattr(wt, "interrupt", fake_interrupt)
    bad = {"entry": "nope", "nodes": []}  # invalid: empty nodes + bad entry
    out = wt.workflow_propose_edit.invoke({"workflow_id": wid, "new_dsl_json": json.dumps(bad)})
    assert out.startswith("error")
    assert called["n"] == 0  # never paused on invalid input
    assert wf_store.get_def(wid)["version"] == 1
