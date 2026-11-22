"""方案 D2：GET /api/workflows/python-entries 只读白名单端点。

Studio NodeInspector 的 entry 下拉数据源——与 python_node 校验用的
ENTRY_REGISTRY 同源（单一事实源），UI 永远不会给出一个会被 DSL 校验拒绝的
入口名。无副作用、无 LLM；断连时前端降级手输（前端侧契约）。
"""

from __future__ import annotations

import pytest

from ginno_runtime.workflows.scripts import ENTRY_REGISTRY

pytestmark = pytest.mark.api


def test_python_entries_lists_registry_sorted_with_doc(client):
    r = client.get("/api/workflows/python-entries")
    assert r.status_code == 200, r.text
    body = r.json()
    names = [e["name"] for e in body["entries"]]
    assert names == sorted(ENTRY_REGISTRY.keys())
    # 与注册表逐一对得上（UI 下拉 ⇄ DSL 校验永不漂移）
    assert set(names) == set(ENTRY_REGISTRY.keys())
    # doc 取首行（billing/compare/kb_wiki 的 entry 都有 docstring）
    for e in body["entries"]:
        if e.get("doc"):
            first_line = (ENTRY_REGISTRY[e["name"]].__doc__ or "").strip().splitlines()[0]
            assert e["doc"] == first_line


def test_python_entries_readonly_no_dsl_required(client):
    # 不需要创建任何工作流 / 不触碰 store——纯注册表读取。
    r = client.get("/api/workflows/python-entries")
    assert r.status_code == 200
    assert "errors" not in r.json()
