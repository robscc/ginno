"""The `python` workflow node type: registry, DSL validation, execution.

Deterministic entries run without an LLM; the compiler-injected extract node
must take the WRITE_JSON fast path (never consume the model). Engine-level
tests follow tests/unit/test_workflow_engine.py's event-stream idiom.
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import dsl, engine, nodes
from ginno_runtime.workflows.scripts import ENTRY_REGISTRY
from ginno_runtime.workflows.scripts.billing import _months_between, _usage_to_tokens

pytestmark = pytest.mark.unit


def _dsl(nodes_, initial=None):
    return {
        "name": "t",
        "entry": nodes_[0]["id"],
        "context": {"schema": {"type": "object"}, "initial": initial or {}},
        "nodes": nodes_,
        "edges": [
            {"from": nodes_[i]["id"], "to": nodes_[i + 1]["id"]}
            for i in range(len(nodes_) - 1)
        ],
    }


async def _run(doc, scripts=None):
    model = ScriptedChatModel(scripts=scripts or [])
    events = []
    async for ev in engine.run_workflow(doc, run_id="r1", model=model, tools=[]):
        events.append(ev)
    return events


def test_python_type_registered():
    assert "python" in nodes.known_types()


def test_validate_rejects_unknown_entry():
    doc = _dsl([{"id": "p", "type": "python", "entry": "nope_entry",
                 "writes": {"x": {"type": "object"}}}])
    errs = dsl.validate_dsl(doc)
    assert any("unknown entry" in e for e in errs), errs


def test_validate_accepts_registered_entry():
    doc = _dsl([{"id": "p", "type": "python", "entry": "normalize_and_compare",
                 "writes": {"comparison_summary": {"type": "object"}}}])
    assert not dsl.validate_dsl(doc)


async def test_python_node_writes_context_and_extract_fast_path():
    doc = _dsl(
        [
            {"id": "p", "type": "python", "entry": "normalize_and_compare",
             "args": {"model_name": "{{model_name}}",
                      "aliyun_records": "{{context.aliyun_records}}",
                      "volc_records": "{{context.volc_records}}",
                      "list_prices": "{{context.list_prices}}"},
             "writes": {"comparison_summary": {"type": "object"}}},
            {"id": "w", "type": "step", "agent": "writer",
             "goal": "report {{context.comparison_summary}}"},
        ],
        initial={
            "model_name": "m",
            "aliyun_records": [{"provider": "aliyun", "usage_tokens": 1000,
                                "original": 10, "discount": 4, "payable": 6}],
            "volc_records": [{"provider": "volc", "usage_tokens": 1000,
                              "original": 10, "discount": 5, "payable": 5}],
            "list_prices": {},
        },
    )
    # exactly ONE model call: the final writer step. The python node and its
    # injected extract node must not consume the model.
    events = await _run(doc, scripts=[script(text="report done")])
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"
    cw = [e for e in events if e["kind"] == "context_write"]
    # python commit + extract fast path (write_json), same key both times
    assert [c["method"] for c in cw] == ["python", "write_json"]
    assert all(c["keys"] == ["comparison_summary"] for c in cw)
    enters = [e for e in events if e["kind"] == "node_enter"]
    assert [e["node_type"] for e in enters] == ["python", "extract", "step"]


async def test_python_node_args_raw_placeholder_and_template(monkeypatch):
    # raw placeholder passes the OBJECT; mixed template renders a string
    doc = _dsl(
        [{"id": "p", "type": "python", "entry": "t_echo",
          "args": {"obj": "{{context.things}}", "label": "n={{count}}"},
          "writes": {"echo": {"type": "object"}}}],
        initial={"things": {"a": 1}, "count": 2},
    )
    captured = {}

    def echo(args):
        captured.update(args)
        return {"echo": args}

    monkeypatch.setitem(ENTRY_REGISTRY, "t_echo", echo)
    events = await _run(doc)
    assert events[-1]["kind"] == "done", events
    assert captured["obj"] == {"a": 1}  # raw object, not a rendered string
    assert captured["label"] == "n=2"


async def test_python_entry_failure_fails_run(monkeypatch):
    def boom(args):
        raise ValueError("kaboom")

    monkeypatch.setitem(ENTRY_REGISTRY, "t_boom", boom)
    doc = _dsl([{"id": "p", "type": "python", "entry": "t_boom",
                 "writes": {"x": {"type": "object"}}}])
    events = await _run(doc)
    assert events[-1]["kind"] in ("error", "done")
    err = [e for e in events if e["kind"] == "error"]
    assert err and "kaboom" in str(err[0]), events


async def test_python_node_timeout(monkeypatch):
    def sleepy(args):
        time.sleep(1.5)
        return {"x": {}}

    monkeypatch.setitem(ENTRY_REGISTRY, "t_sleep", sleepy)
    doc = _dsl([{"id": "p", "type": "python", "entry": "t_sleep", "timeout": 0.2,
                 "writes": {"x": {"type": "object"}}}])
    t0 = time.time()
    events = await _run(doc)
    assert time.time() - t0 < 1.4
    err = [e for e in events if e["kind"] == "error"]
    assert err and "timed out" in str(err[0]), events


def test_writes_schema_violation_fails(monkeypatch):
    # entry returns wrong shape → node raises → run errors (not a silent pass)
    import asyncio

    monkeypatch.setitem(ENTRY_REGISTRY, "t_bad", lambda args: {"wrong_key": 1})
    doc = _dsl([{"id": "p", "type": "python", "entry": "t_bad",
                 "writes": {"x": {"type": "object"}}}])
    events = asyncio.run(_run(doc))
    err = [e for e in events if e["kind"] == "error"]
    assert err and "writes schema" in str(err[0]), events


def test_billing_helpers():
    assert _months_between("2026-06-15", "2026-08-02") == ["2026-06", "2026-07", "2026-08"]
    assert _months_between("2026-07", "2026-07") == ["2026-07"]
    assert _usage_to_tokens("123.4", "千tokens") == 123400
    assert _usage_to_tokens("2", "M tokens") == 2_000_000
    assert _usage_to_tokens("500", "tokens") == 500
    assert _usage_to_tokens(None, None) is None


def test_normalize_and_compare_winner():
    out = ENTRY_REGISTRY["normalize_and_compare"]({
        "model_name": "m",
        "aliyun_records": [{"usage_tokens": 2000, "original": 10, "discount": 4, "payable": 6}],
        "volc_records": [{"usage_tokens": 2000, "original": 10, "discount": 5, "payable": 5}],
        "list_prices": {},
    })
    summary = out["comparison_summary"]
    assert summary["cheaper"] == "volc"  # 2.5 vs 3.0 per 1K tokens
    assert summary["providers"]["aliyun"]["effective_per_1k_tokens"] == 3.0
    assert summary["providers"]["volc"]["effective_per_1k_tokens"] == 2.5
    assert summary["same_workload_cost"] == {"aliyun": 12.0, "volc": 10.0}
    assert summary["usage_mix"] == {"aliyun": 0.5, "volc": 0.5}


def test_normalize_and_compare_empty_inputs_raises():
    with pytest.raises(ValueError):
        ENTRY_REGISTRY["normalize_and_compare"]({"model_name": "m"})


async def test_normalize_and_compare_as_node():
    doc = _dsl(
        [{"id": "p", "type": "python", "entry": "normalize_and_compare",
          "args": {"model_name": "m", "aliyun_records": "{{context.aliyun_records}}",
                   "volc_records": "{{context.volc_records}}", "list_prices": {}},
          "writes": {"comparison_summary": {"type": "object"}}}],
        initial={
            "aliyun_records": [{"usage_tokens": 2000, "original": 10, "discount": 4, "payable": 6}],
            "volc_records": [{"usage_tokens": 2000, "original": 10, "discount": 5, "payable": 5}],
        },
    )
    events = await _run(doc)
    assert events[-1]["kind"] == "done"
    cw = [e for e in events if e["kind"] == "context_write"]
    assert cw[0]["keys"] == ["comparison_summary"]
