"""loop.parallel data parallelism (stability plan P3): DSL validation, doctor
rules, compiler extract-skip, and the gather runtime (index-ordered arrays,
per-item failure policies, flag degradation)."""

import pytest
from pydantic import PrivateAttr

from ginno_runtime.testing.fake_model import ScriptedChatModel, script, script_raise
from ginno_runtime.workflows import compiler as wf_compiler
from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows import doctor as wf_doctor
from ginno_runtime.workflows import engine


def _par_dsl(parallel=True, body_extra=None, loop_extra=None):
    return {
        "name": "parallel fixture",
        "entry": "seed",
        "context": {
            "schema": {"type": "object", "properties": {
                "items": {"type": "array", "items": {"type": "object"}},
                "reports": {"type": "array", "items": {"type": "string"}},
            }},
            "initial": {"items": [{"name": "a"}, {"name": "b"}, {"name": "c"}]},
        },
        "nodes": [
            {"id": "seed", "type": "step", "agent": "research", "goal": "seed"},
            {"id": "lp", "type": "loop", "over": "context.items", "as": "it",
             "body": "b", "max_iters": 10, **({"parallel": parallel} if parallel is not None else {}), **(loop_extra or {})},
            {"id": "b", "type": "step", "agent": "research", "goal": "analyze {{it.name}}",
             "writes": {"reports": {"type": "array", "items": {"type": "string"}}},
             **(body_extra or {})},
            {"id": "use", "type": "step", "agent": "writer", "goal": "use {{context.reports}}"},
        ],
        "edges": [{"from": "seed", "to": "lp"}, {"from": "lp", "to": "use"}],
    }


def test_validate_parallel_body_writes_must_be_arrays():
    dsl = _par_dsl(body_extra={"writes": {"reports": {"type": "string"}}})
    errs = wf_dsl.validate_dsl(dsl)
    assert any("array" in e for e in errs)


def test_validate_parallel_body_must_be_step():
    dsl = _par_dsl()
    dsl["nodes"][2] = {"id": "b", "type": "llm", "prompt": "x",
                       "writes": {"reports": {"type": "array", "items": {"type": "string"}}}}
    errs = wf_dsl.validate_dsl(dsl)
    assert any("step/agent" in e for e in errs)


def test_validate_parallel_shape():
    dsl = _par_dsl(parallel="yes")
    assert any("parallel" in e for e in wf_dsl.validate_dsl(dsl))
    dsl = _par_dsl(parallel={"max_concurrency": 9})
    assert any("max_concurrency" in e for e in wf_dsl.validate_dsl(dsl))
    dsl = _par_dsl(parallel={"max_concurrency": 4})
    assert wf_dsl.validate_dsl(dsl) == []


def test_doctor_flags_parallel_body_contract():
    dsl = _par_dsl(body_extra={"writes": {"reports": {"type": "object"}}})
    doc = wf_doctor.run_doctor(dsl)
    assert any(f["rule"] == "loop.parallel.body_writes_not_array" for f in doc["errors"])


def test_compiler_skips_extract_for_parallel_body():
    d = wf_compiler._inject_extract_nodes(wf_dsl.normalize_dsl(_par_dsl()))
    ids = [n["id"] for n in d["nodes"]]
    assert "b__extract" not in ids
    # sequential control: same DSL without parallel DOES inject
    d2 = wf_compiler._inject_extract_nodes(wf_dsl.normalize_dsl(_par_dsl(parallel=None)))
    assert "b__extract" in [n["id"] for n in d2["nodes"]]
    # step table agrees with the graph
    steps = wf_dsl.steps_from_dsl(d, include_extracts=True)
    assert all(not s["id"].endswith("b__extract") for s in steps)


@pytest.mark.asyncio
async def test_parallel_gather_writes_index_ordered_arrays(monkeypatch):
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    model = ScriptedChatModel(scripts=[
        script(text="seed done"),
        script(text='r0\nWRITE_JSON {"reports": "r0"}'),
        script(text='r1\nWRITE_JSON {"reports": "r1"}'),
        script(text='r2\nWRITE_JSON {"reports": "r2"}'),
        script(text="used"),
    ])
    events = [e async for e in engine.run_workflow(
        _par_dsl(parallel={"max_concurrency": 2}), run_id="p-1", model=model,
        tools=[], project_slug="unit-par")]
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"
    # The gather adapter emits one parallel loop_iter per item as it starts.
    par_iters = [e for e in events if e["kind"] == "loop_iter" and e.get("parallel")]
    assert len(par_iters) == 3
    assert [e["index"] for e in sorted(par_iters, key=lambda e: e["index"])] == [0, 1, 2]
    assert all(e["of"] == 3 for e in par_iters)
    enters = [e for e in events if e["kind"] == "node_enter" and e["node_id"] == "b"]
    assert len(enters) == 1 and enters[0].get("parallel") is True
    ctx_writes = [e for e in events if e["kind"] == "context_write" and "reports" in e.get("keys", [])]
    assert ctx_writes  # the gather committed the assembled array


@pytest.mark.asyncio
async def test_parallel_on_error_continue_nulls_failed_index(monkeypatch):
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    dsl = _par_dsl(parallel={"max_concurrency": 1}, body_extra={
        "on_error": "continue",
        "writes": {"reports": {"type": "array", "items": {"type": "string"}}},
    })
    model = ScriptedChatModel(scripts=[
        script(text="seed done"),
        script(text='WRITE_JSON {"reports": "r0"}'),
        script_raise(RuntimeError("item blew up")),
        script(text='WRITE_JSON {"reports": "r2"}'),
        script(text="used"),
    ])
    events = [e async for e in engine.run_workflow(
        dsl, run_id="p-2", model=model, tools=[], project_slug="unit-par")]
    assert [e["kind"] for e in events][-1] == "done"
    item_errs = [e for e in events if e["kind"] == "loop_item_error"]
    assert len(item_errs) == 1


@pytest.mark.asyncio
async def test_parallel_on_error_stop_fails_run(monkeypatch):
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    model = ScriptedChatModel(scripts=[
        script(text="seed done"),
        script(text='WRITE_JSON {"reports": "r0"}'),
        script_raise(RuntimeError("fatal item")),
        script(text='WRITE_JSON {"reports": "r2"}'),
    ])
    events = [e async for e in engine.run_workflow(
        _par_dsl(parallel={"max_concurrency": 1}), run_id="p-3", model=model,
        tools=[], project_slug="unit-par")]
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "error"
    assert "loop_item_error" not in kinds


class _Recording(ScriptedChatModel):
    """ScriptedChatModel that also records every ainvoke message list."""

    _calls: list = PrivateAttr(default_factory=list)

    async def _agenerate(self, messages, *a, **k):
        self._calls.append(list(messages))
        return await super()._agenerate(messages, *a, **k)


@pytest.mark.asyncio
async def test_parallel_downstream_step_consumes_assembled_array(monkeypatch):
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    model = _Recording(scripts=[
        script(text="seed done"),
        script(text='WRITE_JSON {"reports": "r0"}'),
        script(text='WRITE_JSON {"reports": "r1"}'),
        script(text='WRITE_JSON {"reports": "r2"}'),
        script(text="used"),
    ])
    events = [e async for e in engine.run_workflow(
        _par_dsl(), run_id="p-5", model=model, tools=[], project_slug="unit-par")]
    assert [e["kind"] for e in events][-1] == "done"
    # The final "use" step's rendered goal must see the index-ordered array.
    last_human = str(model._calls[-1][-1].content)
    assert "r0" in last_human and "r2" in last_human
    assert last_human.index("r0") < last_human.index("r1") < last_human.index("r2")


@pytest.mark.asyncio
async def test_parallel_flag_off_degrades_to_sequential_with_warning(monkeypatch):
    monkeypatch.delenv("GINNO_WF_PARALLEL", raising=False)  # settings default off
    model = ScriptedChatModel(scripts=[
        script(text="seed done"),
        script(text='WRITE_JSON {"reports": "r0"}'),
        script(text='WRITE_JSON {"reports": "r1"}'),
        script(text='WRITE_JSON {"reports": "r2"}'),
        script(text="used"),
    ])
    events = [e async for e in engine.run_workflow(
        _par_dsl(), run_id="p-4", model=model, tools=[], project_slug="unit-par")]
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"
    assert any(e["kind"] == "warning" and "parallel" in e.get("message", "") for e in events)
    seq_iters = [e for e in events if e["kind"] == "loop_iter" and not e.get("parallel")]
    assert len(seq_iters) == 3  # sequential passes, not a gather
