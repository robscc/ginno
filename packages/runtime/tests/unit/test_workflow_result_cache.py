"""Cross-run step result cache (odw-comparison P1-①): prefix replay.

Drives the real compiled graph with ScriptedChatModel like
tests/unit/test_workflow_retry.py, asserting the replay contract:
pure-LLM nodes replay on fingerprint hit, the first miss breaks the
prefix, and non-pure turns (tool calls) never cache."""

import time

import pytest
from pydantic import PrivateAttr

from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_raise
from ginno_runtime.workflows import engine
from ginno_runtime.workflows.result_cache import (
    MAX_ENTRIES,
    RunResultCache,
    build_run_cache,
)


class CountingModel(ScriptedChatModel):
    """ScriptedChatModel that counts real _next() pops — a cache hit must
    leave the counter untouched."""

    _calls: int = PrivateAttr(default=0)

    def _next(self, messages=None):
        self._calls += 1
        return super()._next(messages)


def _dsl(nodes, edges=None, context=None):
    d = {
        "name": "rc fixture",
        "entry": nodes[0]["id"],
        "nodes": nodes,
        "edges": edges or [],
    }
    if context:
        d["context"] = context
    return d


def _llm(nid="a", prompt=None, **extra):
    return {"id": nid, "type": "llm", "prompt": prompt or f"say {nid}", **extra}


def _step(nid="a", **extra):
    return {"id": nid, "type": "step", "agent": "dev", "goal": f"do {nid}", **extra}


async def _run(dsl, run_id, model, wf_id, **kw):
    # A FRESH RunResultCache per run, mirroring build_run_cache: the prefix
    # latch is strictly per-run state and must not leak across runs.
    return [
        e
        async for e in engine.run_workflow(
            dsl, run_id=run_id, model=model, tools=[], project_slug="unit-rc",
            result_cache=RunResultCache(wf_id), **kw
        )
    ]


# ---------------------------------------------------------------- llm replay


@pytest.mark.asyncio
async def test_llm_cache_hit_second_run(isolated_home):
    m1 = CountingModel(scripts=[script(text="hello")])
    ev1 = await _run(_dsl([_llm()]), "rc-1", m1, "wf-rc-llm")
    assert [e["kind"] for e in ev1][-1] == "done"
    assert m1._calls == 1

    m2 = CountingModel(scripts=[script(text="SHOULD NOT BE CALLED")])
    ev2 = await _run(_dsl([_llm()]), "rc-2", m2, "wf-rc-llm")
    assert [e["kind"] for e in ev2][-1] == "done"
    assert m2._calls == 0  # replayed, zero live model calls
    exits = [e for e in ev2 if e["kind"] == "node_exit"]
    assert exits and exits[0].get("cached") is True
    assert exits[0].get("output") == "hello"
    assert exits[0]["usage"] == {"input_tokens": 0, "output_tokens": 0}


@pytest.mark.asyncio
async def test_prefix_breaks_on_divergence(isolated_home):

    def chain():
        return _dsl(
            [_llm("a"), _llm("b"), _llm("c")],
            edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "c"}],
        )

    m1 = CountingModel(scripts=[script(text="A"), script(text="B"), script(text="C")])
    await _run(chain(), "rc-3", m1, "wf-rc-brk")
    assert m1._calls == 3

    # b's prompt changed: a replays, b and c run live (latch after first miss).
    def chain2():
        return _dsl(
            [_llm("a"), _llm("b", prompt="CHANGED"), _llm("c")],
            edges=[{"from": "a", "to": "b"}, {"from": "b", "to": "c"}],
        )

    m2 = CountingModel(scripts=[script(text="B2"), script(text="C2")])
    ev2 = await _run(chain2(), "rc-4", m2, "wf-rc-brk")
    assert m2._calls == 2
    cached = {e["node_id"] for e in ev2 if e["kind"] == "node_exit" and e.get("cached")}
    assert cached == {"a"}


@pytest.mark.asyncio
async def test_context_override_change_invalidates(isolated_home):
    # The rendered prompt internalizes the (overridden) initial context, so a
    # different override is a different fingerprint — same mechanism that makes
    # a human context_patch invalidate downstream replays.
    dsl = _dsl([_llm("a", prompt="greet {{context.who}}")], context={
        "schema": {"type": "object", "properties": {"who": {"type": "string"}}},
        "initial": {"who": "alice"},
    })
    m1 = CountingModel(scripts=[script(text="hi alice")])
    await _run(dsl, "rc-5", m1, "wf-rc-ovr", context_override={"who": "alice"})
    assert m1._calls == 1

    m2 = CountingModel(scripts=[script(text="hi bob")])
    ev2 = await _run(dsl, "rc-6", m2, "wf-rc-ovr", context_override={"who": "bob"})
    assert m2._calls == 1  # live — the override changed the rendered prompt
    assert not any(e.get("cached") for e in ev2 if e["kind"] == "node_exit")


@pytest.mark.asyncio
async def test_model_change_invalidates(isolated_home):
    dsl = _dsl([_llm()])
    m1 = CountingModel(scripts=[script(text="A")])
    await _run(dsl, "rc-7", m1, "wf-rc-model", usage_attr={"provider": "p", "model": "m1"})
    m2 = CountingModel(scripts=[script(text="A2")])
    ev2 = await _run(dsl, "rc-8", m2, "wf-rc-model", usage_attr={"provider": "p", "model": "m2"})
    assert m2._calls == 1
    assert not any(e.get("cached") for e in ev2 if e["kind"] == "node_exit")


# ---------------------------------------------------------------- agent steps


@pytest.mark.asyncio
async def test_agent_without_tool_call_cached(isolated_home):
    dsl = _dsl([_step()])
    m1 = CountingModel(scripts=[script(text="step out")])
    await _run(dsl, "rc-9", m1, "wf-rc-agent")
    assert m1._calls == 1

    m2 = CountingModel(scripts=[])
    ev2 = await _run(dsl, "rc-10", m2, "wf-rc-agent")
    assert m2._calls == 0
    exits = [e for e in ev2 if e["kind"] == "node_exit"]
    assert exits and exits[0].get("cached") is True


@pytest.mark.asyncio
async def test_agent_with_tool_call_not_cached(isolated_home):
    from ginno_runtime.testing.fake_model import script_tool_call

    dsl = _dsl([_step()])
    # A tool_call turn with no ToolNode: the loop breaks after the call, but
    # the tool_call EVENT fired — the turn was not pure LLM, so no caching.
    m1 = CountingModel(scripts=[
        script(text="", tool_calls=[script_tool_call("read_file", {"path": "/x"})]),
        script(text="after tool"),
    ])
    ev1 = await _run(dsl, "rc-11", m1, "wf-rc-tool")
    assert any(e["kind"] == "tool_call" for e in ev1)

    m2 = CountingModel(scripts=[script(text="live again")])
    ev2 = await _run(dsl, "rc-12", m2, "wf-rc-tool")
    assert m2._calls == 1  # no entry was recorded → live re-run
    assert not any(e.get("cached") for e in ev2 if e["kind"] == "node_exit")


@pytest.mark.asyncio
async def test_writes_extract_fastpath_on_replay(isolated_home):
    dsl = _dsl([_step("a", writes={"title": {"type": "string"}}), _llm("b")],
               edges=[{"from": "a", "to": "b"}])
    m1 = CountingModel(scripts=[
        script(text='done\nWRITE_JSON {"title": "T"}'),
        script(text="B"),
    ])
    ev1 = await _run(dsl, "rc-13", m1, "wf-rc-wr")
    assert [e["kind"] for e in ev1][-1] == "done"

    # Second run: a replays (text+writes), a__extract takes the WRITE_JSON
    # fast path off the replayed results text (no LLM), b replays too.
    m2 = CountingModel(scripts=[])
    ev2 = await _run(dsl, "rc-14", m2, "wf-rc-wr")
    assert m2._calls == 0
    cached = {e["node_id"] for e in ev2 if e["kind"] == "node_exit" and e.get("cached")}
    assert "a" in cached
    cw = [e for e in ev2 if e["kind"] == "context_write" and e.get("method") == "cache"]
    assert cw and cw[0]["keys"] == ["title"]
    assert [e["kind"] for e in ev2][-1] == "done"


@pytest.mark.asyncio
async def test_failed_node_not_cached(isolated_home):
    dsl = _dsl([_step(retry={"max_attempts": 1})])
    m1 = CountingModel(scripts=[script_raise(RuntimeError("boom"))])
    ev1 = await _run(dsl, "rc-15", m1, "wf-rc-fail")
    assert [e["kind"] for e in ev1][-1] == "error"

    m2 = CountingModel(scripts=[script(text="ok now")])
    ev2 = await _run(dsl, "rc-16", m2, "wf-rc-fail")
    assert m2._calls == 1  # no entry from the failed run
    assert not any(e.get("cached") for e in ev2 if e["kind"] == "node_exit")


# ---------------------------------------------------------------- parallel items


def _par_dsl():
    return {
        "name": "rc parallel fixture",
        "entry": "lp",
        "context": {
            "schema": {"type": "object", "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "reports": {"type": "array", "items": {"type": "string"}},
            }},
            "initial": {"items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
        },
        "nodes": [
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
             "body": "b", "max_iters": 10, "parallel": {"max_concurrency": 2}},
            {"id": "b", "type": "step", "agent": "research", "goal": "analyze {{it.name}}",
             "writes": {"reports": {"type": "array", "items": {"type": "string"}}}},
        ],
        "edges": [],
    }


@pytest.mark.asyncio
async def test_parallel_body_per_item_cache(monkeypatch, isolated_home):
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    m1 = CountingModel(scripts=[
        script(text='r0\nWRITE_JSON {"reports": "r0"}'),
        script(text='r1\nWRITE_JSON {"reports": "r1"}'),
        script(text='r2\nWRITE_JSON {"reports": "r2"}'),
    ])
    ev1 = await _run(_par_dsl(), "rc-17", m1, "wf-rc-par")
    assert [e["kind"] for e in ev1][-1] == "done"
    assert m1._calls == 3

    # Full replay: every item hits, zero model calls.
    m2 = CountingModel(scripts=[])
    ev2 = await _run(_par_dsl(), "rc-18", m2, "wf-rc-par")
    assert m2._calls == 0
    cached_iters = [e for e in ev2 if e["kind"] == "loop_iter" and e.get("cached")]
    assert len(cached_iters) == 3
    assert [e["kind"] for e in ev2][-1] == "done"

    # Change ONE item: only that item re-runs (item fingerprints independent).
    dsl2 = _par_dsl()
    dsl2["context"]["initial"]["items"][1]["name"] = "CHANGED"
    m3 = CountingModel(scripts=[script(text='rx\nWRITE_JSON {"reports": "rx"}')])
    ev3 = await _run(dsl2, "rc-19", m3, "wf-rc-par")
    assert m3._calls == 1


# ---------------------------------------------------------------- gates


@pytest.mark.asyncio
async def test_no_cache_object_means_disabled(isolated_home):
    # resume / retry_from_checkpoint pass result_cache=None — every node must
    # execute live (the checkpoint is the same-run authority).
    m1 = CountingModel(scripts=[script(text="A")])
    ev1 = [
        e
        async for e in engine.run_workflow(
            _dsl([_llm()]), run_id="rc-20", model=m1, tools=[], project_slug="unit-rc"
        )
    ]
    assert m1._calls == 1
    assert not any(e.get("cached") for e in ev1 if e["kind"] == "node_exit")


def test_build_run_cache_gates(monkeypatch, isolated_home):
    monkeypatch.setenv("GINNO_WF_RESULT_CACHE", "0")
    assert build_run_cache({"id": "r"}, "wf") is None
    monkeypatch.setenv("GINNO_WF_RESULT_CACHE", "1")
    assert build_run_cache({"id": "r"}, "wf") is not None
    assert build_run_cache({"id": "r", "cache_disabled": True}, "wf") is None


# ---------------------------------------------------------------- storage hygiene


def test_store_trim_ttl_and_corrupt_rebuild(isolated_home, monkeypatch):
    from ginno_runtime import paths

    cache = RunResultCache("wf-rc-store")
    for i in range(MAX_ENTRIES + 10):
        cache._load()[f"fp{i}"] = {"text": f"t{i}", "writes": {}, "ts": time.time()}
    cache._flush()
    loaded = cache._load()
    assert len(loaded) == MAX_ENTRIES  # oldest evicted
    assert "fp0" not in loaded and f"fp{MAX_ENTRIES + 9}" in loaded

    # TTL: an entry older than the window is dropped on load.
    p = paths.workflow_cache_dir() / "wf-rc-ttl.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"version":1,"entries":{"old":{"text":"x","writes":{},"ts":0}}}')
    assert RunResultCache("wf-rc-ttl")._load() == {}

    # Corrupt index → empty rebuild, never an exception.
    p2 = paths.workflow_cache_dir() / "wf-rc-bad.json"
    p2.parent.mkdir(parents=True, exist_ok=True)
    p2.write_text("{not json")
    assert RunResultCache("wf-rc-bad")._load() == {}

    # Hostile workflow ids must not escape the cache dir.
    assert RunResultCache("../evil")._path.parent == paths.workflow_cache_dir()
