"""Plan store — 任务清单编排的数据层（subagent-plan-orchestration-design.md §2）.

每张清单一个 JSON 文件::

    ~/.ginno/projects/<slug>/sessions/<sid>/plans/<plan_id>.json

主 agent 生成清单（``create_subagent_plan`` 工具）→ watcher 按清单调度子
代理并回写条目状态 → 去抖合流唤醒主 agent（§4）。文件持久化（内存注册表
+ 原子写）而非纯内存——重启可恢复（§6），前端 board 也能直读。

状态机（条目）：pending → running → done | failed | stopped；
``blocked`` 是调度期派生态（依赖未满足），不落盘。清单级：
active | pending_confirm | completed | cancelled。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from typing import Any

from ginno_runtime import jsonx as json

from .. import paths

ITEM_PENDING = "pending"
ITEM_RUNNING = "running"
ITEM_DONE = "done"
ITEM_FAILED = "failed"
ITEM_STOPPED = "stopped"
ITEM_TERMINAL = {ITEM_DONE, ITEM_FAILED, ITEM_STOPPED}
ITEM_STATUSES = ITEM_TERMINAL | {ITEM_PENDING, ITEM_RUNNING}

PLAN_ACTIVE = "active"
PLAN_PENDING_CONFIRM = "pending_confirm"
PLAN_COMPLETED = "completed"
PLAN_CANCELLED = "cancelled"
PLAN_OPEN = {PLAN_ACTIVE, PLAN_PENDING_CONFIRM}

# 执行器：ginno = 默认子代理（走 create_subagent）；其余为 delegation 后端
# 名（pi / claude-code / …，watcher 侧解析可用性）。空 = ginno（D1）。
BACKEND_GINNO = "ginno"

MAX_ITEMS = 50
MAX_TEXT_CHARS = 4000

# RLock（可重入）：_mutate/amend 等持锁路径会再调 _load（内含加锁）——
# 非 2026-10-12 实测卡死过整条 pytest（threading.Lock 不可重入）。
_lock = threading.RLock()
# (session_id, plan_id) -> plan dict。文件是事实源，注册表只是热缓存；
# watcher 回写都在锁内换缓存，两个并发 settle 不会互相覆盖。
_CACHE: dict[tuple[str, str], dict[str, Any]] = {}


# ---- 路径 -----------------------------------------------------------------

def _plans_dir(slug: str, session_id: str):
    return paths.session_files_dir(slug, session_id) / "plans"


def _plan_path(slug: str, session_id: str, plan_id: str):
    return _plans_dir(slug, session_id) / f"{plan_id}.json"


def _projects_root():
    return paths.home() / "projects"


# ---- 校验 -----------------------------------------------------------------

def _text(v: Any, *, required: bool = False, what: str = "text") -> str:
    s = (v or "").strip() if isinstance(v, str) else ""
    if required and not s:
        raise ValueError(f"{what} required")
    if len(s) > MAX_TEXT_CHARS:
        raise ValueError(f"{what} exceeds {MAX_TEXT_CHARS} chars")
    return s


def _fresh_item(item: dict[str, Any], idx: int) -> dict[str, Any]:
    """新条目规范化（生命周期字段全部从零）。"""
    goal = _text(item.get("goal"), required=True, what=f"items[{idx}].goal")
    deps = item.get("depends_on") or []
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        raise ValueError(f"items[{idx}].depends_on must be a list of item ids")
    return {
        "item_id": _text(item.get("item_id")) or f"it_{idx + 1}",
        "goal": goal,
        "constraints": _text(item.get("constraints")),
        "acceptance": _text(item.get("acceptance")),
        "agent_type": _text(item.get("agent_type")),
        "backend": _text(item.get("backend")) or BACKEND_GINNO,
        # delegation 专用（delegate_agent 同款语义）：read-only 只分析不改
        # 文件；edit 允许改工作区文件。ginno 子代理忽略此字段。
        "mode": "edit" if _text(item.get("mode")) == "edit" else "read-only",
        "depends_on": sorted(set(deps)),
        "status": ITEM_PENDING,
        "child_session_id": None,
        "attempts": 0,
        "result_summary": "",
        "error": "",
        "settled_at": 0,
    }


def _validate_dag(items: list[dict[str, Any]]) -> None:
    """依赖引用存在 + 无环（Kahn）。create 与 amend 共用；不动字段。"""
    ids = [it["item_id"] for it in items]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate item_id")
    known = set(ids)
    indeg = {i: 0 for i in ids}
    children: dict[str, list[str]] = {i: [] for i in ids}
    for it in items:
        for d in it.get("depends_on") or []:
            if d not in known:
                raise ValueError(f"unknown depends_on: {d}")
            indeg[it["item_id"]] += 1
            children[d].append(it["item_id"])
    queue = [i for i, n in indeg.items() if n == 0]
    seen = 0
    while queue:
        cur = queue.pop()
        seen += 1
        for c in children[cur]:
            indeg[c] -= 1
            if indeg[c] == 0:
                queue.append(c)
    if seen != len(ids):
        raise ValueError("depends_on cycle detected")


def _validate_new_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(items, list) or not items:
        raise ValueError("items must be a non-empty list")
    if len(items) > MAX_ITEMS:
        raise ValueError(f"too many items (max {MAX_ITEMS})")
    return [_fresh_item(it, i) for i, it in enumerate(items)]


# ---- 读写 -----------------------------------------------------------------

def _save(slug: str, session_id: str, plan: dict[str, Any]) -> None:
    # create_plan 无外层锁直达此处；变更路径持 RLock 重入。
    with _lock:
        p = _plan_path(slug, session_id, plan["plan_id"])
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        _CACHE[(session_id, plan["plan_id"])] = plan


def _load(slug: str, session_id: str, plan_id: str) -> dict[str, Any] | None:
    with _lock:
        hit = _CACHE.get((session_id, plan_id))
        if hit is not None:
            return hit
    p = _plan_path(slug, session_id, plan_id)
    if not p.exists():
        return None
    try:
        plan = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    with _lock:
        _CACHE[(session_id, plan_id)] = plan
    return plan


def list_plans(slug: str, session_id: str) -> list[dict[str, Any]]:
    """该会话的全部清单（按创建序）。目录缺失 = 空。"""
    d = _plans_dir(slug, session_id)
    if not d.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for p in sorted(d.glob("*.json")):
        plan = _load(slug, session_id, p.stem)
        if plan:
            out.append(plan)
    out.sort(key=lambda x: x.get("created") or 0)
    return out


def get_plan(slug: str, session_id: str, plan_id: str) -> dict[str, Any] | None:
    return _load(slug, session_id, plan_id)


def all_active_plans() -> list[tuple[str, str, dict[str, Any]]]:
    """重启恢复用：扫全部项目的 open 清单 → [(slug, session_id, plan)]。
    会话已删除的由恢复方（watcher 启动入口）过滤——session_files_dir 在
    会话删除后是保留的（paths 注释），这里不做存在性判断。"""
    out: list[tuple[str, str, dict[str, Any]]] = []
    projects = _projects_root()
    if not projects.is_dir():
        return out
    for slug_dir in sorted(projects.iterdir()):
        if not slug_dir.is_dir():
            continue
        sess_dir = slug_dir / "sessions"
        if not sess_dir.is_dir():
            continue
        for sdir in sorted(sess_dir.iterdir()):
            if not (sdir / "plans").is_dir():
                continue
            for p in sorted((sdir / "plans").glob("*.json")):
                try:
                    plan = json.loads(p.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    continue
                if plan.get("status") in PLAN_OPEN:
                    out.append((slug_dir.name, sdir.name, plan))
    return out


# ---- 生命周期 -------------------------------------------------------------

def create_plan(
    session_id: str,
    *,
    slug: str,
    title: str,
    items: list[dict[str, Any]],
    needs_confirm: bool = False,
    author: str = "agent",
) -> dict[str, Any]:
    """建清单。needs_confirm=True（D2）：模型明示这张清单要先经用户确认
    才调度；默认自动启动。"""
    plan_id = f"pl_{uuid.uuid4().hex[:10]}"
    fresh = _validate_new_items(items)
    _validate_dag(fresh)
    plan = {
        "plan_id": plan_id,
        "session_id": session_id,
        "project_slug": slug,
        "title": _text(title) or "Untitled plan",
        "status": PLAN_PENDING_CONFIRM if needs_confirm else PLAN_ACTIVE,
        "version": 1,
        "author": author,
        "created": time.time(),
        "updated": time.time(),
        "items": fresh,
    }
    _save(slug, session_id, plan)
    return plan


def confirm_plan(slug: str, session_id: str, plan_id: str) -> dict[str, Any] | None:
    with _lock:
        plan = _load(slug, session_id, plan_id)
        if plan is None or plan.get("status") != PLAN_PENDING_CONFIRM:
            return None
        plan["status"] = PLAN_ACTIVE
        plan["updated"] = time.time()
        _save(slug, session_id, plan)
        return plan


def cancel_plan(slug: str, session_id: str, plan_id: str) -> dict[str, Any] | None:
    """取消清单：未启动条目直接 stopped；在跑的由 watcher 收到状态变化后
    下发协作式停止（不改这里的数据——running 条目保留，等子会话真停）。"""
    with _lock:
        plan = _load(slug, session_id, plan_id)
        if plan is None or plan.get("status") in (PLAN_COMPLETED, PLAN_CANCELLED):
            return None
        plan["status"] = PLAN_CANCELLED
        plan["updated"] = time.time()
        for it in plan["items"]:
            if it["status"] == ITEM_PENDING:
                it["status"] = ITEM_STOPPED
                it["settled_at"] = time.time()
        _save(slug, session_id, plan)
        return plan


def _bump(plan: dict[str, Any]) -> None:
    plan["version"] = int(plan.get("version") or 0) + 1
    plan["updated"] = time.time()


def amend_plan(
    slug: str,
    session_id: str,
    plan_id: str,
    *,
    add: list[dict[str, Any]] | None = None,
    remove: list[str] | None = None,
    reset: list[str] | None = None,
    update: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, str]:
    """主 agent 的改单（D3）：add / remove / reset（复位重试，attempts+1）/
    update（改文本，仅限未启动条目）。已启动条目只能 reset（先等它终态）。
    保留既有条目的生命周期字段——只规范新条目，DAG 全量再校验。"""
    with _lock:
        plan = _load(slug, session_id, plan_id)
        if plan is None:
            return None, "plan not found"
        if plan.get("status") in (PLAN_COMPLETED, PLAN_CANCELLED):
            return None, f"plan is {plan['status']}"

        items = plan["items"]
        by_id = {it["item_id"]: it for it in items}
        for iid in remove or []:
            it = by_id.get(iid)
            if it is None:
                return None, f"unknown item: {iid}"
            if it["status"] == ITEM_RUNNING:
                return None, f"item {iid} is running (stop it first)"
        for iid in reset or []:
            it = by_id.get(iid)
            if it is None:
                return None, f"unknown item: {iid}"
            if it["status"] == ITEM_RUNNING:
                return None, f"item {iid} is running"
        for iid in (update or {}):
            it = by_id.get(iid)
            if it is None:
                return None, f"unknown item: {iid}"
            if it["status"] != ITEM_PENDING:
                return None, f"item {iid} already started (reset it first)"

        removed = set(remove or [])
        items = [it for it in items if it["item_id"] not in removed]
        existing_ids = {it["item_id"] for it in items}
        for i, raw in enumerate(add or []):
            norm = _fresh_item(raw, len(items) + i)
            while norm["item_id"] in existing_ids:  # 撞旧 id 自动顺延
                norm["item_id"] += "_x"
            existing_ids.add(norm["item_id"])
            items.append(norm)
        for it in items:  # 被删条目的依赖引用一并清掉
            it["depends_on"] = [d for d in it.get("depends_on") or [] if d not in removed]
        for iid in reset or []:
            it = next(x for x in items if x["item_id"] == iid)
            it.update(
                {
                    "status": ITEM_PENDING,
                    "child_session_id": None,
                    "result_summary": "",
                    "error": "",
                    "settled_at": 0,
                }
            )
            it["attempts"] = int(it.get("attempts") or 0) + 1  # reset = retry
        for iid, patch in (update or {}).items():
            it = next(x for x in items if x["item_id"] == iid)
            for k in ("goal", "constraints", "acceptance", "agent_type", "backend", "mode"):
                if k in patch:
                    if k == "mode":
                        it[k] = "edit" if _text(patch.get(k)) == "edit" else "read-only"
                    else:
                        it[k] = _text(patch.get(k), required=(k == "goal"), what=k)

        try:
            _validate_dag(items)
        except ValueError as e:
            return None, str(e)
        plan["items"] = items
        _bump(plan)
        _save(slug, session_id, plan)
        return plan, ""


# ---- watcher 用：调度与回写 ------------------------------------------------

def ready_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    """可调度条目：pending ∧ 依赖全 done。依赖 failed/stopped 的保持不动
    （阻塞由注入报告给主 agent，由它 amend 处置——D3）。"""
    by_id = {it["item_id"]: it for it in plan.get("items") or []}
    out = []
    for it in plan.get("items") or []:
        if it.get("status") != ITEM_PENDING:
            continue
        deps = it.get("depends_on") or []
        if all(by_id.get(d, {}).get("status") == ITEM_DONE for d in deps):
            out.append(it)
    return out


def blocked_reasons(plan: dict[str, Any]) -> dict[str, str]:
    """item_id → 依赖了哪些失败/中止条目（注入报告用）。"""
    by_id = {it["item_id"]: it for it in plan.get("items") or []}
    out: dict[str, str] = {}
    for it in plan.get("items") or []:
        if it.get("status") != ITEM_PENDING:
            continue
        bad = [
            d
            for d in it.get("depends_on") or []
            if by_id.get(d, {}).get("status") in (ITEM_FAILED, ITEM_STOPPED)
        ]
        if bad:
            out[it["item_id"]] = ",".join(bad)
    return out


def running_items(plan: dict[str, Any]) -> list[dict[str, Any]]:
    return [it for it in plan.get("items") or [] if it.get("status") == ITEM_RUNNING]


def _mutate(
    slug: str,
    session_id: str,
    plan_id: str,
    item_id: str,
    *,
    expect_status: str,
    apply,
) -> dict[str, Any] | None:
    with _lock:
        plan = _load(slug, session_id, plan_id)
        if plan is None:
            return None
        it = next((x for x in plan["items"] if x["item_id"] == item_id), None)
        if it is None or (expect_status and it["status"] != expect_status):
            return None
        apply(it)
        _bump(plan)
        if plan["status"] == PLAN_ACTIVE and all(
            x["status"] in ITEM_TERMINAL for x in plan["items"]
        ):
            plan["status"] = PLAN_COMPLETED
        _save(slug, session_id, plan)
        return plan


def mark_running(
    slug: str, session_id: str, plan_id: str, item_id: str, child_session_id: str
) -> dict[str, Any] | None:
    def apply(it):
        it["status"] = ITEM_RUNNING
        it["child_session_id"] = child_session_id
        it["attempts"] = int(it.get("attempts") or 0) + 1

    return _mutate(slug, session_id, plan_id, item_id, expect_status=ITEM_PENDING, apply=apply)


def mark_settled(
    slug: str,
    session_id: str,
    plan_id: str,
    item_id: str,
    status: str,
    *,
    result_summary: str = "",
    error: str = "",
) -> dict[str, Any] | None:
    if status not in ITEM_TERMINAL:
        raise ValueError(f"not a terminal status: {status}")

    def apply(it):
        it["status"] = status
        it["result_summary"] = (result_summary or "")[:600]
        it["error"] = (error or "")[:600]
        it["settled_at"] = time.time()

    return _mutate(slug, session_id, plan_id, item_id, expect_status=ITEM_RUNNING, apply=apply)


def fail_item(
    slug: str, session_id: str, plan_id: str, item_id: str, error: str
) -> dict[str, Any] | None:
    """启动失败直标 failed（条目还是 pending——spawn 没成功就没有
    running 态可期望）。完成判定照常参与。"""

    def apply(it):
        it["status"] = ITEM_FAILED
        it["error"] = (error or "")[:600]
        it["settled_at"] = time.time()

    return _mutate(slug, session_id, plan_id, item_id, expect_status="", apply=apply)


def settle_by_child(
    session_id: str, child_session_id: str, status: str, *, summary: str = "", error: str = ""
) -> list[dict[str, Any]]:
    """子会话终态反查回写（settle 钩子入口）：该会话全部 open 清单里按
    child_session_id 找条目。返回更新后的 plan 列表供 watcher 推事件/唤醒。"""
    out: list[dict[str, Any]] = []
    with _lock:
        cached = [
            _CACHE[k]
            for k in _CACHE
            if k[0] == session_id and _CACHE[k].get("status") in PLAN_OPEN
        ]
    for plan in cached:
        it = next(
            (x for x in plan.get("items") or [] if x.get("child_session_id") == child_session_id),
            None,
        )
        if it is None or it.get("status") in ITEM_TERMINAL:
            continue
        slug = plan.get("project_slug") or "default"
        updated = mark_settled(
            slug, session_id, plan["plan_id"], it["item_id"], status,
            result_summary=summary, error=error,
        )
        if updated:
            out.append(updated)
    return out
