"""Per-item checkpoint granularity for the parallel gather adapter
(2026-08-17 follow-up): finished items persist via put_writes and a
re-executed activation skips them instead of replaying the whole batch."""

import pytest

from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_raise
from ginno_runtime.workflows.nodes import builtin

_DSL = {
    "name": "resume fixture",
    "entry": "lp",
    "nodes": [
        {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
         "body": "b", "max_iters": 10, "parallel": {"max_concurrency": 1}},
        {"id": "b", "type": "step", "agent": "research", "goal": "analyze {{it}}",
         "writes": {"reports": {"type": "array", "items": {"type": "string"}}}},
    ],
    "edges": [],
}

_ITEMS = ["a", "b", "c"]


def _setup():
    cp = FileCheckpointer("par-resume", surface_pending_writes=True)
    ret = cp.put(
        {"configurable": {"thread_id": "run-r1"}},
        {"id": "cp1", "ts": "t", "channel_values": {}, "channel_versions": {},
         "versions_seen": {}},
        {"step": 1},
        {},
    )
    config = {"configurable": {"thread_id": "run-r1", "checkpoint_id": ret["configurable"]["checkpoint_id"]}}
    cctx = {
        "dsl": _DSL,
        "run_ctx": {"run_id": "run-r1", "events": []},
        "tools": [],
        "checkpointer": cp,
    }
    state = {"context": {"items": _ITEMS}, "loop_vars": {"it": _ITEMS},
             "context_meta": {}, "results": {}}
    return cp, config, cctx, state


@pytest.mark.asyncio
async def test_reexecuted_activation_skips_finished_items():
    cp, config, cctx, state = _setup()

    # First activation: item 0 finishes, item 1 blows up (on_error=stop).
    cctx["model"] = ScriptedChatModel(scripts=[
        script(text='WRITE_JSON {"reports": "r0"}'),
        script_raise(RuntimeError("item1 crashed")),
    ])
    with pytest.raises(RuntimeError):
        await builtin._execute_parallel_body(
            _DSL["nodes"][1], _DSL["nodes"][0], cctx, state, {}, config
        )
    saved = cp.get_app_pending(config, "parallel_progress")
    assert saved and 0 in saved and saved[0]["vals"] == {"reports": "r0"}

    # Re-execution (what resume / crash-retry does): only items 1..2 run.
    model2 = ScriptedChatModel(scripts=[
        script(text='WRITE_JSON {"reports": "r1"}'),
        script(text='WRITE_JSON {"reports": "r2"}'),
    ])
    cctx["model"] = model2
    out = await builtin._execute_parallel_body(
        _DSL["nodes"][1], _DSL["nodes"][0], cctx, state, {}, config
    )
    assert out["context"]["reports"] == ["r0", "r1", "r2"]  # r0 restored, not re-run
    resumed = [e for e in out["events"] if e["kind"] == "loop_iter" and e.get("resumed")]
    assert len(resumed) == 1 and resumed[0]["index"] == 0
    # progress cleared after the batch committed
    assert cp.get_app_pending(config, "parallel_progress") == {}


@pytest.mark.asyncio
async def test_first_activation_without_progress_runs_all():
    cp, config, cctx, state = _setup()
    cctx["model"] = ScriptedChatModel(scripts=[
        script(text='WRITE_JSON {"reports": "r0"}'),
        script(text='WRITE_JSON {"reports": "r1"}'),
        script(text='WRITE_JSON {"reports": "r2"}'),
    ])
    out = await builtin._execute_parallel_body(
        _DSL["nodes"][1], _DSL["nodes"][0], cctx, state, {}, config
    )
    assert out["context"]["reports"] == ["r0", "r1", "r2"]
    assert not [e for e in out["events"] if e["kind"] == "loop_iter" and e.get("resumed")]
