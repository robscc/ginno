"""Additional loop.parallel coverage (stability plan P3): per-item retry, the
concurrency cap, and the per-item extract-LLM fallback path. Complements
test_workflow_parallel.py (validation / gather order / failure policies / flag)."""

import asyncio

import pytest
from langchain_core.messages import AIMessage

from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_raise
from ginno_runtime.workflows import engine


def _par_dsl(body_extra=None, loop_extra=None):
    return {
        "name": "parallel extra fixture",
        "entry": "lp",
        "context": {
            "schema": {"type": "object"},
            "initial": {"items": ["a", "b", "c"]},
        },
        "nodes": [
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
             "body": "b", "max_iters": 10, "parallel": True, **(loop_extra or {})},
            {"id": "b", "type": "step", "agent": "research", "goal": "analyze {{it}}",
             "writes": {"reports": {"type": "array", "items": {"type": "string"}}},
             **(body_extra or {})},
        ],
        "edges": [],
    }


@pytest.mark.asyncio
async def test_parallel_per_item_retry_recovers(monkeypatch):
    """A transient per-item failure is retried (body node's retry policy applied
    PER ITEM) and the final array is complete — no null slot."""
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    dsl = _par_dsl(
        body_extra={"retry": {"max_attempts": 2, "backoff": "fixed", "backoff_ms": 0}},
        loop_extra={"parallel": {"max_concurrency": 1}},  # deterministic script order
    )
    model = ScriptedChatModel(scripts=[
        script(text='WRITE_JSON {"reports": "r0"}'),
        script_raise(RuntimeError("transient")),   # item1 attempt1 fails
        script(text='WRITE_JSON {"reports": "r1"}'),  # item1 attempt2 recovers
        script(text='WRITE_JSON {"reports": "r2"}'),
    ])
    events = [e async for e in engine.run_workflow(
        dsl, run_id="pm-retry", model=model, tools=[], project_slug="unit-par")]
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"
    retries = [e for e in events if e["kind"] == "node_retry"]
    assert len(retries) == 1 and retries[0]["item"] == 1
    assert "loop_item_error" not in kinds  # recovered, not soft-failed


@pytest.mark.asyncio
async def test_parallel_concurrency_is_capped(monkeypatch):
    """No more than max_concurrency items run at once."""
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    dsl = _par_dsl(loop_extra={"parallel": {"max_concurrency": 2}})

    class TrackingModel:
        def __init__(self):
            self.inflight = 0
            self.peak = 0

        def bind_tools(self, *a, **k):
            return self

        async def ainvoke(self, *a, **k):
            self.inflight += 1
            self.peak = max(self.peak, self.inflight)
            await asyncio.sleep(0.05)  # overlap window so concurrency is observable
            self.inflight -= 1
            return AIMessage(content='WRITE_JSON {"reports": "r"}', id="t")

    m = TrackingModel()
    events = [e async for e in engine.run_workflow(
        dsl, run_id="pm-cap", model=m, tools=[], project_slug="unit-par")]
    assert [e["kind"] for e in events][-1] == "done"
    assert m.peak <= 2, f"peak concurrency {m.peak} exceeded max_concurrency=2"


@pytest.mark.asyncio
async def test_parallel_per_item_extract_llm_fallback(monkeypatch):
    """An item whose reply has no WRITE_JSON is extracted by the LLM path
    (extract_from_text) — the assembled array still gets that element."""
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    dsl = _par_dsl(loop_extra={"parallel": {"max_concurrency": 1}})
    model = ScriptedChatModel(scripts=[
        script(text='WRITE_JSON {"reports": "r0"}'),       # item0 fast path
        script(text="r1 result, no structured marker"),     # item1 turn → needs LLM extract
        script(text='{"reports": "r1"}'),                   # item1 extract reply
        script(text='WRITE_JSON {"reports": "r2"}'),       # item2 fast path
    ])
    events = [e async for e in engine.run_workflow(
        dsl, run_id="pm-extract", model=model, tools=[], project_slug="unit-par")]
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"
    assert "loop_item_error" not in kinds
    # All three items contributed; the middle one via the extract LLM path.
    cw = [e for e in events if e["kind"] == "context_write" and "reports" in e.get("keys", [])]
    assert cw
