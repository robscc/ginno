"""成员 turn 生命周期钩子（agent-team-design.md §5.3；backlog P0-7 / P1-2 / P1-3 / P1-4）。

**为什么需要它**：此前 team 的状态回流与回帖只挂在 ``deliver.py`` 自己的两条
路径上（``_run_member_turn`` 结束、steer waiter 轮询到结束）。于是：

* 成员撞权限/ask_user 卡时，``_run_managed_turn`` 的返回只是**挂起**（parked），
  却被当成 turn 结束 → 提前回帖半截话 + 置 done；用户代答后跑出的**真正结果
  永不回主会话**（P0-7）。
* 用户直接在成员工作台开跑的 turn，结束不触发任何 team 逻辑 → blocked 不落盘、
  不推 ``team.changed``、不进 Dock 徽标、排队不 drain（P1-2 / P1-3）。
* 每条 steer 投递各 spawn 一个 waiter → N 次 steer = N 条重复回帖 + N 个
  30 分钟轮询任务（P1-4）。

**解法**：把钩子挂到 engine / resume 的**真实终态点**，成为回帖与状态回流的
**唯一触发源**。deliver 不再自己回帖、不再 spawn waiter。

纪律：
* 钩子**绝不带崩 turn**——整体兜异常（照 ``memory.turn_context`` 范式）。
* 按 ``team_of_session`` 判型：非团队成员会话（普通/subagent/主会话）直接 no-op，
  零副作用。
* 回帖按 turn_id 去重（``member.last_reply_turn``）：engine 与 resume 可能对
  同一 turn 各触发一次，不得双回帖。
"""

from __future__ import annotations

import time

from ..lang import t
from ..server_shared import _log, spawn_bg
from . import status as team_status
from . import store


def _locate(session_id: str) -> tuple[str, dict, dict] | None:
    """(team_id, team, member) —— 仅当该 session 是某团队的**成员实例**。

    主会话命中（member is None）返回 None：广场不自跑 turn，也不回帖给自己。
    """
    located = store.team_of_session(session_id)
    if located is None:
        return None
    team, member = located
    if member is None:
        return None
    team_id = str(team.get("id") or "")
    if not team_id:
        return None
    return team_id, team, member


async def on_turn_parked(session_id: str) -> None:
    """turn 停在 interrupt（权限 / ask_user / 版本确认）→ 让 blocked 实时冒泡。

    **不回帖**：此刻的产出是半截话，回它等于骗主会话（P0-7 的前半）。
    """
    try:
        found = _locate(session_id)
        if found is None:
            return
        team_id, _team, member = found
        team_status.refresh_team(team_id)
        from .deliver import _broadcast_status

        await _broadcast_status(team_id)
        _log.info(
            "team_turn_parked team=%s instance=%s session=%s",
            team_id, member.get("instance_id"), session_id,
        )
    except Exception:  # noqa: BLE001 — 钩子绝不带崩 turn
        _log.exception("team_park_hook_failed session=%s", session_id)


async def on_turn_settled(session_id: str, turn_id: str = "") -> None:
    """turn 真正结束 → 回帖 + 置 done + 状态回流 + 放行排队。

    这是回帖的**唯一触发源**（deliver 不再自己回帖，也不再有 steer waiter）。
    按 turn_id 去重，engine 与 resume 对同一 turn 各触发一次也只回一帖。
    """
    try:
        found = _locate(session_id)
        if found is None:
            return
        team_id, _team, member = found
        instance_id = str(member.get("instance_id") or "")

        # 仍停在 interrupt → 不是终态（resume 路径可能在中途调用）。
        from ..server_shared import _PENDING_RESUME

        if session_id in _PENDING_RESUME:
            await on_turn_parked(session_id)
            return

        # 去重：同一 turn 只回一帖（P1-4）。turn_id 为空时不去重（宁可多回，
        # 不可漏回——漏回是 P0-7 的事故形状）。
        if turn_id and str(member.get("last_reply_turn") or "") == turn_id:
            return

        from .deliver import _broadcast_status, _drain_queue, post_reply

        # 传**真正跑完 turn 的那条会话**（backlog P2-21）：多会话成员在 turn
        # 期间切换活跃会话后，回帖必须读原会话的产出，不能张冠李戴。
        posted = await post_reply(team_id, instance_id, source_session_id=session_id)
        if not posted:
            # Quick-fix D10：post_reply 返回 False（团队/成员已删、主会话
            # 缺失、checkpoint 写失败——post_reply 绝不抛，失败只记日志）时，
            # 绝不能照旧置 done——那会把投递失败静默成「已回帖」，且
            # last_reply_turn 去重会焊死同一 turn 的重试窗口。改为：
            #   · 不写 last_completed / last_reply_turn（保住收件箱语义与重试）；
            #   · 写 sticky 的 last_reply_error，聚合层（status.py）判 error；
            #   · 推 notice 到主会话广场——失败可见，用户可重新投递重试；
            #   · 成员 turn 本身已结束，并发闸照常放行（失败只影响回帖）。
            detail = t(
                "Reply delivery failed — the member's output is intact in its "
                "workspace; re-deliver to retry",
                "回帖投递失败——成员产出仍完整保存在其工作台，可重新投递重试",
            )
            store.patch_member(team_id, instance_id, {
                "last_reply_error": detail,
                "status_detail": detail,
            })
            team_status.refresh_team(team_id)
            await _broadcast_status(team_id)
            from ..server_shared import _push_session_event

            main_sid = str(_team.get("main_session_id") or "")
            await _push_session_event(main_sid, "notice", {"message": detail})
            await _drain_queue(team_id)
            _log.warning(
                "team_reply_settle_failed team=%s instance=%s session=%s turn=%s",
                team_id, instance_id, session_id, turn_id,
            )
            return
        store.patch_member(team_id, instance_id, {
            "status": "done",
            "last_completed": time.time(),
            "last_reply_turn": turn_id or "",
            # 清 sticky 回帖失败（quick-fix D10）：成功即恢复。
            "last_reply_error": "",
            "status_detail": t("Reply posted", "已回帖"),
        })
        team_status.refresh_team(team_id)
        await _broadcast_status(team_id)
        # 并发闸：腾出的额度放行下一条排队投递（P1-3：用户自起 turn 也 drain）。
        await _drain_queue(team_id)
        _log.info(
            "team_turn_settled team=%s instance=%s session=%s turn=%s",
            team_id, instance_id, session_id, turn_id,
        )
    except Exception:  # noqa: BLE001 — 钩子绝不带崩 turn
        _log.exception("team_settle_hook_failed session=%s", session_id)


def spawn_settled(session_id: str, turn_id: str = "") -> None:
    """同步调用点用的薄封装（engine/resume 在同步上下文里 spawn）。"""
    spawn_bg(on_turn_settled(session_id, turn_id))


def spawn_parked(session_id: str) -> None:
    spawn_bg(on_turn_parked(session_id))
