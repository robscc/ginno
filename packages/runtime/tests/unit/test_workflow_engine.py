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
    # A dedicated body node with NO out-edge of its own (the body->head return
    # is structural). "a" keeps its a->b edge, so it must not be the body.
    d["nodes"].append({"id": "bd", "type": "step", "goal": "per-item"})
    d["nodes"].append({"id": "lp", "type": "loop", "over": "context.items", "body": "bd", "max_iters": 5})
    d["nodes"].append({"id": "c", "type": "step", "goal": "x"})
    # a single explicit "done/next" out-edge is allowed (loop chaining)
    d["edges"].append({"from": "lp", "to": "b"})
    assert wf_dsl.validate_dsl(d) == []
    # an edge from the loop to its own body is structural -> rejected
    d2 = dict(d)
    d2["edges"] = d["edges"] + [{"from": "lp", "to": "bd"}]
    assert any("body" in e for e in wf_dsl.validate_dsl(d2))
    # more than one explicit out-edge is rejected
    d3 = dict(d)
    d3["edges"] = d["edges"] + [{"from": "lp", "to": "c"}]
    assert any("at most one" in e for e in wf_dsl.validate_dsl(d3))
    # a body node carrying its own out-edge is rejected (return is structural)
    d4 = dict(d)
    d4["edges"] = d["edges"] + [{"from": "bd", "to": "c"}]
    assert any("loop body returns" in e for e in wf_dsl.validate_dsl(d4))


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
    # Localization fields: the failing node is attributed and a trimmed
    # traceback is carried so the UI can hand it to a debugger/Claude.
    err = next(e for e in events if e["kind"] == "error")
    assert err.get("node_id") == "a"
    tb = err.get("traceback") or ""
    assert "Traceback" in tb
    assert "kaboom" in tb
    assert "RuntimeError" in tb


@pytest.mark.asyncio
async def test_engine_error_attributes_failing_node_in_chain():
    """A two-step chain where the SECOND step raises: the error event must name
    node "b", and — because events flush incrementally — the footprint of the
    failed node (its node_enter) plus the whole of node "a" must already be in
    the stream. A batch flush would have lost all of it."""
    from langchain_core.messages import AIMessage

    class BoomSecond:
        def __init__(self):
            self.calls = 0

        def bind_tools(self, *a, **k):
            return self

        async def ainvoke(self, *a, **k):
            self.calls += 1
            if self.calls == 1:
                return AIMessage(content="done a")
            raise ValueError("boom-b")

    events = []
    async for ev in engine.run_workflow(_dsl(), run_id="r3", model=BoomSecond(), tools=[]):
        events.append(ev)

    enters = [e["node_id"] for e in events if e["kind"] == "node_enter"]
    exits = [e["node_id"] for e in events if e["kind"] == "node_exit"]
    # node a completed fully; node b entered (footprint kept) then raised.
    assert enters == ["a", "b"]
    assert exits == ["a"]
    err = next(e for e in events if e["kind"] == "error")
    assert err.get("node_id") == "b"
    assert "boom-b" in (err.get("error") or "")
    assert "boom-b" in (err.get("traceback") or "")
