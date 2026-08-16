"""Per-node fault tolerance (stability plan P2): retry / backoff / timeout_s /
on_error, and the control-flow exceptions that must NEVER be retried.

Drives the real compiled graph with ScriptedChatModel + the script_raise()
seam, mirroring tests/unit/test_workflow_engine.py conventions."""

import asyncio

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_raise
from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows import engine


def _dsl(nodes, edges=None):
    return {
        "name": "retry fixture",
        "entry": nodes[0]["id"],
        "nodes": nodes,
        "edges": edges or [],
    }


def _step(nid="a", **extra):
    return {"id": nid, "type": "step", "agent": "dev", "goal": f"do {nid}", **extra}


@pytest.mark.asyncio
async def test_retry_recovers_on_second_attempt():
    dsl = _dsl([_step(retry={"max_attempts": 2, "backoff": "fixed", "backoff_ms": 0})])
    model = ScriptedChatModel(
        scripts=[script_raise(RuntimeError("provider blip")), script(text="done\n")]
    )
    events = [e async for e in engine.run_workflow(dsl, run_id="r-retry-1", model=model, tools=[], project_slug="unit-retry")]
    kinds = [e["kind"] for e in events]
    retries = [e for e in events if e["kind"] == "node_retry"]
    assert len(retries) == 1
    assert retries[0]["attempt"] == 1 and retries[0]["node_id"] == "a"
    assert kinds[-1] == "done"
    assert kinds.count("node_exit") == 1  # the successful attempt only


@pytest.mark.asyncio
async def test_retry_exhausted_stop_is_legacy_failure():
    dsl = _dsl([_step(retry={"max_attempts": 2, "backoff_ms": 0})])
    model = ScriptedChatModel(
        scripts=[script_raise(RuntimeError("boom1")), script_raise(RuntimeError("boom2"))]
    )
    events = [e async for e in engine.run_workflow(dsl, run_id="r-retry-2", model=model, tools=[], project_slug="unit-retry")]
    kinds = [e["kind"] for e in events]
    assert len([e for e in events if e["kind"] == "node_retry"]) == 1
    err = [e for e in events if e["kind"] == "error"]
    assert err and not err[0].get("handled")
    assert kinds[-1] == "error"  # run-level failure path unchanged (on_error=stop)


@pytest.mark.asyncio
async def test_on_error_continue_soft_fails_and_run_completes():
    dsl = _dsl(
        [
            _step(retry={"max_attempts": 1}, on_error="continue"),
            _step("b"),
        ],
        edges=[{"from": "a", "to": "b"}],
    )
    model = ScriptedChatModel(
        scripts=[script_raise(RuntimeError("soft boom")), script(text="b done\n")]
    )
    events = [e async for e in engine.run_workflow(dsl, run_id="r-retry-3", model=model, tools=[], project_slug="unit-retry")]
    kinds = [e["kind"] for e in events]
    handled = [e for e in events if e["kind"] == "error" and e.get("handled")]
    assert len(handled) == 1 and handled[0]["node_id"] == "a"
    assert "node_enter" in kinds and [e for e in events if e["kind"] == "node_enter"][-1]["node_id"] == "b"
    assert kinds[-1] == "done"  # downstream ran; run finishes done with a warning count


@pytest.mark.asyncio
async def test_graph_interrupt_is_never_retried():
    # human node suspends via interrupt(); a retry policy on the same node must
    # NOT swallow the GraphBubbleUp family (it IS an Exception subclass).
    dsl = _dsl(
        [
            {"id": "h", "type": "human", "question": "confirm?", "retry": {"max_attempts": 3, "backoff_ms": 0}},
            _step("b"),
        ],
        edges=[{"from": "h", "to": "b"}],
    )
    model = ScriptedChatModel(scripts=[script(text="b done\n")])
    events = [e async for e in engine.run_workflow(dsl, run_id="r-retry-4", model=model, tools=[], project_slug="unit-retry")]
    kinds = [e["kind"] for e in events]
    assert "interrupt" in kinds
    assert "node_retry" not in kinds  # the suspension was not treated as a failure
    assert kinds[-1] == "paused"


@pytest.mark.asyncio
async def test_per_node_timeout_triggers_retry_then_fails():
    class _Hang:
        def bind_tools(self, *a, **k):
            return self

        async def ainvoke(self, *a, **k):
            await asyncio.sleep(5)
            return script(text="never")

    dsl = _dsl([_step(timeout_s=0.2, retry={"max_attempts": 2, "backoff_ms": 0})])
    events = [e async for e in engine.run_workflow(dsl, run_id="r-retry-5", model=_Hang(), tools=[], project_slug="unit-retry")]
    err = [e for e in events if e["kind"] == "error"]
    assert err and "timed out" in err[-1]["error"]
    assert len([e for e in events if e["kind"] == "node_retry"]) == 1


@pytest.mark.asyncio
async def test_exponential_backoff_sequence(monkeypatch):
    sleeps: list[float] = []

    async def _fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    dsl = _dsl([_step(retry={"max_attempts": 3, "backoff": "exponential", "backoff_ms": 100})])
    model = ScriptedChatModel(
        scripts=[
            script_raise(RuntimeError("e1")),
            script_raise(RuntimeError("e2")),
            script(text="ok\n"),
        ]
    )
    events = [e async for e in engine.run_workflow(dsl, run_id="r-retry-6", model=model, tools=[], project_slug="unit-retry")]
    assert [e["kind"] for e in events][-1] == "done"
    assert sleeps == [0.1, 0.2]  # 100ms * 2^(n-1)


def test_validate_fault_tolerance_fields():
    base = _step(retry={"max_attempts": 11})
    assert any("max_attempts" in e for e in wf_dsl.validate_dsl(_dsl([base])))
    base = _step(retry={"backoff": "linear"})
    assert any("backoff" in e for e in wf_dsl.validate_dsl(_dsl([base])))
    base = _step(retry={"backoff_ms": -5})
    assert any("backoff_ms" in e for e in wf_dsl.validate_dsl(_dsl([base])))
    base = _step(timeout_s=0)
    assert any("timeout_s" in e for e in wf_dsl.validate_dsl(_dsl([base])))
    base = _step(on_error="ignore")
    assert any("on_error" in e for e in wf_dsl.validate_dsl(_dsl([base])))
    # on_error is meaningless on flow-control nodes
    dsl = _dsl([{"id": "br", "type": "branch", "cases": [{"when": "True", "then": "x"}], "default": "x", "on_error": "continue"}, _step("x")])
    assert any("on_error" in e for e in wf_dsl.validate_dsl(dsl))
    # absent fields stay valid (legacy DSLs untouched)
    assert wf_dsl.validate_dsl(_dsl([_step()])) == []


def test_multi_out_edge_rejected_by_default_and_flag_rolls_off(monkeypatch):
    dsl = _dsl(
        [_step(), _step("b"), _step("c")],
        edges=[{"from": "a", "to": "b"}, {"from": "a", "to": "c"}],
    )
    errs = wf_dsl.validate_dsl(dsl)
    assert any("out-edges" in e for e in errs)

    from ginno_runtime import world_state as ws_mod

    monkeypatch.setattr(
        ws_mod, "context_settings", lambda: {"workflow_strict_multi_edge": False}
    )
    assert wf_dsl.validate_dsl(dsl) == []
