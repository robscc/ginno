"""REST：后台任务中心（background-tasks-design.md §6.2/§7）。

只读 + stop/remove（评审决策 3：不开 POST /api/tasks——任务只能由 agent 的
bg_run 发起）。任务中心与聊天 TaskCard 均以轮询消费本接口（WS 是
per-session 的，任务天然跨 session，1–2s 轮询足够）。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from ..tasks import get_registry
from ..tasks import store as task_store

_log = logging.getLogger("ginno.api.tasks")

router = APIRouter()


def _task_or_404(task_id: str) -> dict:
    """活任务（registry 内存）优先；终态/重启前历史任务回落 index。

    回放聊天里的 TaskCard 会轮询此端点——任务在 sidecar 重启后不在内存
    （startup 只加载 running），但 index 里有最终记录（interrupted/
    finished），必须能查，否则整张卡 404（2026-10-29 实录）。"""
    rec = get_registry().try_get(task_id)
    if rec is None:
        rec = task_store.read_index().get(task_id)
    if rec is None:
        raise HTTPException(
            404, detail={"ok": False, "error": f"unknown task: {task_id}"}
        )
    return rec


@router.get("/api/tasks")
async def list_tasks_endpoint() -> list[dict]:
    """All tasks (running first) across sessions — the task center list."""
    return get_registry().list()


@router.get("/api/tasks/{task_id}")
async def get_task_endpoint(task_id: str) -> dict:
    return _task_or_404(task_id)


@router.get("/api/tasks/{task_id}/logs")
async def task_logs_endpoint(
    task_id: str,
    tail: int = 200,
    offset: int = -1,
) -> dict:
    """Log view for the task center. ``offset`` = line count the client has
    (follow mode: returns only new lines); -1 → plain ``tail`` read."""
    rec = _task_or_404(task_id)
    if offset >= 0:
        lines, total = task_store.read_log(task_id, offset_line=offset)
        return {"status": rec.get("status"), "lines": lines, "total": total}
    if tail > 0:
        lines = task_store.read_log_tail(task_id, tail)
        total = len(task_store.read_log(task_id)[0])
        return {"status": rec.get("status"), "lines": lines, "total": total}
    lines, total = task_store.read_log(task_id)
    return {"status": rec.get("status"), "lines": lines, "total": total}


@router.post("/api/tasks/{task_id}/stop")
async def stop_task_endpoint(task_id: str) -> dict:
    rec = _task_or_404(task_id)
    try:
        rec = get_registry().stop(task_id)
    except KeyError:
        # index-only（重启后历史任务）：本就没有活进程可停，原样返回。
        pass
    return {"ok": True, "task": rec}


@router.delete("/api/tasks/{task_id}")
async def delete_task_endpoint(task_id: str) -> dict:
    _task_or_404(task_id)
    get_registry().remove(task_id)
    return {"ok": True}
