"""定时任务端点（scheduled-tasks-design.md §5.1）。

- GET/PUT /api/schedule               — 全局开关（enabled/keep_awake）
- POST /api/schedule/tasks            — 新建（间隔下限、workflow 必填输入校验）
- PATCH/DELETE /api/schedule/tasks/{id}
- POST /api/schedule/tasks/{id}/run   — 立即执行（manual，按 target 分流）
- GET  /api/schedule/runs             — 执行记录分页 + 过滤（同 run_id 去重）
- POST /api/schedule/runs/seen        — 标记已读（单条 / 一键全部，schedule-unseen）
- POST /api/schedule/runs/{id}/fork   — 影子会话 fork 为普通会话（schedule-followup-design）
- GET  /api/schedule/timeline         — 当天 runs + 剩余计划点
- WS   /api/ws/schedule               — run_started/run_finished/task_updated/missed/runs_seen
"""

from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from .. import schedule_store as store
from .. import scheduler
from .. import workflows as wf_store

router = APIRouter()


def _schedule_payload() -> dict:
    cfg = store.load_config()
    return {
        "enabled": bool(cfg.get("enabled", True)),
        "keep_awake": bool(cfg.get("keep_awake", False)),
        "tasks": cfg.get("tasks") or [],
        # 未读执行结果统计（schedule-unseen）：侧栏红点与执行记录页签角标
        # 的数据源，与任务列表同端点返回，AppShell 一次冷轮询全拿。
        "unseen": store.unseen_summary(),
    }


def _emit_task_updated(task: dict) -> None:
    scheduler.schedule_events().emit("task_updated", {"task": task})


@router.get("/api/schedule")
async def get_schedule() -> dict:
    return _schedule_payload()


@router.put("/api/schedule")
async def put_schedule(data: dict) -> dict:
    """整体更新 enabled/keep_awake（任务走粒度接口，§5.1）。"""
    data = data or {}
    cfg = store.load_config()
    if "enabled" in data:
        cfg["enabled"] = bool(data["enabled"])
    if "keep_awake" in data:
        cfg["keep_awake"] = bool(data["keep_awake"])
    store.save_config(cfg)
    _emit_task_updated({"_global": {"enabled": cfg["enabled"], "keep_awake": cfg["keep_awake"]}})
    return _schedule_payload()


def _validate_inputs_check(data: dict) -> None:
    """workflow 目标保存时校验（§3.2）：配方存在则必填输入缺失 → 400；
    配方已删除允许保存（触发时报 workflow_missing）。"""
    target = data.get("target") or {}
    if target.get("type") != "workflow":
        return
    wf = wf_store.get_def(str(target.get("workflow_id") or ""))
    if not wf:
        return
    ctx = (wf.get("dsl") or {}).get("context") or {}
    missing = store.missing_required_inputs(
        ctx.get("schema"), ctx.get("initial"), target.get("context_override")
    )
    if missing:
        raise HTTPException(
            status_code=400, detail=f"Missing required workflow inputs: {', '.join(missing)}"
        )


def _validated_task(data: dict, *, partial: bool, current: dict | None = None) -> dict:
    """合并（PATCH）或整体（POST）校验任务数据，返回待落库的任务字段。"""
    merged = dict(current or {})
    for key in ("name", "target", "schedule", "enabled", "notify"):
        if key in data:
            merged[key] = data[key]
    errs = store.validate_task(merged)
    if errs:
        raise HTTPException(status_code=400, detail="; ".join(errs))
    _validate_inputs_check(merged)
    return merged


@router.post("/api/schedule/tasks")
async def create_task(data: dict) -> dict:
    data = data or {}
    merged = _validated_task(data, partial=False)
    now = time.time()
    task = {
        "id": store.new_task_id(),
        "name": merged.get("name"),
        "target": merged.get("target"),
        "schedule": merged.get("schedule"),
        "enabled": bool(merged.get("enabled", True)),
        "notify": bool(merged.get("notify", False)),  # P1
        "created": now,
        "updated": now,
    }
    nxt = scheduler.compute_next(task, now)
    if nxt is not None:
        task["next_run_at"] = nxt
    store.upsert_task(task)
    _emit_task_updated(task)
    return task


@router.patch("/api/schedule/tasks/{task_id}")
async def patch_task(task_id: str, data: dict) -> dict:
    current = store.get_task(task_id)
    if not current:
        raise HTTPException(status_code=404, detail="task not found")
    data = data or {}
    merged = _validated_task(data, partial=True, current=current)
    patch = {k: merged[k] for k in ("name", "target", "schedule", "enabled", "notify") if k in data}
    # 仅计划变更/启停重算 next_run_at（§5.1）；改名等无关字段不动它——interval
    # 的网格锚点若被重置，会让每次改名都把节拍平移到 now+间隔。
    if "next_run_at" in data and data["next_run_at"] is None:
        patch["next_run_at"] = None
    elif "schedule" in data or "enabled" in data:
        probe = {**current, **patch}
        nxt = scheduler.compute_next(probe, time.time()) if probe.get("enabled") else None
        patch["next_run_at"] = nxt
    updated = store.patch_task(task_id, patch)
    _emit_task_updated(updated or current)
    return updated or current


@router.delete("/api/schedule/tasks/{task_id}")
async def delete_task(task_id: str) -> dict:
    """删除任务；执行记录与影子会话保留（§3.2）。"""
    ok = store.delete_task(task_id)
    if not ok:
        raise HTTPException(status_code=404, detail="task not found")
    scheduler.schedule_events().emit("task_updated", {"task": {"id": task_id, "deleted": True}})
    return {"ok": True}


@router.post("/api/schedule/tasks/{task_id}/run")
async def run_task_now(task_id: str) -> dict:
    """立即执行（manual，不计入计划，§3.2）。进行中 → 记 skipped_overlap。"""
    task = store.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="task not found")
    run_id = scheduler.trigger_task(task, trigger="manual")
    return {"ok": True, "run_id": run_id}


@router.get("/api/schedule/runs")
async def list_runs(
    date: str | None = Query(None),
    task_id: str | None = Query(None),
    target_type: str | None = Query(None),
    status: str | None = Query(None),
    trigger: str | None = Query(None),
    seen: str | None = Query(None),
    sort: str = Query("desc"),
    page: int = Query(1),
    page_size: int = Query(50),
) -> dict:
    """执行记录分页（同 run_id 去重，§4.2）。``seen=unseen|seen`` 过滤未读/
    已读（schedule-unseen）；每行附带 ``seen`` 布尔。"""
    return store.query_runs(
        date_str=date,
        task_id=task_id,
        target_type=target_type,
        status=status,
        trigger=trigger,
        seen=seen if seen in ("unseen", "seen") else None,
        sort="asc" if sort == "asc" else "desc",
        page=page,
        page_size=page_size,
    )


@router.post("/api/schedule/runs/{run_id}/fork")
async def fork_run(run_id: str) -> dict:
    """把一次执行的影子会话 fork 成普通会话（schedule-followup-design §1/§2）。

    新会话 = create_session（普通类型，agent/project 沿任务 target；任务已删
    则回落默认）+ _seed_fork_history 复制影子会话全量历史（id 全部重铸，顶部
    一条 [turn context] 溯源行）。影子会话本体不动——回放/下一轮定时照旧。
    一 run 一副本：已 fork 且副本存活时幂等返回既有会话；副本被删则重新
    fork。run 行回写 forked_session_id（
    「跳到对应对话」用）。"""
    run = store.get_run(run_id)
    if not run:
        return {"ok": False, "error": "run not found"}
    if run.get("status") not in ("ok", "error"):
        return {"ok": False, "error": "run is not finished"}
    src = run.get("session_id")
    if not src:
        return {"ok": False, "error": "run has no session to fork"}

    # 一 run 一副本（2026-10-14 产品定稿，覆盖早先「允许多次 fork」）：
    # 已 fork 且副本仍存活 → 幂等返回既有会话（防其他窗口/旧 UI 重复点击
    # 造出双胞胎）；副本被用户删了 → 清掉死引用走重新 fork。
    existing = run.get("forked_session_id")
    if existing:
        from ..session_meta import _find_meta

        if _find_meta(existing):
            return {"ok": True, "session_id": existing, "existing": True}
        run = {**run, "forked_session_id": None}

    task = store.get_task(run.get("task_id") or "") if run.get("task_id") else None
    target = (task or {}).get("target") or {}
    slug = target.get("project_slug") or "default"
    agent_id = target.get("agent_id")
    base_name = (run.get("task_name") or "").lstrip("⏰").strip() or "Scheduled run"

    from .sessions import CreateSessionRequest, create_session

    meta = await create_session(
        CreateSessionRequest(
            project_slug=slug,
            workspace="",
            agent_id=agent_id,
            title=base_name,
        )
    )
    if meta.get("ok") is False or not meta.get("id"):
        return {"ok": False, "error": str(meta.get("error") or "session creation failed")}
    new_id = meta["id"]

    from ..lang import t
    from ..session_meta import _session_meta_patch

    started = run.get("started_at") or 0
    stamp = time.strftime("%m-%d %H:%M", time.localtime(started)) if started else ""
    brief = t(
        f"[turn context] Continued from scheduled task <{base_name}>"
        f" (run of {stamp}). Its full transcript follows — keep discussing the"
        " results here.",
        f"[turn context] 续自定时任务〈{base_name}〉（{stamp} 执行）。以下为其"
        "完整记录，可就结果继续沟通。",
    )
    try:
        from ..subagent_scheduler import _seed_fork_history

        await _seed_fork_history(src, new_id, slug, brief)
    except Exception as e:  # noqa: BLE001 — 种子失败要显式报错（副本没历史不能用）
        from ..server_shared import _log

        _log.exception("schedule_fork_seed_failed run=%s src=%s child=%s", run_id, src, new_id)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    _session_meta_patch(slug, new_id, {"forked_from_run": run_id})
    # run 行回写最近 fork 落点（jsonl 末行胜出语义 = append）
    store.record_run({**run, "forked_session_id": new_id})
    from ..server_shared import _log

    _log.info("schedule_run_forked run=%s src=%s child=%s", run_id, src, new_id)
    return {"ok": True, "session_id": new_id}


@router.post("/api/schedule/runs/seen")
async def mark_runs_seen(data: dict) -> dict:
    """标记执行结果已读（schedule-unseen）。body ``{"run_id": "..."}`` 标单条
    （前端点开某行）；``{"all": true}`` 一键全部已读（watermark=now）。
    返回最新未读摘要，并向 /api/ws/schedule 发 ``runs_seen`` 事件（侧栏
    红点 / 页签角标即时刷新）。"""
    data = data or {}
    if data.get("all"):
        summary = store.mark_runs_seen(None)
    else:
        rid = str(data.get("run_id") or "")
        if not rid:
            raise HTTPException(status_code=400, detail="run_id required (or all:true)")
        summary = store.mark_runs_seen([rid])
    scheduler.schedule_events().emit("runs_seen", {"unseen": summary, "all": bool(data.get("all"))})
    return {"ok": True, "unseen": summary}


@router.get("/api/schedule/timeline")
async def timeline(date: str | None = Query(None)) -> dict:
    """时间条数据：当天 runs + enabled 任务的剩余计划点（§5.1）。"""
    ds = date or time.strftime("%Y-%m-%d", time.localtime())
    try:
        time.strptime(ds, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD") from None
    runs = store.dedupe_runs(store.load_day(ds))
    cfg = store.load_config()
    planned = []
    if cfg.get("enabled", True):
        for task in cfg.get("tasks") or []:
            if not task.get("enabled"):
                continue
            for pt in scheduler.planned_points(task, ds):
                planned.append(
                    {"task_id": task.get("id"), "task_name": task.get("name") or "", "at": pt}
                )
    planned.sort(key=lambda p: p["at"])
    return {"date": ds, "runs": runs, "planned": planned}


@router.websocket("/api/ws/schedule")
async def schedule_events_ws(ws: WebSocket) -> None:
    """定时任务事件通道（照 connectors WS 通道的实现模式）：先发 snapshot，
    之后推 run_started/run_finished/task_updated/missed。"""
    await ws.accept()
    bus = scheduler.schedule_events()
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def _listener(type_: str, data: dict) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, {"type": type_, **(data or {})})

    bus.subscribe(_listener)
    try:
        await ws.send_json({"type": "snapshot", **_schedule_payload()})
        while True:
            msg = await queue.get()
            await ws.send_json(msg)
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 — client vanished mid-send
        pass
    finally:
        bus.unsubscribe(_listener)
