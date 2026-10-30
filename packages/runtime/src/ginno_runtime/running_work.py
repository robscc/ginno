"""「现在有什么在跑」聚合(桌面壳退出前确认 /api/running 的事实源)。

信号全部现成:运行中 turn 看 ``_RUNNING_TURNS``(含调度器 managed turn,
见 server_shared 注释),后台命令看任务中心 registry 的 running 记录。
刻意**不算**的两类:

- 停等恢复的会话(``_PENDING_RESUME``,ask_user/权限卡)——那是泊着的
  会话,checkpoint 已持久,重启后照常恢复,拦退出只会烦人;
- detached 后台命令(background-tasks-design P2)——设计上 sidecar 死后
  继续跑、重启认领,退出不构成打断。
"""

from __future__ import annotations

from .server_shared import _RUNNING_TURNS
from .session_meta import _resolve_session_meta
from .tasks import get_registry
from .tasks import store as task_store


def running_work_report() -> dict:
    """``{ok, sessions: [{id,title}], tasks: [{id,title,command}], count}``。

    同步函数(FastAPI 端点 def 会自动丢线程池):读内存注册表 + 落盘 meta
    取标题。标题/任务列表读取失败一律兜底空值——这个报告服务退出确认,
    查不出来不能拦着用户退出,更不能抛 500。"""
    sessions: list[dict] = []
    for sid in list(_RUNNING_TURNS):  # 快照:turn 收尾会并发 pop
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
    }
