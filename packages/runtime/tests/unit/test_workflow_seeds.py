"""Unit tests for the seed workflow DSLs, incl. the「Wiki 编译」pipeline."""

from __future__ import annotations

import pytest

from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows.store import _SEED

pytestmark = pytest.mark.unit


def test_all_seeds_have_valid_dsl():
    for wf in _SEED:
        d = wf.get("dsl")
        if not d:
            continue
        errs = wf_dsl.validate_dsl(wf_dsl.normalize_dsl(dict(d)))
        assert not errs, (wf["id"], errs)


def test_wiki_compile_pipeline_shape():
    wf = next(w for w in _SEED if w["id"] == "wiki-compile")
    assert wf["system"] is True
    d = wf_dsl.normalize_dsl(dict(wf["dsl"]))
    by_id = {n["id"]: n for n in d["nodes"]}
    assert by_id["inv"]["entry"] == "kb_wiki_inventory"
    loop = by_id["compile"]
    assert loop["type"] == "loop"
    assert loop["parallel"] is True  # fan-out:每个文档一个并发 agent turn
    body = by_id[loop["body"]]  # body 以节点 id 引用
    assert body["type"] == "agent"
    assert body["writes"]["compiled"]["type"] == "array"
    assert by_id["fin"]["entry"] == "kb_wiki_finalize"
    assert by_id["fix"]["type"] == "agent"
    edges = {(e["from"], e["to"]) for e in d["edges"]}
    assert edges == {("inv", "compile"), ("compile", "fin"), ("fin", "fix")}
