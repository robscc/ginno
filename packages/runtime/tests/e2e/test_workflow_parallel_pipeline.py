"""E2E: parallel loop 全链路（stability plan 测试清单 #23）。

场景：prep 拆主题 → parallel loop 逐主题调研（gather 模式）→ 汇总消费
``{{context.reports}}``。覆盖计划点名的五个环节：

* parallel loop 走 gather 运行时（单对 node_enter/exit 包整批、per-item
  loop_iter、index 序数组一次写回）；
* WRITE_JSON 快路径（多数 item 直接 parse_writes，不触发抽取 LLM）；
* 一个 item 只回散文 → per-item 内联 ``extract_from_text`` LLM 修正路径
  （与 ExtractNode 共用同一抽取核）；
* array 合成：context.reports = [v0, v1, v2] 按 index 序（软失败位为 null）；
* 下游 step 的 goal 渲染真实消费合成数组（顺序可读）。

并验证编译器形状：顺序 step（prep/use）注入 ``__extract``，parallel body
（probe）跳过注入（stability plan P3b）。脚本顺序依赖 ``max_concurrency:1``
串行化 gather——并发语义本身由 tests/unit/test_workflow_parallel*.py 覆盖。
"""

from __future__ import annotations

import pytest
from pydantic import PrivateAttr

from ginno_runtime.testing.fake_model import ScriptedChatModel, script
from ginno_runtime.workflows import compiler as wf_compiler
from ginno_runtime.workflows import dsl as wf_dsl
from ginno_runtime.workflows import engine

pytestmark = pytest.mark.e2e

_REPORT_ITEM_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "note": {"type": "string"}},
    "required": ["title"],
}


def parallel_dsl() -> dict:
    """The pure DSL (no test-only node types) — also what the API receives."""
    return {
        "dsl_version": "1",
        "name": "parallel-research",
        "description": "prep 拆主题 → 并行逐主题调研 → 汇总",
        "entry": "prep",
        "context": {
            "schema": {
                "type": "object",
                "properties": {
                    "themes": {"type": "array", "items": {"type": "string"}},
                    "reports": {"type": "array", "items": _REPORT_ITEM_SCHEMA},
                    "digest": {"type": "string"},
                },
            },
            "initial": {},
        },
        "nodes": [
            {
                "id": "prep",
                "type": "step",
                "agent": "research",
                "goal": "把调研对象拆成 3 个可并行的子主题",
                "writes": {"themes": {"type": "array", "items": {"type": "string"}}},
            },
            {
                "id": "research",
                "type": "loop",
                "over": "context.themes",
                "as": "theme",
                "body": "probe",
                "max_iters": 8,
                # max_concurrency:1 keeps the scripted model's pop order
                # deterministic; the gather adapter is still the code path.
                "parallel": {"max_concurrency": 1},
            },
            {
                "id": "probe",
                "type": "step",
                "agent": "research",
                "goal": "针对主题「{{theme}}」深入调研，产出一份报告（title+note）",
                "writes": {"reports": {"type": "array", "items": _REPORT_ITEM_SCHEMA}},
            },
            {
                "id": "use",
                "type": "step",
                "agent": "writer",
                "goal": "基于 {{context.reports}} 汇总成一段总结",
                "writes": {"digest": {"type": "string"}},
            },
        ],
        "edges": [
            {"from": "prep", "to": "research"},
            {"from": "research", "to": "use"},
        ],
    }


def _wjson(payload: dict) -> str:
    import json

    return f"\nWRITE_JSON {json.dumps(payload, ensure_ascii=False)}"


class _Recording(ScriptedChatModel):
    """ScriptedChatModel that also records every ainvoke message list."""

    _calls: list = PrivateAttr(default_factory=list)

    async def _agenerate(self, messages, *a, **k):
        self._calls.append(list(messages))
        return await super()._agenerate(messages, *a, **k)


# --------------------------------------------------------------------------- #
# 0. DSL validates; compiler skips extract for the parallel body only
# --------------------------------------------------------------------------- #
def test_dsl_validates_and_parallel_body_skips_extract(client):
    d = wf_dsl.normalize_dsl(parallel_dsl())
    assert wf_dsl.validate_dsl(d) == []

    ids = [n["id"] for n in wf_compiler._inject_extract_nodes(d)["nodes"]]
    assert "prep__extract" in ids  # sequential steps keep injected extracts
    assert "use__extract" in ids
    assert "probe__extract" not in ids  # parallel body extracts inline per item

    r = client.post(
        "/api/workflows", json={"name": "parallel-research", "dsl": parallel_dsl()}
    )
    assert r.status_code == 200, r.text
    assert r.json()["workflow"]["version"] == 1


# --------------------------------------------------------------------------- #
# 1. Full chain: fast path ×2 + one extract-LLM fallback + array consumption
# --------------------------------------------------------------------------- #
async def test_parallel_pipeline_full_chain(isolated_home, monkeypatch):
    monkeypatch.setenv("GINNO_WF_PARALLEL", "1")
    model = _Recording(
        scripts=[
            # prep (its __extract takes the WRITE_JSON fast path — no extra call)
            script(text="拆分完成" + _wjson({"themes": ["上下文压缩", "缓存机制", "评测体系"]})),
            # item 0 — WRITE_JSON fast path
            script(text="报告 A" + _wjson({"reports": {"title": "压缩盘点", "note": "摘要类方法为主"}})),
            # item 1 — prose only: forces the per-item extract LLM path
            script(text="已完成「缓存机制」调研：核心结论是 prefix cache 友好的分段最划算。"),
            # item 1's extraction call (shared extract core, ≤2 self-corrections)
            script(text='{"reports": {"title": "缓存机制", "note": "prefix cache 友好"}}'),
            # item 2 — WRITE_JSON fast path
            script(text="报告 C" + _wjson({"reports": {"title": "评测体系", "note": "L0-L4 漏斗"}})),
            # use (its __extract takes the fast path — no extra call)
            script(text="汇总完成" + _wjson({"digest": "三主题汇总报告"})),
        ]
    )
    events = [
        e
        async for e in engine.run_workflow(
            parallel_dsl(), run_id="par-e2e", model=model, tools=[], project_slug="e2e-par"
        )
    ]
    kinds = [e["kind"] for e in events]
    assert kinds[-1] == "done"

    # Gather shape: ONE node_enter/exit pair wraps all three items.
    enters = [e for e in events if e["kind"] == "node_enter" and e["node_id"] == "probe"]
    exits = [e for e in events if e["kind"] == "node_exit" and e["node_id"] == "probe"]
    assert len(enters) == 1 and enters[0].get("parallel") is True and enters[0]["of"] == 3
    assert len(exits) == 1 and exits[0]["status"] == "done" and exits[0].get("parallel") is True

    # Per-item progress events, index-ordered.
    par_iters = [e for e in events if e["kind"] == "loop_iter" and e.get("parallel")]
    assert [e["index"] for e in sorted(par_iters, key=lambda e: e["index"])] == [0, 1, 2]
    assert all(e["of"] == 3 for e in par_iters)

    # The assembled array was committed in one context_write.
    assert any(
        e["kind"] == "context_write" and e.get("keys") == ["reports"] for e in events
    )

    # The extract-LLM fallback fired exactly once — for item 1's prose — and the
    # extraction prompt carries the item's source text.
    assert len(model._calls) == 6  # prep, i0, i1, extract(i1), i2, use
    extract_msgs = model._calls[3]
    extract_human = str(extract_msgs[-1].content)
    assert "结构化数据抽取器" in extract_human
    assert "缓存机制" in extract_human  # item 1's prose reached the extractor
    # No extraction prompt anywhere else (items 0/2 took the fast path).
    assert all(
        "结构化数据抽取器" not in str(c[-1].content)
        for c in model._calls[:3] + model._calls[4:]
    )

    # Downstream consumption: the use step's rendered goal sees the assembled,
    # index-ordered array (titles from all three items, in order).
    use_human = str(model._calls[-1][-1].content)
    assert "压缩盘点" in use_human and "缓存机制" in use_human and "评测体系" in use_human
    assert use_human.index("压缩盘点") < use_human.index("缓存机制") < use_human.index("评测体系")

    written = {k for e in events if e["kind"] == "context_write" for k in e["keys"]}
    assert {"themes", "reports", "digest"} <= written
