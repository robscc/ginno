"""Compiler-level supervisor gate injection (design B §8.5): placement rules
and the disabled no-op guarantee."""

from __future__ import annotations

import pytest

from ginno_runtime.workflows import compiler as C
from ginno_runtime.workflows import dsl as wf_dsl

pytestmark = pytest.mark.unit


def _nodes(d):
    return {n["id"]: n for n in d["nodes"]}


def test_disabled_supervisor_is_a_byte_identical_noop():
    src = {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "llm", "prompt": "a"},
            {"id": "s2", "type": "llm", "prompt": "b"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }
    d = wf_dsl.normalize_dsl(src)
    assert (C._inject_supervisor_gates(d)) is d  # same object, untouched


def test_every_step_gates_after_extract_when_writes_present():
    src = {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "llm", "prompt": "a", "writes": {"k": {"type": "string"}}},
            {"id": "s2", "type": "llm", "prompt": "b"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
        "supervisor": {"enabled": True, "mode": "human"},
    }
    d = C._inject_supervisor_gates(C._inject_extract_nodes(wf_dsl.normalize_dsl(src)))
    ns = _nodes(d)
    assert "s1__sup" in ns and ns["s1__sup"]["source_node"] == "s1"
    assert ns["s1__sup"]["continue_to"] == "s2"
    # the gate rides on the extract node, not the producing node
    edge_from_s1 = [e["to"] for e in d["edges"] if e["from"] == "s1"]
    edge_from_extract = [e["to"] for e in d["edges"] if e["from"] == "s1__extract"]
    assert edge_from_s1 == ["s1__extract"]
    assert edge_from_extract == ["s1__sup"]


def test_after_nodes_wins_over_every_step():
    src = {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "llm", "prompt": "a"},
            {"id": "s2", "type": "llm", "prompt": "b"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
        "supervisor": {
            "enabled": True,
            "mode": "human",
            "checkpoints": {"after_nodes": ["s2"]},
        },
    }
    d = C._inject_supervisor_gates(wf_dsl.normalize_dsl(src))
    assert "s1__sup" not in _nodes(d)
    ns = _nodes(d)
    assert ns["s2__sup"]["continue_to"] is None  # s2 ended the graph → END


def test_loop_bodies_and_branch_never_get_gates():
    src = {
        "entry": "fetch",
        "nodes": [
            {"id": "fetch", "type": "llm", "prompt": "a"},
            {"id": "each", "type": "loop", "over": "items", "body": "review", "max_iters": 3},
            {"id": "review", "type": "llm", "prompt": "b"},
            {"id": "gate", "type": "branch", "cases": [{"when": "1", "then": "done"}],
             "default": "done"},
            {"id": "done", "type": "llm", "prompt": "c"},
        ],
        "edges": [{"from": "fetch", "to": "each"}, {"from": "each", "to": "gate"}],
        "supervisor": {"enabled": True, "mode": "human"},
    }
    d = C._inject_supervisor_gates(C._inject_extract_nodes(wf_dsl.normalize_dsl(src)))
    ns = _nodes(d)
    assert "review__sup" not in ns  # loop body: back-edge is structural
    assert "gate__sup" not in ns  # branch routes via cases
    assert "fetch__sup" in ns
    assert "done__sup" in ns


def test_reserved_suffix_rejected():
    errs = wf_dsl.validate_dsl({
        "entry": "x__sup",
        "nodes": [{"id": "x__sup", "type": "llm", "prompt": "a"}],
        "edges": [],
    })
    assert any("__sup" in e for e in errs)


def test_supervisor_config_validation():
    bad = wf_dsl.validate_dsl({
        "entry": "s1",
        "nodes": [{"id": "s1", "type": "llm", "prompt": "a"}],
        "edges": [],
        "supervisor": {"enabled": True, "mode": "human", "retry_limit": 0,
                       "confidence_min": 1.5, "checkpoints": {"after_nodes": "s1"}},
    })
    assert any("retry_limit" in e for e in bad)
    assert any("confidence_min" in e for e in bad)
    assert any("after_nodes" in e for e in bad)
