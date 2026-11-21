"""运行中任务统一面板（Tasks Overview）——六源只读聚合 + 统一停止门面。

产品方案：2026-11-21-运行中任务统一面板产品方案.md §2.1。对标 Claude Code
的 ``/tasks``：Ginno 的六类「agent 正在干什么」句柄全都在，但入口分散在五个
地方——本模块只做两件事：

* ``GET /api/tasks/overview``：一次返回六组活动句柄（统一形状
  ``{kind, id, title, session_id?, session_title?, started_at?/next_fire_at?,
  detail?, stoppable, stop_hint?}``，空态返回空数组而非 404）；
* ``POST /api/tasks/overview/stop``：纯路由门面——按 ``kind`` 把停止动作
  分发给各域既有 handler，**不复制任何停止逻辑**。

六源取数（全部只读复用既有真源，不新增写路径/索引）：

===============  ==========================================================
turns            ``server_shared._RUNNING_TURNS ∪ _PENDING_RESUME``（badge
                 内核同源）join 会话标题；parked（ask_user/permission/
                 version_propose）标 ``stoppable=false`` + ``stop_hint``
scheduled        ``schedule_store.load_config()`` 的启用任务（全局+任务级
                 enabled，含 ``next_run_at``）
subagents        ``plans.store.all_active_plans()``（跨项目扫盘，open 清单）
runs             ``workflows.store.list_runs(status="running")``（与
                 ``GET /api/workflow_runs`` 同源；steps 自带节点进度）
goals            各 project ``goals.json`` 的 active goal（goal_store 按项目
                 集中存储，无需 per-session 扫描）
delegations      ``tools.external_agent._ACTIVE_DELEGATIONS`` 的 in-flight
                 集合（进程内真源；重启后空窗属已知语义，见产品方案 §5.3）
===============  ==========================================================

停止语义（门面分发目标，全部是既有 handler）：

* turn       → ``POST /api/sessions/{sid}/stop`` 同逻辑（``stop_session``）；
  parked（等待回答/权限确认）的 turn 拒停 409——面板把这类行做成「查看」
  跳会话，门面透传这个不可停语义；
* schedule   → ``PATCH /api/schedule/tasks/{id}`` 的 ``enabled=false`` 暂停
  （产品方案 §5.2：不 DELETE，删除留在设置页防误删）；
* plan       → ``POST /api/sessions/{sid}/plans/{plan_id}/cancel``（需
  ``session_id`` 辅助参数——枚举句柄里带，前端回传）；
* run        → ``POST /api/workflow_runs/{run_id}/cancel``；
* goal       → ``DELETE /api/sessions/{sid}/goal``（清目标）；
* delegation → ``POST /api/delegations/{did}/stop``（直杀子进程）。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import paths, schedule_store, server_shared
from ..goals import store as goal_store
from ..plans import store as plan_store
from ..session_meta import _find_meta
from ..workflows import store as wf_store

router = APIRouter()

# parked（_PENDING_KIND）→ 面板行文案。这类 turn 没有活任务在跑，停在面板
# 上的动作应是「去会话里回答」，所以 stoppable=false + hint。
_PARKED_HINTS = {
    "user_question": "等待你回答，请到会话中处理",
    "permission_request": "等待权限确认，请到会话中处理",
    "version_propose": "等待版本选择，请到会话中处理",
}
_PARKED_FALLBACK_HINT = "turn 已挂起等待交互，请到会话中处理"


def _session_title(session_id: str) -> str:
    found = _find_meta(session_id)
    return ((found[0] if found else None) or {}).get("title") or session_id


def _turn_handle(sid: str, parked_kind: str | None, started_at: float | None) -> dict[str, Any]:
    title = _session_title(sid)
    handle: dict[str, Any] = {
        "kind": "turn",
        "id": sid,
        "title": title,
        "session_id": sid,
        "session_title": title,
        "started_at": started_at,
    }
    if parked_kind is not None:
        handle["stoppable"] = False
        handle["stop_hint"] = _PARKED_HINTS.get(parked_kind, _PARKED_FALLBACK_HINT)
        handle["detail"] = handle["stop_hint"]
        return handle
    handle["stoppable"] = True
    since = handle.get("started_at")
    if since:
        handle["detail"] = f"已运行 {max(0, int(time.time() - since))}s"
    return handle


def _snapshot_turns() -> list[tuple[str, str | None, float | None]]:
    # 事件循环拥有这些容器；在第一个 await 前捕获同一时刻的值。
    parked = server_shared._PENDING_RESUME
    return [
        (sid, server_shared._PENDING_KIND.get(sid) if sid in parked else None,
         server_shared._RUNNING_TURNS_AT.get(sid))
        for sid in sorted(set(server_shared._RUNNING_TURNS).union(parked))
    ]


_WEEKDAYS_ZH = ["一", "二", "三", "四", "五", "六", "日"]


def _schedule_label(sched: dict[str, Any]) -> str:
    """调度的人读摘要（2026-11-21 修：此前 detail 填 target.type 内部值，用户看到的是 "prompt" 噪音）。"""
    kind = str(sched.get("kind") or "").strip()
    at = str(sched.get("at") or "").strip()
    if kind == "daily":
        return f"每天 {at}" if at else "每天"
    if kind == "weekly":
        try:
            wd = _WEEKDAYS_ZH[int(sched.get("weekday", 0)) % 7]
        except (TypeError, ValueError):
            wd = "?"
        return f"每周{wd} {at}".strip()
    if kind == "interval":
        try:
            m = max(1, int(sched.get("minutes", 0)))
        except (TypeError, ValueError):
            m = 1
        return f"每 {m} 分钟"
    if kind == "cron" or sched.get("expr"):
        return f"cron {sched.get('expr') or sched.get('cron') or ''}".strip()
    return kind or "调度"


def _collect_scheduled() -> list[dict[str, Any]]:
    cfg = schedule_store.load_config()
    if not cfg.get("enabled", True):
        # 全局调度关闭时不 fire 任何任务——它们不是「正在运行」。
        return []
    out: list[dict[str, Any]] = []
    for task in cfg.get("tasks") or []:
        if not task.get("enabled"):
            continue
        handle = {
            "kind": "schedule",
            "id": str(task.get("id") or ""),
            "title": str(task.get("name") or "未命名任务"),
            "next_fire_at": task.get("next_run_at"),
            "detail": _schedule_label(task.get("schedule") or {}),
            "stoppable": True,
            "stop_hint": "暂停调度（可随时重新启用）",
        }
        out.append(handle)
    out.sort(key=lambda h: h.get("next_fire_at") or 0)
    return out


def _collect_subagents() -> list[dict[str, Any]]:
    # all_active_plans() 跨项目扫 open 清单（重启恢复同款路径）；会话已删的
    # 由这里的 meta 存在性过滤（plans 目录在会话删除后是保留的）。
    out: list[dict[str, Any]] = []
    for _slug, session_id, plan in plan_store.all_active_plans():
        if not _find_meta(session_id):
            continue
        items = plan.get("items") or []
        done = sum(1 for it in items if it.get("status") == plan_store.ITEM_DONE)
        running = sum(1 for it in items if it.get("status") == plan_store.ITEM_RUNNING)
        out.append(
            {
                "kind": "plan",
                "id": str(plan.get("plan_id") or ""),
                "title": str(plan.get("title") or "Untitled plan"),
                "session_id": session_id,
                "session_title": _session_title(session_id),
                "started_at": plan.get("created"),
                "detail": f"{done} done / {running} running / {len(items)} items",
                "stoppable": True,
                "stop_hint": "取消清单（未启动条目直接 stopped）",
            }
        )
    out.sort(key=lambda h: (h["session_id"], h["id"]))
    return out


def _collect_runs() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run in wf_store.list_runs(status="running"):
        steps = run.get("steps") or []
        detail = "running"
        for i, s in enumerate(steps):
            if s.get("status") == "running":
                detail = f"step {i + 1}/{len(steps)}"
                break
        out.append(
            {
                "kind": "run",
                "id": str(run.get("id") or ""),
                "title": str(run.get("name") or run.get("workflow_id") or "workflow"),
                "session_id": run.get("present_in_session_id") or run.get("session_id"),
                "detail": detail,
                "started_at": run.get("started"),
                "stoppable": True,
            }
        )
    out.sort(key=lambda h: h.get("started_at") or 0, reverse=True)
    return out


def _usage_counter(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _goal_progress_label(g: dict[str, Any]) -> str:
    """goal 进度的人读摘要（2026-11-21 修：旧 goal 缺 turns_used/created_at
    ——此前的 "turn 0 · 0min" 对旧数据永远失真；改以 updated_at 相对时间
    为主信号，turns_used 仅在真实 >0 时附带）。"""
    parts: list[str] = []
    turns = _usage_counter(g.get("turns_used"))
    if turns > 0:
        secs = _usage_counter(g.get("time_used_seconds"))
        parts.append(f"turn {turns} · {secs // 60}min")
    updated = g.get("updated_at")
    if updated:
        try:
            ago = max(0, int(time.time() - float(updated)))
            if ago < 60:
                parts.append("刚推进")
            elif ago < 3600:
                parts.append(f"{ago // 60} 分钟前推进")
            elif ago < 86400:
                parts.append(f"{ago // 3600} 小时前推进")
            else:
                parts.append(f"{ago // 86400} 天前推进")
        except (TypeError, ValueError):
            pass
    if not parts:
        parts.append("driver 活跃")
    return " · ".join(parts)


def _collect_goals() -> list[dict[str, Any]]:
    # goal_store 按项目集中存（projects/<slug>/goals.json keyed by session），
    # 全局枚举 = 扫各 project 的文件，只取 active（paused/blocked 的 driver
    # 不在跑，不算「运行中」；complete 会被 turn-end hook 归档走）。
    out: list[dict[str, Any]] = []
    for p in paths.home().glob("projects/*/goals.json"):
        slug = p.parent.name
        try:
            goals = goal_store.list_goals(slug)
        except Exception:  # noqa: BLE001 — 坏文件不拖垮聚合端点
            continue
        for sid, g in goals.items():
            if g.get("status") != goal_store.STATUS_ACTIVE:
                continue
            objective = str(g.get("objective") or "")
            out.append(
                {
                    "kind": "goal",
                    "id": sid,
                    "title": objective[:80] or "（无目标文本）",
                    "session_id": sid,
                    "session_title": _session_title(sid),
                    "started_at": g.get("created_at") or g.get("updated_at"),
                    "detail": _goal_progress_label(g),
                    "stoppable": True,
                    "stop_hint": "清除目标（含用量记录，不可恢复）",
                }
            )
    out.sort(key=lambda h: h.get("started_at") or 0)
    return out


def _snapshot_delegations() -> list[tuple[str, str | None]]:
    # in-flight 真源：_ACTIVE_DELEGATIONS。proc=None 的条目是并发闸门的
    # 预占槽（uuid key、无子会话），不是真实委托，跳过。
    from ..tools.external_agent import _ACTIVE_DELEGATIONS, _ACTIVE_LOCK

    with _ACTIVE_LOCK:
        return [
            (did, entry.get("session_id"))
            for did, entry in _ACTIVE_DELEGATIONS.items()
            if entry.get("proc") is not None
        ]


def _collect_delegations(entries: list[tuple[str, str | None]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for did, session_id in entries:
        found = _find_meta(did)
        meta = (found[0] if found else None) or {}
        started = meta.get("created")
        handle = {
            "kind": "delegation",
            "id": did,
            "title": str(meta.get("title") or f"外部委托 {did[:8]}"),
            "session_id": session_id,
            "detail": "外部 CLI 进程运行中",
            "started_at": started,
            "stoppable": True,
            "stop_hint": "终止外部代理进程（结果作废）",
        }
        if started:
            handle["detail"] = f"外部 CLI · 已运行 {max(0, int(time.time() - started))}s"
        out.append(handle)
    out.sort(key=lambda h: h.get("started_at") or 0)
    return out


@router.get("/api/tasks/overview")
async def tasks_overview() -> dict:
    return await asyncio.to_thread(_collect_overview, _snapshot_turns(), _snapshot_delegations())


def _collect_overview(
    turn_snapshot: list[tuple[str, str | None, float | None]],
    delegation_snapshot: list[tuple[str, str | None]],
) -> dict:
    turns = [_turn_handle(*entry) for entry in turn_snapshot]
    scheduled = _collect_scheduled()
    subagents = _collect_subagents()
    runs = _collect_runs()
    goals = _collect_goals()
    delegations = _collect_delegations(delegation_snapshot)
    return {
        "ok": True,
        "counts": {
            "turns": len(turns),
            "scheduled": len(scheduled),
            "subagents": len(subagents),
            "runs": len(runs),
            "goals": len(goals),
            "delegations": len(delegations),
            "total": (
                len(turns) + len(scheduled) + len(subagents)
                + len(runs) + len(goals) + len(delegations)
            ),
        },
        "turns": turns,
        "scheduled": scheduled,
        "subagents": subagents,
        "runs": runs,
        "goals": goals,
        "delegations": delegations,
    }


class StopRequest(BaseModel):
    kind: str  # turn | schedule | plan | run | goal | delegation
    id: str
    # plan 的 cancel 是 per-session 路由（/api/sessions/{sid}/plans/{pid}/cancel），
    # plan_id 全局不唯一——枚举句柄带 session_id，前端停止时回传。
    session_id: str | None = None


@router.post("/api/tasks/overview/stop")
async def tasks_overview_stop(req: StopRequest) -> dict:
    """统一停止门面：只做 kind → 各域既有 handler 的路由分发。

    各域自身的校验/事件广播原样生效（404 等异常透传）；唯一在门面层拦的
    是 parked turn——GET 侧已标 ``stoppable=false``，这里同语义 409，防止
    UI 缺陷把「等待你回答」的挂起 turn heal 掉。
    """
    # 惰性 import：分发点保持可 monkeypatch（测试），且避免模块级拉起整条
    # sessions/workflows 依赖链。
    if req.kind == "turn":
        if req.id in server_shared._PENDING_RESUME:
            hint = _PARKED_HINTS.get(
                server_shared._PENDING_KIND.get(req.id, ""), _PARKED_FALLBACK_HINT
            )
            raise HTTPException(
                status_code=409, detail={"ok": False, "error": hint, "stop_hint": hint}
            )
        from .sessions.delegations import stop_session

        result = await stop_session(req.id)
    elif req.kind == "schedule":
        from .schedule import patch_task

        result = await patch_task(req.id, data={"enabled": False})
    elif req.kind == "plan":
        if not req.session_id:
            raise HTTPException(
                status_code=400,
                detail={
                    "ok": False,
                    "error": "plan stop requires session_id (from the overview handle)",
                },
            )
        from .plans import cancel_plan_endpoint

        result = await cancel_plan_endpoint(req.session_id, req.id)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result)
    elif req.kind == "run":
        from .workflows import cancel_workflow_run_endpoint

        result = await cancel_workflow_run_endpoint(req.id)
    elif req.kind == "goal":
        from .sessions.goal import clear_session_goal

        result = await clear_session_goal(req.id)
    elif req.kind == "delegation":
        from .sessions.delegations import stop_delegation

        result = await stop_delegation(req.id)
    else:
        raise HTTPException(
            status_code=400,
            detail={
                "ok": False,
                "error": (
                    "kind must be one of turn|schedule|plan|run|goal|delegation, "
                    f"got: {req.kind}"
                ),
            },
        )
    return {"ok": True, "kind": req.kind, "id": req.id, "result": result}
