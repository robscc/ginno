"""Session 运行时状态机（2026-11-11，/compact 异步化 P1）。

单一真源视图：idle / running / paused / compacting / swapping。
**不持久化**——进程重启回落 idle，事实源仍是 checkpoint + _RUNNING_TURNS；
本模块是它们的派生视图 + 新增态（compacting/swapping）的权威。

迁移广播 ``session.state_changed``（全局事件），前端/渠道/worker 可订阅。
GET /api/sessions/{id}/state 供重连 reconcile。
"""

from __future__ import annotations

import time
from typing import Any

# 合法状态
STATES = frozenset({"idle", "running", "paused", "compacting", "swapping"})

# sid → {state, since, detail}
_SESSION_STATE: dict[str, dict[str, Any]] = {}


def get_state(session_id: str) -> dict[str, Any]:
    """当前状态快照（缺省 idle）。"""
    return _SESSION_STATE.get(
        session_id, {"state": "idle", "since": 0.0, "detail": None}
    )


def derive_state(session_id: str) -> str:
    """从既有注册表派生 running/paused（P1 收编前的兼容层）。

    compacting/swapping 由 set_state 显式写入，优先级最高；
    否则查 _PENDING_RESUME → paused，_RUNNING_TURNS/_TURN_TASKS → running。
    """
    cur = _SESSION_STATE.get(session_id)
    if cur and cur["state"] in ("compacting", "swapping"):
        return cur["state"]
    from . import server_shared as shared

    if session_id in shared._PENDING_RESUME:
        return "paused"
    task = shared._TURN_TASKS.get(session_id)
    if task is not None and not task.done():
        return "running"
    if session_id in shared._RUNNING_TURNS:
        return "running"
    return "idle"


async def set_state(
    session_id: str, state: str, detail: str | None = None
) -> dict[str, Any]:
    """迁移 + 广播。返回新快照。非法状态抛 ValueError。"""
    if state not in STATES:
        raise ValueError(f"bad state: {state}")
    prev = get_state(session_id)["state"]
    snap = {"state": state, "since": time.time(), "detail": detail}
    _SESSION_STATE[session_id] = snap
    if state == "idle":
        _SESSION_STATE.pop(session_id, None)  # idle 不留条目（省内存）
    if prev != state:
        try:
            from .server_shared import _push_global_event

            await _push_global_event(
                "session.state_changed",
                {"session_id": session_id, "state": state, "prev": prev,
                 "detail": detail},
            )
        except Exception:  # noqa: BLE001 — 广播失败不拦迁移
            pass
    return snap


def is_busy(session_id: str) -> bool:
    """状态机视角的 busy（compacting/swapping/running/paused 均忙）。"""
    return derive_state(session_id) != "idle"
