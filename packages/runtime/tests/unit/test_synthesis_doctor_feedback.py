"""Stability plan P1 tests: doctor dataflow feedback in the synthesis retry
loop, the node-contract catalog (single source of truth), and the doctor gate
in workflow_propose_edit.

_run_synthesis is exercised directly (no HTTP): it is a pure async helper, so
the retry/hint mechanics are unit-testable with a recording fake model.
"""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage

from ginno_runtime.tools import workflow_tools as wt

pytestmark = pytest.mark.unit


class _RecordingModel:
    """Returns scripted replies and records every ainvoke message list."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls: list[list] = []

    async def ainvoke(self, messages, *a, **k):
        self.calls.append(list(messages))
        if self.replies:
            return self.replies.pop(0)
        return AIMessage(content="")


# A DSL that is schema-valid but dataflow-dirty: the loop iterates
# context.items, which no upstream writes/initial ever produces
# (doctor rule loop.over.no_source — the 2026-08 incident class).
_DOCTOR_DIRTY_DSL = json.dumps(
    {
        "name": "dirty",
        "entry": "lp",
        "nodes": [
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
             "body": "body", "max_iters": 5},
            {"id": "body", "type": "step", "agent": "dev", "goal": "process {{it}}"},
        ],
        "edges": [],
    }
)

# The model's self-corrected answer: declare the missing source in
# context.initial so the doctor is satisfied.
_DOCTOR_CLEAN_DSL = json.dumps(
    {
        "name": "clean",
        "entry": "lp",
        "context": {"schema": {"type": "object"}, "initial": {"items": []}},
        "nodes": [
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
             "body": "body", "max_iters": 5},
            {"id": "body", "type": "step", "agent": "dev", "goal": "process {{it}}"},
        ],
        "edges": [],
    }
)


async def test_doctor_errors_are_fed_back_and_recovered(tmp_path):
    from ginno_runtime.api.workflows import _run_synthesis

    model = _RecordingModel(
        [AIMessage(content=_DOCTOR_DIRTY_DSL), AIMessage(content=_DOCTOR_CLEAN_DSL)]
    )
    result = await _run_synthesis("trace", model, tmp_path)
    assert result["ok"] is True, result
    assert result["fail_stage"] is None
    assert len(model.calls) == 2
    # The second attempt's hint carries the dataflow findings.
    hint = model.calls[1][1].content
    assert "dataflow" in hint
    assert "loop.over.no_source" in hint


async def test_doctor_errors_exhaust_attempts_fail_with_label(tmp_path):
    from ginno_runtime.api.workflows import _run_synthesis

    model = _RecordingModel([AIMessage(content=_DOCTOR_DIRTY_DSL)] * 5)
    result = await _run_synthesis("trace", model, tmp_path)
    assert result["ok"] is False
    assert len(model.calls) == 3  # bounded like the validate-error loop
    assert result["fail_stage"] == "doctor.loop.over.no_source"
    # errors must not be empty for the dirty-but-schema-valid case — the
    # endpoint surfaces them verbatim to the UI.
    assert result["errors"]
    assert any("loop.over.no_source" in e for e in result["errors"])
    # attempts.jsonl recorded the doctor findings per attempt.
    lines = [
        json.loads(ln)
        for ln in (tmp_path / "attempts.jsonl").read_text(encoding="utf-8").splitlines()
        if ln.strip()
    ]
    assert len(lines) == 3
    assert lines[0].get("doctor_errors")
    assert lines[0]["parse"] == "ok"  # schema-valid; dirty on dataflow


async def test_doctor_warnings_only_pass_through(tmp_path):
    from ginno_runtime.api.workflows import _run_synthesis

    # writes declared but never consumed downstream → doctor WARNING only
    # (writes.unused); it must not trigger a retry, just ride along.
    warn_dsl = json.dumps(
        {
            "name": "w",
            "entry": "s1",
            "nodes": [
                {"id": "s1", "type": "step", "agent": "research", "goal": "list PRs",
                 "writes": {"report": {"type": "string"}}},
                {"id": "s2", "type": "step", "agent": "dev", "goal": "publish"},
            ],
            "edges": [{"from": "s1", "to": "s2"}],
        }
    )
    model = _RecordingModel([AIMessage(content=warn_dsl)])
    result = await _run_synthesis("trace", model, tmp_path)
    assert result["ok"] is True, result
    assert len(model.calls) == 1  # no retry for warnings
    rules = {w.get("rule") for w in result["doctor_warnings"]}
    assert "writes.unused" in rules


# --------------------------------------------------------------------------- #
# Node-contract catalog (P1b)
# --------------------------------------------------------------------------- #
def test_contracts_cover_registry_and_skip_internal():
    from ginno_runtime.workflows import contracts
    from ginno_runtime.workflows.nodes import registry

    public = {
        t for t in registry.known_types()
        if not getattr(registry.get_node(t), "_internal", False)
    }
    got = {c["type"] for c in contracts.node_contracts()}
    assert got == public
    assert "extract" not in got  # compiler-internal never taught to authors
    # The core authoring types are all present with rules or schema fields.
    for t in ("agent", "branch", "loop", "browser"):
        c = next(c for c in contracts.node_contracts() if c["type"] == t)
        assert c["rules"], f"{t} lost its structural rules"


def test_render_catalog_respects_budget_and_degrades():
    from ginno_runtime.workflows import contracts

    full = contracts.render_catalog(char_budget=100_000)
    for t in ("agent", "branch", "loop", "browser", "human", "llm", "pass"):
        assert t in full
    # Default budget is honored.
    assert len(contracts.render_catalog()) <= 2400
    # Degradation never drops the type listing, even under a budget smaller
    # than the minimal render (the contract floor is returned as-is): every
    # budget still yields all public types.
    tight = contracts.render_catalog(char_budget=10)
    for t in ("agent", "branch", "loop", "browser", "human", "llm", "pass"):
        assert t in tight
    # A generous budget keeps the floor within budget; shrinking never grows.
    assert len(contracts.render_catalog(char_budget=100_000, include_examples=False)) <= len(full)


# --------------------------------------------------------------------------- #
# propose_edit doctor gate (P1c)
# --------------------------------------------------------------------------- #
def _make_wf() -> str:
    from ginno_runtime import workflows as wf_store

    wf = wf_store.create_def(
        {
            "name": "E",
            "dsl": {
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "a"}],
                "edges": [],
            },
        }
    )
    return wf["id"]


def test_propose_edit_rejects_doctor_errors_without_interrupt(isolated_home, monkeypatch):
    wid = _make_wf()
    called = {"n": 0}

    def fake_interrupt(value):
        called["n"] += 1
        return {"decision": "allow"}

    monkeypatch.setattr(wt, "interrupt", fake_interrupt)
    out = wt.workflow_propose_edit.invoke(
        {"workflow_id": wid, "new_dsl_json": _DOCTOR_DIRTY_DSL, "rationale": "bad"}
    )
    assert out.startswith("error")
    assert "dataflow" in out
    assert "loop.over.no_source" in out
    assert called["n"] == 0  # rejected BEFORE the human diff gate
    from ginno_runtime import workflows as wf_store

    assert wf_store.get_def(wid)["version"] == 1


def test_propose_edit_warnings_do_not_block(isolated_home, monkeypatch):
    wid = _make_wf()
    seen: dict = {}

    def fake_interrupt(value):
        seen.update(value)
        return {"decision": "allow"}

    monkeypatch.setattr(wt, "interrupt", fake_interrupt)
    warn_dsl = {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "step", "agent": "dev", "goal": "a",
             "writes": {"report": {"type": "string"}}},
        ],
        "edges": [],
    }
    out = wt.workflow_propose_edit.invoke(
        {"workflow_id": wid, "new_dsl_json": json.dumps(warn_dsl), "rationale": "w"}
    )
    assert "version 2" in out
    # warnings ride along on the interrupt payload for the diff dialog
    rules = {w.get("rule") for w in seen.get("doctor_warnings") or []}
    assert "writes.unused" in rules
