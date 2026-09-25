"""E2E: Studio run-list filters + the cross-workflow decision inbox (design B).

``GET /api/workflow_runs`` optional filters backing the left rail / inbox:

* ``workflow_id`` — exact match;
* ``status`` — single value or comma list, whitespace-tolerant, empty segments
  ignored (an all-empty value degrades to no filter);
* ``supervisor_pending=true`` — ONLY paused runs awaiting a human decision
  (``pending_interrupt.kind`` in human/supervisor); a MANUAL pause is
  deliberately excluded.

Legacy/corrupt run JSON rows never crash a filter — they just never match it.
Every test builds its own state in an isolated home (the autouse
``isolated_home`` fixture) and drives the REAL engine via scripted models.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from langchain_core.outputs import ChatGeneration, ChatResult

from ginno_runtime import paths
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_raise

import studio_lib as L

pytestmark = pytest.mark.e2e

API = L.API


# --------------------------------------------------------------------------- #
# Local helpers
# --------------------------------------------------------------------------- #
def _runs(client, **params) -> list:
    """GET /api/workflow_runs with optional query params; assert 200, return rows."""
    r = client.get(f"{API}/workflow_runs", params=params or None)
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body, list), f"expected a bare JSON array, got: {type(body)}"
    return body


def _ids(rows: list) -> set:
    return {r["id"] for r in rows}


def _run_done(client, name: str) -> tuple[str, str]:
    """A 1-node run that completes (script turn "one"); returns (wf_id, run_id)."""
    wf = L.make_wf(client, L.dsl_linear(1, name=name))
    rid = L.run_wf(client, wf["id"])
    run = L.await_run(client, rid)
    assert run["status"] == "done", run
    return wf["id"], rid


def _run_gate_parked(client, name: str) -> tuple[str, str]:
    """A run parked at a supervisor gate (kind "supervisor"); returns (wf_id, run_id)."""
    wf = L.make_wf(client, L.gated(L.dsl_linear(2, name=name), after=["s1"]))
    rid = L.run_wf(client, wf["id"])
    run = L.await_run(client, rid)
    assert run["status"] == "paused", run
    assert (run.get("pending_interrupt") or {}).get("kind") == "supervisor", run
    return wf["id"], rid


def _run_human_parked(client, name: str) -> tuple[str, str]:
    """A run parked at a human node (kind "human"); returns (wf_id, run_id)."""
    wf = L.make_wf(client, L.dsl_with_human())
    rid = L.run_wf(client, wf["id"])
    run = L.await_run(client, rid)
    assert run["status"] == "paused", run
    assert (run.get("pending_interrupt") or {}).get("kind") == "human", run
    return wf["id"], rid


class _SlowScripted(ScriptedChatModel):
    """Scripted model that dawdles before answering — keeps a run RUNNING long
    enough for POST /pause to land mid-flight (deterministic manual pause)."""

    delay: float = 0.05

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        time.sleep(self.delay)
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        await asyncio.sleep(self.delay)
        return super()._agenerate(messages, stop=stop, run_manager=run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        await asyncio.sleep(self.delay)
        async for chunk in super()._astream(messages, stop=stop, run_manager=run_manager, **kwargs):
            yield chunk


def _patch_slow_model(monkeypatch, turns: list, delay: float = 0.05) -> None:
    msgs = [t if not isinstance(t, str) else script(text=t) for t in turns]

    def _factory(*a, **k):
        return _SlowScripted(scripts=list(msgs), delay=delay)

    monkeypatch.setattr("ginno_runtime.api.workflows.build_model", _factory)


def _run_manual_paused(client, monkeypatch, name: str) -> tuple[str, str]:
    """A 3-node run paused mid-flight via POST /pause (kind "manual").

    The slow model keeps the run alive; /pause is retried until the engine has
    a live execution loop registered. Returns (wf_id, run_id)."""
    _patch_slow_model(monkeypatch, ["one", "two", "three"])
    wf = L.make_wf(client, L.dsl_linear(3, name=name))
    rid = L.run_wf(client, wf["id"])
    r = client.post(f"{API}/workflow_runs/{rid}/pause")
    for _ in range(200):
        if r.status_code != 409:  # 409 = loop not live yet / transient
            break
        time.sleep(0.02)
        r = client.post(f"{API}/workflow_runs/{rid}/pause")
    assert r.status_code == 200, r.text
    run = L.await_run(client, rid)
    assert run["status"] == "paused", run
    assert (run.get("pending_interrupt") or {}).get("kind") == "manual", run
    return wf["id"], rid


def _write_legacy_row(run_id: str, doc: dict) -> None:
    d = paths.home() / "workflow_runs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.json").write_text(json.dumps(doc, ensure_ascii=False))


# --------------------------------------------------------------------------- #
# 1-9: workflow_id / status filters
# --------------------------------------------------------------------------- #
def test_01_no_filter_returns_every_run_as_bare_list(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", script_raise(ValueError("boom"))])
    wf_a, rid_a = _run_done(client, "L01a")
    wf_b = L.make_wf(client, L.dsl_linear(2, name="L01b"))
    rid_fail = L.run_wf(client, wf_b["id"])
    L.await_run(client, rid_fail)  # s2 raises → failed

    rows = _runs(client)
    assert _ids(rows) == {rid_a, rid_fail}


def test_02_workflow_id_exact_match(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    wf_a, rid_a = _run_done(client, "L02a")
    wf_b, rid_b = _run_done(client, "L02b")

    assert _ids(_runs(client, workflow_id=wf_a)) == {rid_a}
    assert _ids(_runs(client, workflow_id=wf_b)) == {rid_b}


def test_03_status_single_value_done(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", script_raise(ValueError("boom"))])
    _, rid_done = _run_done(client, "L03a")
    wf_b = L.make_wf(client, L.dsl_linear(2, name="L03b"))
    rid_fail = L.run_wf(client, wf_b["id"])
    L.await_run(client, rid_fail)

    rows = _runs(client, status="done")
    assert _ids(rows) == {rid_done}


def test_04_status_comma_list(client, monkeypatch):
    # one script: "one" then raise → 1-node run done, 2-node run failed
    L.patch_model(monkeypatch, ["one", script_raise(ValueError("boom"))])
    _, rid_done = _run_done(client, "L04a")
    wf_fail = L.make_wf(client, L.dsl_linear(2, name="L04b"))
    rid_fail = L.run_wf(client, wf_fail["id"])
    fail_run = L.await_run(client, rid_fail)
    assert fail_run["status"] == "failed", fail_run
    _, rid_paused = _run_gate_parked(client, "L04c")

    rows = _runs(client, status="running,paused")
    assert _ids(rows) == {rid_paused}  # running+paused classes; done/failed excluded
    assert rid_done not in _ids(rows) and rid_fail not in _ids(rows)


def test_05_status_whitespace_tolerant(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", script_raise(ValueError("boom"))])
    _, rid_done = _run_done(client, "L05a")
    wf_fail = L.make_wf(client, L.dsl_linear(2, name="L05b"))
    rid_fail = L.run_wf(client, wf_fail["id"])
    L.await_run(client, rid_fail)
    _, rid_paused = _run_gate_parked(client, "L05c")

    assert _ids(_runs(client, status=" done , paused ")) == {rid_done, rid_paused}


def test_06_status_empty_string_degrades_to_no_filter(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    _, rid_a = _run_done(client, "L06a")
    _, rid_b = _run_done(client, "L06b")

    assert _ids(_runs(client, status="")) == {rid_a, rid_b}


def test_07_status_all_empty_segments_no_filter(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    _, rid_a = _run_done(client, "L07a")
    _, rid_b = _run_done(client, "L07b")

    assert _ids(_runs(client, status=",,,,")) == {rid_a, rid_b}


def test_08_workflow_id_and_status_intersect(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    # wf_a gets BOTH a done run (park → decide continue) and a parked run
    wf_a = L.make_wf(client, L.gated(L.dsl_linear(2, name="L08a"), after=["s1"]))
    rid_a_done = L.run_wf(client, wf_a["id"])
    assert L.await_run(client, rid_a_done)["status"] == "paused"
    assert L.decide(client, rid_a_done, "continue").status_code == 200
    assert L.await_run(client, rid_a_done)["status"] == "done"
    rid_a_paused = L.run_wf(client, wf_a["id"])
    assert L.await_run(client, rid_a_paused)["status"] == "paused"
    _, rid_b_paused = _run_gate_parked(client, "L08b")

    # intersection: wf_a's done run only (its paused run and wf_b's excluded)
    assert _ids(_runs(client, workflow_id=wf_a["id"], status="done")) == {rid_a_done}
    assert _ids(_runs(client, workflow_id=wf_a["id"], status="paused")) == {rid_a_paused}


def test_09_workflow_id_matching_nothing(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    _run_done(client, "L09a")

    assert _runs(client, workflow_id="no-such-workflow") == []


# --------------------------------------------------------------------------- #
# 10-14, 18: the supervisor_pending decision inbox
# --------------------------------------------------------------------------- #
def test_10_inbox_lists_gate_parked_run(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    _, rid = _run_gate_parked(client, "L10")

    rows = _runs(client, supervisor_pending="true")
    mine = [r for r in rows if r["id"] == rid]
    assert len(mine) == 1
    assert (mine[0].get("pending_interrupt") or {}).get("kind") == "supervisor"


def test_11_inbox_includes_human_node_parks(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    _, rid = _run_human_parked(client, "L11")

    assert rid in _ids(_runs(client, supervisor_pending="true"))


def test_12_inbox_excludes_manual_pause(client, monkeypatch):
    wf_id, rid = _run_manual_paused(client, monkeypatch, "L12")
    # guard: the run really is paused with kind manual (no vacuous pass)
    run = L.get_run(client, rid)
    assert run["status"] == "paused", run
    assert (run.get("pending_interrupt") or {}).get("kind") == "manual", run

    rows = _runs(client, supervisor_pending="true")
    assert rid not in _ids(rows)


def test_13_inbox_excludes_terminal_and_running_runs(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "one", "two"])
    _, rid_done = _run_done(client, "L13done")
    _, rid_parked = _run_gate_parked(client, "L13park")

    rows = _runs(client, supervisor_pending="true")
    assert _ids(rows) == {rid_parked}
    assert rid_done not in _ids(rows)


def test_14_inbox_combined_with_workflow_id(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    wf_a, rid_a = _run_gate_parked(client, "L14a")
    wf_b, rid_b = _run_gate_parked(client, "L14b")

    rows = _runs(client, supervisor_pending="true", workflow_id=wf_a)
    assert _ids(rows) == {rid_a}
    rows_b = _runs(client, supervisor_pending="true", workflow_id=wf_b)
    assert _ids(rows_b) == {rid_b}


def test_18_inbox_drains_after_decision(client, monkeypatch):
    L.patch_model(monkeypatch, ["one", "two"])
    _, rid = _run_gate_parked(client, "L18")
    assert rid in _ids(_runs(client, supervisor_pending="true"))

    r = L.decide(client, rid, "continue")
    assert r.status_code == 200, r.text
    run = L.await_run(client, rid)
    assert run["status"] == "done", run

    assert rid not in _ids(_runs(client, supervisor_pending="true"))


# --------------------------------------------------------------------------- #
# 15: newest-first ordering
# --------------------------------------------------------------------------- #
def test_15_list_is_newest_first_by_started(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    wf = L.make_wf(client, L.dsl_linear(1, name="L15"))
    rid_a = L.run_wf(client, wf["id"])
    L.await_run(client, rid_a)
    time.sleep(0.05)
    rid_b = L.run_wf(client, wf["id"])
    L.await_run(client, rid_b)

    rows = _runs(client)
    assert [r["id"] for r in rows if r["id"] in (rid_a, rid_b)] == [rid_b, rid_a]
    started = {r["id"]: r["started"] for r in rows if r["id"] in (rid_a, rid_b)}
    assert started[rid_b] > started[rid_a]


# --------------------------------------------------------------------------- #
# 16-17: legacy / corrupt rows never crash a filter
# --------------------------------------------------------------------------- #
def test_16_legacy_row_missing_fields_never_matches_but_never_crashes(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    wf_a, rid_a = _run_done(client, "L16")
    _write_legacy_row("legacyrow", {"id": "legacyrow", "name": "old", "started": time.time()})

    # unfiltered: legacy row is listed alongside real runs
    rows = _runs(client)
    assert "legacyrow" in _ids(rows) and rid_a in _ids(rows)
    # workflow_id filter: excluded, no crash
    assert _ids(_runs(client, workflow_id=wf_a)) == {rid_a}
    # status filter: excluded, no crash
    assert _ids(_runs(client, status="done")) == {rid_a}
    # inbox filter: excluded, no crash
    assert _runs(client, supervisor_pending="true") == []


def test_17_corrupt_run_file_is_skipped(client, monkeypatch):
    L.patch_model(monkeypatch, ["one"])
    wf_a, rid_a = _run_done(client, "L17")
    d = paths.home() / "workflow_runs"
    d.mkdir(parents=True, exist_ok=True)
    (d / "corruptrow.json").write_text("not json", encoding="utf-8")

    assert _ids(_runs(client)) == {rid_a}
    assert _ids(_runs(client, workflow_id=wf_a)) == {rid_a}
    assert _ids(_runs(client, status="done")) == {rid_a}
    assert _runs(client, supervisor_pending="true") == []
