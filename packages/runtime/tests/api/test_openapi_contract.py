"""OpenAPI contract snapshots for api/sessions.py + api/code.py.

重构批次 0 的验收基准：这两个模块后续要拆成资源 Router（sessions.py 2133 行 /
28 端点、code.py 1629 行 / 9 端点）。拆分 PR 跑本测试，契约 diff 非空即拦。

快照内容 = app.openapi() 中「归属这两个 router」的路径切片：
  - 路径/方法（归属判定来自 ``router.routes`` 本身，而非前缀匹配 ——
    /api/sessions* 与 /api/code* 前缀下还有 plans/distill/usage/code_fsops
    等其他模块的端点，必须剥离；反之 sessions.py 还挂了两个非 sessions 前缀
    的端点（external-agents、delegations stop），必须保留）。
  - 每个 operation 的 parameters / requestBody / responses{status: schema}。
  - 剥离 summary/description/operationId 与 response description（散文，
    非契约；docstring 改动不应触发快照 diff）。
  - components.schemas 仅保留被上述 operation $ref 闭包引用的模型。

路径模板归一化：Starlette 的 ``{rel_path:path}`` 在 OpenAPI 里记作
``{rel_path}``，比较前统一归一（否则 code.py 的 file 端点对不上）。

再生成快照（当前代码为契约真源，生成后连同测试一起提交）::

    cd packages/runtime && GINNO_UPDATE_OPENAPI_SNAPSHOT=1 \\
        uv run --group test --extra docs pytest tests/api/test_openapi_contract.py -q

注意：本文件不改产品代码；允许的维护动作仅两种 —— 更新快照（契约有意识变更）、
扩充 FOREIGN_PREFIX_PATHS（其他模块新占了这两个前缀）。
"""

from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from ginno_runtime import server
from ginno_runtime.api import code as code_api
from ginno_runtime.api import sessions as sessions_api
from ginno_runtime.api import tasks_counts as tasks_counts_api
from ginno_runtime.api import tasks_overview as tasks_overview_api

pytestmark = pytest.mark.api

SNAPSHOTS_DIR = Path(__file__).parent / "openapi_snapshots"
REGENERATE_CMD = (
    "cd packages/runtime && GINNO_UPDATE_OPENAPI_SNAPSHOT=1 "
    "uv run --group test --extra docs pytest tests/api/test_openapi_contract.py -q"
)

OPENAPI_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}

# /api/sessions* 与 /api/code* 前缀下、不属于本批两模块的路径（2026-11-20 冻结）。
# 出现不在名单里的新前缀路径（其他模块新占前缀）→ 测试红，需有意识扩充名单；
# 名单里的路径从 OpenAPI 消失 → 测试红（对应模块把端点删/挪了，需同步本文件）。
FOREIGN_PREFIX_PATHS = {
    # api/code_fsops.py
    "/api/code/delete",
    "/api/code/mkdir",
    "/api/code/move",
    "/api/code/rename",
    # api/distill.py
    "/api/sessions/{session_id}/distill",
    # api/plans.py
    "/api/sessions/{session_id}/plans",
    "/api/sessions/{session_id}/plans/{plan_id}",
    "/api/sessions/{session_id}/plans/{plan_id}/cancel",
    "/api/sessions/{session_id}/plans/{plan_id}/confirm",
    # api/usage.py
    "/api/sessions/{session_id}/usage",
}


def _norm_path(path: str) -> str:
    """OpenAPI 会把 ``{p:path}`` 的 convertor 后缀剥掉，比较前先归一。"""
    return re.sub(r"\{([^{}:]+):path\}", r"{\1}", path)


def _owned_ops(router: Any) -> dict[str, set[str]]:
    """router 自身声明的 path -> 小写 method 集合（拆分后聚合 router 仍成立）。"""
    out: dict[str, set[str]] = {}
    for route in router.routes:
        path = getattr(route, "path", None)
        if not path:
            continue
        methods = {
            str(m).lower() for m in (getattr(route, "methods", None) or ())
        }
        out.setdefault(_norm_path(path), set()).update(methods)
    return out


def _collect_refs(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, val in node.items():
            if key == "$ref" and isinstance(val, str):
                out.add(val)
            else:
                _collect_refs(val, out)
    elif isinstance(node, list):
        for item in node:
            _collect_refs(item, out)


def _schema_closure(refs: set[str], all_schemas: dict[str, Any]) -> dict[str, Any]:
    """从 operation 切片引用的 $ref 出发，取传递闭包内的 component schemas。"""
    closure: dict[str, Any] = {}
    stack = [ref.rsplit("/", 1)[-1] for ref in refs]
    while stack:
        name = stack.pop()
        if name in closure or name not in all_schemas:
            continue
        closure[name] = all_schemas[name]
        inner: set[str] = set()
        _collect_refs(all_schemas[name], inner)
        stack.extend(ref.rsplit("/", 1)[-1] for ref in inner)
    return closure


def _strip_operation(op: dict[str, Any]) -> dict[str, Any]:
    """保留契约字段，剥掉散文（summary/description/operationId/resp description）。"""
    kept: dict[str, Any] = {}
    for field in ("parameters", "requestBody"):
        if field in op:
            kept[field] = op[field]
    responses: dict[str, Any] = {}
    for status, resp in op.get("responses", {}).items():
        content = resp.get("content") if isinstance(resp, dict) else None
        responses[status] = {"content": content} if content else {}
    kept["responses"] = responses
    return kept


def _scoped_contract(router: Any) -> dict[str, Any]:
    """app.openapi() 中归属该 router 的路径切片（含 schema 闭包），排序无关化。"""
    owned = _owned_ops(router)
    # deepcopy：openapi() 的返回值被 FastAPI 缓存在 app 上，其他测试（如
    # diagnostics）还会读同一 dict —— 绝不能原地剥字段。
    spec_paths = copy.deepcopy(server.app.openapi()).get("paths", {})

    missing = sorted(set(owned) - set(spec_paths))
    assert not missing, (
        "router 声明但 app OpenAPI 缺失的路径（include_router 断链？）: "
        f"{missing}"
    )

    scoped: dict[str, dict[str, Any]] = {}
    for path in sorted(owned):
        spec_ops = {
            m: op for m, op in spec_paths[path].items() if m in OPENAPI_METHODS
        }
        extra = sorted(set(spec_ops) - owned[path])
        assert not extra, f"非本 router 的 method 挂在了本 router 的路径上 {path}: {extra}"
        absent = sorted(owned[path] - set(spec_ops))
        assert not absent, f"本 router 的 method 未出现在 OpenAPI {path}: {absent}"
        scoped[path] = {m: _strip_operation(spec_ops[m]) for m in sorted(spec_ops)}

    refs: set[str] = set()
    _collect_refs(scoped, refs)
    schemas = server.app.openapi().get("components", {}).get("schemas", {})
    contract: dict[str, Any] = {"paths": scoped}
    closure = _schema_closure(refs, schemas)
    if closure:
        contract["components"] = {"schemas": closure}
    return contract


def _meta(module_file: str, router_qualname: str, contract: dict[str, Any]) -> dict[str, Any]:
    paths = contract.get("paths", {})
    return {
        "note": (
            "OpenAPI contract snapshot — 拆分资源 Router 的验收基准，勿手改；"
            "契约有意识变更时再生成并连同本文件一起提交。"
        ),
        "source_module": module_file,
        "router": router_qualname,
        "paths": len(paths),
        "endpoints": sum(len(ops) for ops in paths.values()),
        "covers": "path+method+parameters+requestBody+responses(status+schema)+schema闭包",
        "stripped_prose": "summary/description/operationId/response description",
        "regenerate": REGENERATE_CMD,
    }


def _assert_snapshot(name: str, contract: dict[str, Any], meta: dict[str, Any]) -> None:
    snap_path = SNAPSHOTS_DIR / f"{name}.json"
    current = {"_meta": meta, **contract}
    if os.environ.get("GINNO_UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        snap_path.write_text(
            json.dumps(current, sort_keys=True, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        print(f"[openapi-contract] snapshot regenerated: {snap_path}")
        return
    assert snap_path.exists(), f"快照缺失，先再生成: {REGENERATE_CMD}"
    expected = json.loads(snap_path.read_text(encoding="utf-8"))
    assert current == expected, _diff_summary(name, expected, current)


def _diff_summary(name: str, expected: dict[str, Any], current: dict[str, Any]) -> str:
    """契约 diff 的人类可读摘要（集合级全列，内容级截断）。"""
    lines = [f"OpenAPI 契约快照不一致: {name}"]
    e_paths, c_paths = expected.get("paths", {}), current.get("paths", {})
    for label, items in (
        ("新增", sorted(set(c_paths) - set(e_paths))),
        ("缺失", sorted(set(e_paths) - set(c_paths))),
    ):
        if items:
            lines.append(f"  路径{label}: {items}")
    for path in sorted(set(e_paths) & set(c_paths)):
        e_ops, c_ops = e_paths[path], c_paths[path]
        for label, items in (
            ("新增", sorted(set(c_ops) - set(e_ops))),
            ("缺失", sorted(set(e_ops) - set(c_ops))),
        ):
            if items:
                lines.append(f"  {path}: method{label} {items}")
        for method in sorted(set(e_ops) & set(c_ops)):
            e_op, c_op = e_ops[method], c_ops[method]
            if e_op == c_op:
                continue
            lines.append(f"  {path} {method}: operation 契约变化")
            e_resps, c_resps = e_op.get("responses", {}), c_op.get("responses", {})
            for label, items in (
                ("新增", sorted(set(c_resps) - set(e_resps))),
                ("缺失", sorted(set(e_resps) - set(c_resps))),
            ):
                if items:
                    lines.append(f"    status {label}: {items}")
            for status in sorted(set(e_resps) & set(c_resps)):
                if e_resps[status] != c_resps[status]:
                    lines.append(f"    response {status} schema 变化:")
                    lines.append(
                        "      快照: "
                        + json.dumps(e_resps[status], sort_keys=True, ensure_ascii=False)[:1200]
                    )
                    lines.append(
                        "      当前: "
                        + json.dumps(c_resps[status], sort_keys=True, ensure_ascii=False)[:1200]
                    )
            for field in ("parameters", "requestBody"):
                if e_op.get(field) != c_op.get(field):
                    lines.append(f"    {field} 变化:")
                    lines.append(
                        "      快照: "
                        + json.dumps(e_op.get(field), sort_keys=True, ensure_ascii=False)[:800]
                    )
                    lines.append(
                        "      当前: "
                        + json.dumps(c_op.get(field), sort_keys=True, ensure_ascii=False)[:800]
                    )
    e_schemas = expected.get("components", {}).get("schemas", {})
    c_schemas = current.get("components", {}).get("schemas", {})
    for label, items in (
        ("新增", sorted(set(c_schemas) - set(e_schemas))),
        ("缺失", sorted(set(e_schemas) - set(c_schemas))),
    ):
        if items:
            lines.append(f"  schema {label}: {items}")
    for schema in sorted(set(e_schemas) & set(c_schemas)):
        if e_schemas[schema] != c_schemas[schema]:
            lines.append(f"  schema 变化: {schema}")
    lines.append(f"再生成（确认契约变更后提交快照）: {REGENERATE_CMD}")
    return "\n".join(lines)


def test_sessions_openapi_contract_snapshot() -> None:
    contract = _scoped_contract(sessions_api.router)
    meta = _meta(
        "src/ginno_runtime/api/sessions.py",
        "ginno_runtime.api.sessions.router",
        contract,
    )
    assert meta["endpoints"] == 28, f"sessions 端点数漂移: {meta['endpoints']}"
    _assert_snapshot("sessions", contract, meta)


def test_code_openapi_contract_snapshot() -> None:
    contract = _scoped_contract(code_api.router)
    meta = _meta(
        "src/ginno_runtime/api/code.py",
        "ginno_runtime.api.code.router",
        contract,
    )
    assert meta["endpoints"] == 9, f"code 端点数漂移: {meta['endpoints']}"
    _assert_snapshot("code", contract, meta)


class _MergedRoutes:
    """_owned_ops 只读 ``.routes``——把两个 tasks router 拼成一只鸭子。"""

    routes: list[Any] = []


def test_tasks_openapi_contract_snapshot() -> None:
    """tasks_overview（GET overview + POST stop）+ tasks_counts（GET counts）
    的契约快照。counts 是 2026-11-21 徽标轻量路径新增端点，与 overview 的
    ``counts`` 形状一致；明细端点形状变更也会被这里拦住。"""
    routes = _MergedRoutes()
    routes.routes = [*tasks_overview_api.router.routes, *tasks_counts_api.router.routes]
    contract = _scoped_contract(routes)
    meta = _meta(
        "src/ginno_runtime/api/{tasks_overview,tasks_counts}.py",
        "ginno_runtime.api.{tasks_overview,tasks_counts}.router",
        contract,
    )
    assert meta["endpoints"] == 3, f"tasks 端点数漂移: {meta['endpoints']}"
    _assert_snapshot("tasks", contract, meta)


def test_foreign_prefix_paths_pinned() -> None:
    """/api/sessions*、/api/code* 前缀下非本批两模块的路径必须与冻结名单完全一致。"""
    owned = _owned_ops(sessions_api.router)
    for path, methods in _owned_ops(code_api.router).items():
        owned.setdefault(path, set()).update(methods)
    prefixes = ("/api/sessions", "/api/code")
    spec_paths = server.app.openapi().get("paths", {})
    foreign = sorted(
        p
        for p in spec_paths
        if any(p.startswith(prefix) for prefix in prefixes) and p not in owned
    )
    assert foreign == sorted(FOREIGN_PREFIX_PATHS), (
        "前缀归属边界漂移：新模块占了 /api/sessions* 或 /api/code* 前缀，"
        f"或名单过期。\n当前前缀下外来路径: {foreign}\n冻结名单: {sorted(FOREIGN_PREFIX_PATHS)}"
    )
