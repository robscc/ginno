"""「现在有什么在跑」聚合(桌面壳退出前确认 /api/running 的事实源)。

信号全部现成:运行中 turn 看 ``live_running_turns()``(经三信号活性验证的
``_RUNNING_TURNS`` 快照,含调度器 managed turn,泄漏条目就地 heal,
见 server_shared),后台命令看任务中心 registry 的 running 记录。
刻意**不算**的两类:

- 停等恢复的会话(``_PENDING_RESUME``,ask_user/权限卡)——那是泊着的
  会话,checkpoint 已持久,重启后照常恢复,拦退出只会烦人;
- detached 后台命令(background-tasks-design P2)——设计上 sidecar 死后
  继续跑、重启认领,退出不构成打断。
"""

from __future__ import annotations

from .server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    live_running_turns,
)
from .session_meta import _resolve_session_meta
from .tasks import get_registry
from .tasks import store as task_store


def running_work_report() -> dict:
    """``{ok, sessions: [{id,title}], tasks: [{id,title,command}], count}``。

    同步函数(FastAPI 端点 def 会自动丢线程池):读内存注册表 + 落盘 meta
    取标题。标题/任务列表读取失败一律兜底空值——这个报告服务退出确认,
    查不出来不能拦着用户退出,更不能抛 500。"""
    sessions: list[dict] = []
    # 经活性验证读（2026-11-22 僵死 turn 事故）：泄漏的 _RUNNING_TURNS 条目
    # （超 STALE 窗口、无活 task、非 parked）就地 heal，不再把僵死 turn 报成
    # 「在跑」拦退出。O(n) 纯内存；snapshot 语义同旧 list() 快照。
    for sid in live_running_turns(via="report"):
        title = ""
        try:
            meta = _resolve_session_meta(sid) or {}
            title = str(meta.get("title") or "")
        except Exception:  # noqa: BLE001 — 标题只是提示文案
            title = ""
        sessions.append({"id": sid, "title": title})

    try:
        records = get_registry().list()
    except Exception:  # noqa: BLE001 — registry 未起/异常按无任务处理
        records = []
    tasks: list[dict] = []
    for rec in records:
        if rec.get("status") != task_store.STATUS_RUNNING or rec.get("detached"):
            continue
        tasks.append(
            {
                "id": str(rec.get("id") or ""),
                "title": str(rec.get("title") or ""),
                "command": str(rec.get("command") or ""),
            }
        )

    return {
        "ok": True,
        "sessions": sessions,
        "tasks": tasks,
        "count": len(sessions) + len(tasks),
        # 停在 interrupt 等用户应答的会话（2026-11-05：侧栏 ask_user 图标）。
        # kind ∈ user_question | *_decision（见 server_shared._PENDING_KIND），
        # 侧栏按 kind 选图标与提示文案。
        "waiting": [
            {"id": sid, "kind": _PENDING_KIND.get(sid, "unknown")}
            for sid in list(_PENDING_RESUME)
        ],
    }
