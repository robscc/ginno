"""派活（worker-protocol-design.md §6.1）：双路投递 + 回执三态。

忙 → steer（同 turn 吸收，回执 queued——未持久，进程崩溃会丢，协议
明示该缺口）；闲 → wake turn（brief 即回合 user_text，天生持久，回执
absorbed）；终态/非 task-typed/不存在 → refused。投递竞态全部继承
``_wake_parent_turn`` 的三道闸（under-lock 重读 / 预置 busy 标记 /
stop-event 前置检查），锁内发现目标忙则降级 steer。
"""

from __future__ import annotations

import time
import uuid
from typing import Any

from ..lang import t
from ..server_shared import (
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _TURN_TASKS,
    _log,
    steer_enqueue,
)
from ..session_meta import _find_meta
from ..subagent_scheduler import _wake_parent_turn
from ..subagent_scheduler.brief import _xml_attr, _xml_text
from .registry import TERMINAL_PROJECTED, project_status, resolve_target


def _sender_title(sender_id: str) -> str:
    found = _find_meta(sender_id)
    if not found:
        return sender_id[:8]
    return str(found[0].get("title") or sender_id[:8])


def _stamp_assign_links(sender_id: str, tid: str) -> None:
    """派活成功后落关联戳(session_links, topbar-unification.md §3.2):发送方
    ``linked_auto``(assigned)+ 目标方 ``in_links``——incoming(谁在派我)
    面板的数据源。延迟导入破环:session_links → worker_bus.registry 反向
    依赖,而本模块被 worker_bus/__init__ 顶层加载。失败静默——戳是视野
    增强,不阻塞投递主流程。"""
    from ..session_links import stamp_link

    stamp_link(sender_id, tid, "assigned")
    stamp_link(tid, sender_id, "assigned-by", field="in_links")


def build_assign_text(
    sender_id: str, sender_title: str, goal: str, constraints: str, acceptance: str
) -> str:
    """派活正文：``<ginno_worker_assign>`` XML 信封（前端折成任务卡，
    同 subagent brief 契约）。接收方按「有能力但零上下文的同事」读——
    信封里带派方身份，模型才知道找谁复命。"""
    sec = ""
    if (constraints or "").strip():
        sec += f"<constraints>{_xml_text(constraints.strip())}</constraints>\n"
    if (acceptance or "").strip():
        sec += f"<acceptance>{_xml_text(acceptance.strip())}</acceptance>\n"
    return (
        f'<ginno_worker_assign from="{_xml_attr(sender_title)}"'
        f' session="{_xml_attr(sender_id)}">\n'
        f"<goal>{_xml_text(goal)}</goal>\n"
        f"{sec}</ginno_worker_assign>"
    )


async def assign_to_worker(
    sender_id: str,
    target: str,
    goal: str,
    constraints: str = "",
    acceptance: str = "",
) -> dict[str, Any]:
    """派活入口。返回回执 dict：

    ``{ok, receipt: absorbed|queued|refused, reason?, status?, msg_id}``
    —— receipt 语义是协议契约（design §4），文案在工具层转双语。
    """
    goal = (goal or "").strip()
    if not goal:
        return {"ok": False, "receipt": "refused", "reason": "goal-empty"}
    tid, meta, err = resolve_target(target)
    if err or not meta:
        return {"ok": False, "receipt": "refused", "reason": "bad-target",
                "detail": err or "unknown target"}
    st = project_status(tid, meta)
    if st in TERMINAL_PROJECTED:
        return {"ok": False, "receipt": "refused", "reason": "terminal",
                "status": st}

    sender_title = _sender_title(sender_id)
    text = build_assign_text(sender_id, sender_title, goal, constraints, acceptance)
    msg_id = uuid.uuid4().hex
    wp: dict[str, Any] = {
        "msg_id": msg_id,
        "from": sender_id,
        "kind": "assign",
        "sent_at": int(time.time()),
    }

    task = _TURN_TASKS.get(tid)
    busy = (
        tid in _RUNNING_TURNS
        or tid in _PENDING_RESUME
        or (task is not None and not task.done())
    )
    if busy:
        # steer：origin="worker" 让段末清场保留未吸收条目（eventual
        # delivery），extra_kwargs 溯源键随持久化 HumanMessage 落 checkpoint。
        steer_enqueue(
            tid,
            {
                "steer_id": f"worker-{msg_id}",
                "turn_id": _RUNNING_TURNS.get(tid) or "",
                "text": text,
                "injected_at": time.time(),
                "origin": "worker",
                "extra_kwargs": {"ginno_worker_msg": wp},
            },
        )
        _log.info("worker_assign_steer from=%s to=%s", sender_id, tid)
        _stamp_assign_links(sender_id, tid)
        return {"ok": True, "receipt": "queued", "msg_id": msg_id}

    # 闲 → wake turn。_wake_parent_turn 的目标检查面向 subagent 终态，
    # delegation/scheduled/main 目标天然放行（main 2026-10-29 已放开可派活，
    # 唤醒 main 的机制本就是它诞生时的用途——子代理回传父级）；channel 仍
    # 在 resolve_target 拒掉。出口码映射回执；steer_origin 让锁内竞态降级
    # 的条目也享受 worker 保留待遇。
    outcome = await _wake_parent_turn(
        tid, text, {"ginno_worker_msg": wp}, steer_origin="worker"
    )
    if outcome == "ran":
        _log.info("worker_assign_wake from=%s to=%s", sender_id, tid)
        _stamp_assign_links(sender_id, tid)
        return {"ok": True, "receipt": "absorbed", "msg_id": msg_id}
    if outcome == "steered":
        _log.info("worker_assign_wake_raced_busy from=%s to=%s", sender_id, tid)
        _stamp_assign_links(sender_id, tid)
        return {"ok": True, "receipt": "queued", "msg_id": msg_id}
    _log.info("worker_assign_refused from=%s to=%s outcome=%s", sender_id, tid, outcome)
    return {"ok": False, "receipt": "refused", "reason": outcome,
            "detail": t("target refused the wake (terminal/stopped race)",
                        "目标拒绝了唤醒（终态/停止竞态）")}
