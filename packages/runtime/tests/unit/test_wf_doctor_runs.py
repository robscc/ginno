"""Workflow doctor: recent-run evidence (WF-02).

The static lint cannot see that a python entry's return shape disagrees with
its ``writes`` declaration — run cbfa7e2b23 (wiki-compile) failed exactly that
way while ``GET /api/workflows/{id}/doctor`` reported a clean bill of health.
"""

from __future__ import annotations

import pytest

from ginno_runtime.workflows.doctor import run_doctor

pytestmark = pytest.mark.unit

_DSL = {
    "entry": "inv",
    "nodes": [
        {"id": "inv", "type": "python", "entry": "kb_wiki_inventory",
         "writes": {"plan": {"type": "object"}}},
        {"id": "fin", "type": "agent", "agent": "dev",
         "goal": "use {{plan.files}}"},
    ],
    "edges": [{"from": "inv", "to": "fin"}],
}

_FAILED_PY = {
    "id": "r1", "status": "failed", "dsl_version": 1,
    "error": "RuntimeError: python entry 'kb_wiki_inventory' output violates "
             "writes schema: key 'plan' missing or null",
    "error_detail": {"node_id": "inv"},
}


def _rules(result, key="errors"):
    return {f["rule"] for f in result[key]}


def test_latest_failed_run_is_reported_against_its_entry():
    r = run_doctor(_DSL, runs=[_FAILED_PY], version=1)
    assert "python.writes_runtime_mismatch" in _rules(r)
    finding = next(f for f in r["errors"] if f["rule"] == "python.writes_runtime_mismatch")
    assert finding["node_id"] == "inv"
    assert "plan" in finding["message"]


def test_other_failure_reports_node():
    run = {
        "id": "r2", "status": "failed", "dsl_version": 4,
        "error": "Extraction failed (source step trash_spam): key 'trashed_count' missing",
        "error_detail": {"node_id": "trash_spam"},
    }
    r = run_doctor(_DSL, runs=[run], version=4)
    assert "runs.latest_failed" in _rules(r)


def test_clean_latest_run_is_silent():
    runs = [
        {"id": "r3", "status": "done", "dsl_version": 1},
        {"id": "r4", "status": "failed", "dsl_version": 1,
         "error": "older failure", "error_detail": {"node_id": "inv"}},
    ]
    r = run_doctor(_DSL, runs=runs, version=1)
    assert "runs.latest_failed" not in _rules(r)
    assert "python.writes_runtime_mismatch" not in _rules(r)


def test_runs_from_older_versions_are_ignored():
    runs = [dict(_FAILED_PY, dsl_version=1)]
    r = run_doctor(_DSL, runs=runs, version=2)
    assert not _rules(r)


def test_high_failure_rate_warns():
    runs = [
        {"id": "a", "status": "failed", "dsl_version": 1, "error": "x"},
        {"id": "b", "status": "failed", "dsl_version": 1, "error": "y"},
    ]
    # latest failed is an error; the rate rule adds the warning as well
    r = run_doctor(_DSL, runs=runs, version=1)
    assert "runs.failure_rate" in _rules(r, "warnings")


def test_no_runs_keeps_static_behaviour():
    assert run_doctor(_DSL)["errors"] == []
