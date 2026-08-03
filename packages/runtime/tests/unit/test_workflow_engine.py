"""Tests for the workflow compiler + engine (P2). Engine runs the real compiled
LangGraph with a scripted fake model and NO tools (steps that only write context)."""

from __future__ import annotations

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import compiler, engine


def _dsl():
    return {
        "name": "w",
        "entry": "a",
        "nodes": [
            {"id": "a", "type": "step", "agent": "dev", "goal": "first"},
            {"id": "b", "type": "step", "agent": "dev", "goal": "second"},
        ],
        "edges": [{"from": "a", "to": "b"}],
    }


def test_compile_builds_graph_for_step_and_branch():
    d = dict(_dsl())
    d["nodes"].append(
        {"id": "br", "type": "branch", "cases": [{"when": "context.x", "then": "b"}], "default": "b"}
    )
    # branch routes via cases/default — NO explicit edge from br
    g = compiler.compile_workflow(d, ScriptedChatModel(scripts=[script(text="ok")]), [], {"run_id": "r", "events": []})
    assert g is not None


def test_compile_loop_with_structural_routing():
    d = {
        "name": "w",
        "entry": "a",
        "nodes": [
            {"id": "a", "type": "step", "goal": "first"},
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it", "body": "body", "max_iters": 5},
            {"id": "body", "type": "step", "goal": "per-item"},
        ],
        "edges": [{"from": "a", "to": "lp"}],  # body->lp is synthesized, not declared
    }
    g = compiler.compile_workflow(
        d, ScriptedChatModel(scripts=[script(text="ok")]), [], {"run_id": "r", "events": []}
    )
    assert g is not None


def test_validate_loop_explicit_edge_rules():
    from ginno_runtime.workflows import dsl as wf_dsl

    d = _dsl()
    d["nodes"].append({"id": "lp", "type": "loop", "over": "context.items", "body": "a", "max_iters": 5})
    d["nodes"].append({"id": "c", "type": "step", "goal": "x"})
    # a single explicit "done/next" out-edge is allowed (loop chaining)
    d["edges"].append({"from": "lp", "to": "b"})
    assert wf_dsl.validate_dsl(d) == []
    # an edge from the loop to its own body is structural -> rejected
    d2 = dict(d)
    d2["edges"] = d["edges"] + [{"from": "lp", "to": "a"}]
    assert any("body" in e for e in wf_dsl.validate_dsl(d2))
    # more than one explicit out-edge is rejected
    d3 = dict(d)
    d3["edges"] = d["edges"] + [{"from": "lp", "to": "c"}]
    assert any("at most one" in e for e in wf_dsl.validate_dsl(d3))


@pytest.mark.asyncio
async def test_engine_runs_two_steps_and_writes_context():
    model = ScriptedChatModel(
        scripts=[
            script(text='done a\nWRITE_JSON {"x": 1}'),
            script(text='done b\nWRITE_JSON {"y": 2}'),
        ]
    )
    events = []
    async for ev in engine.run_workflow(_dsl(), run_id="r1", model=model, tools=[]):
        events.append(ev)
    kinds = [e["kind"] for e in events]
    # enter/exit per step + final done
    assert kinds.count("node_enter") == 2
    assert kinds.count("node_exit") == 2
    assert kinds[-1] == "done"
    writes = [e for e in events if e["kind"] == "context_write"]
    assert {k for w in writes for k in w["keys"]} == {"x", "y"}
    # node order: a then b
    enters = [e["node_id"] for e in events if e["kind"] == "node_enter"]
    assert enters == ["a", "b"]


@pytest.mark.asyncio
async def test_engine_runs_loop_over_context_list():
    model = ScriptedChatModel(
        scripts=[
            script(text='iter\nWRITE_JSON {"seen": 1}'),
            script(text='iter\nWRITE_JSON {"seen": 2}'),
            script(text='iter\nWRITE_JSON {"seen": 3}'),
        ]
    )
    d = {
        "name": "w",
        "entry": "lp",
        "context": {"schema": {"type": "object"}, "initial": {"items": ["a", "b", "c"]}},
        "nodes": [
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it", "body": "body", "max_iters": 10},
            {"id": "body", "type": "step", "agent": "dev", "goal": "process {{it}}"},
        ],
        "edges": [],
    }
    events = []
    async for ev in engine.run_workflow(d, run_id="rL", model=model, tools=[]):
        events.append(ev)
    iters = [e for e in events if e["kind"] == "loop_iter"]
    assert len(iters) == 3
    assert [e["index"] for e in iters] == [0, 1, 2]
    assert events[-1]["kind"] == "done"


@pytest.mark.asyncio
async def test_engine_surfaces_step_error_as_event():
    class Boom:
        def bind_tools(self, *a, **k):
            return self

        async def ainvoke(self, *a, **k):
            raise RuntimeError("kaboom")

    d = {"name": "w", "entry": "a", "nodes": [{"id": "a", "type": "step", "agent": "dev", "goal": "g"}], "edges": []}
    events = []
    async for ev in engine.run_workflow(d, run_id="r2", model=Boom(), tools=[]):
        events.append(ev)
    assert any(e["kind"] == "error" and "kaboom" in e.get("error", "") for e in events)
