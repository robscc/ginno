"""Quick-fix D3: dry-run cycle warnings.

Cyclic DSLs are legal (branch back-edges are the intended way to build
redo/retry loops) and ``validate_dsl`` must keep accepting them — but nothing
bounds their iterations at run time except the recursion_limit. The dry-run
therefore reports ``cycles`` + a non-blocking ``graph.cycle`` warning instead
of an error, so drafts still save while the author sees the risk.
"""

from __future__ import annotations

from pathlib import Path

from ginno_runtime.workflows.dryrun import _unbounded_cycle_count, dry_run_dsl


def _dsl(nodes: list[dict], edges: list[dict], entry: str, **extra) -> dict:
    return {"name": "cycle-check", "entry": entry, "nodes": nodes, "edges": edges, **extra}


_LINEAR = _dsl(
    nodes=[
        {"id": "s1", "type": "step", "agent": "dev", "goal": "a"},
        {"id": "s2", "type": "step", "agent": "dev", "goal": "b"},
    ],
    edges=[{"from": "s1", "to": "s2"}],
    entry="s1",
)

# s1 → br; br routes back to s1 (case) or exits (default): the classic
# legal-but-unbounded branch back-edge cycle.
_BRANCH_BACK = _dsl(
    nodes=[
        {"id": "s1", "type": "step", "agent": "dev", "goal": "work"},
        {"id": "br", "type": "branch", "cases": [{"when": "context.redo", "then": "s1"}],
         "default": "end_step"},
        {"id": "end_step", "type": "step", "agent": "dev", "goal": "done"},
    ],
    edges=[{"from": "s1", "to": "br"}],
    entry="s1",
)

_LOOP = _dsl(
    nodes=[
        {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
         "body": "body", "max_iters": 5},
        {"id": "body", "type": "step", "agent": "dev", "goal": "process {{it}}"},
    ],
    edges=[],
    entry="lp",
    context={"initial": {"items": []}},
)


def test_linear_dsl_reports_zero_cycles(isolated_home: Path):
    r = dry_run_dsl(_LINEAR)
    assert r["ok"] is True, r
    assert r["cycles"] == 0
    assert not [w for w in r["warnings"] if w.get("rule") == "graph.cycle"]
    assert "recursion_limit" not in r  # nothing unbounded → no budget to report


def test_branch_back_edge_still_passes_and_warns(isolated_home: Path):
    r = dry_run_dsl(_BRANCH_BACK)
    # NOT rejected: the cycle is legal DSL and the draft still saves/runs.
    assert r["ok"] is True, r
    assert r["cycles"] == 1
    cyc = [w for w in r["warnings"] if w.get("rule") == "graph.cycle"]
    assert len(cyc) == 1
    assert f"recursion_limit={r['recursion_limit']}" in cyc[0]["message"]
    assert isinstance(r["recursion_limit"], int) and r["recursion_limit"] >= 25


def test_explicit_edge_cycle_also_counted(isolated_home: Path):
    d = _dsl(
        nodes=[
            {"id": "a", "type": "step", "agent": "dev", "goal": "a"},
            {"id": "b", "type": "step", "agent": "dev", "goal": "b"},
        ],
        edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "a"}],
        entry="a",
    )
    assert dry_run_dsl(d)["cycles"] == 1


def test_loop_structural_cycle_not_counted(isolated_home: Path):
    # head↔body is bounded by max_iters (already priced into the recursion
    # budget) — a plain loop must NOT trip the cycle warning.
    r = dry_run_dsl(_LOOP)
    assert r["ok"] is True, r
    assert r["cycles"] == 0
    assert not [w for w in r["warnings"] if w.get("rule") == "graph.cycle"]


def test_unreachable_cycle_is_not_counted():
    # Dead wiring cannot fire at run time; it is the `unreachable` field's
    # business, so the counter ignores it.
    d = _dsl(
        nodes=[
            {"id": "s1", "type": "step", "agent": "dev", "goal": "live"},
            {"id": "x", "type": "step", "agent": "dev", "goal": "dead"},
            {"id": "y", "type": "step", "agent": "dev", "goal": "dead"},
        ],
        edges=[{"from": "x", "to": "y"}, {"from": "y", "to": "x"}],
        entry="s1",
    )
    assert _unbounded_cycle_count(d) == 0
