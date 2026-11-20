"""Plan-route handlers for ``invoke``: @session deliveries and the
AgentTeam mention/plaza routes (batch 4-2 split of ``session_ws``).

These are the invoke branches that answer a message WITHOUT starting a
graph turn in this session — the message belongs elsewhere (another
session, a team member). Each handler returns ``True`` when the invoke is
fully handled (the caller then stops processing); ``False`` falls through
to the normal turn path, preserving the pre-split fallthrough semantics.
"""

from __future__ import annotations

import time
import uuid

from .... import server_shared as shared
from ....server_shared import _RUNNING_TURNS, _log, _push_session_event, spawn_bg
from ... import sessions as sessions_mod
from .handshake import WsConn


async def _deliver_turn(
    target_id: str, tsession: dict, text: str, src_turn: str, label: str
) -> None:
    """@session idle 投递的后台 turn（at-session-design §5.1）——scheduler 的
    headless 同款协议：早置 running 标志 + 关交互 ask_user + finally 清理。
    事件广播目标会话 sockets（无 WS 也落 checkpoint，重连可恢复）。"""
    from ..turn import _run_stream
    from ....tools.ask_tools import reset_interactive as _reset_interactive_turn
    from ....tools.ask_tools import set_interactive as _set_interactive_turn

    tslug = tsession.get("project_slug") or "default"
    cfg = {
        "configurable": {
            "thread_id": target_id,
            "project_slug": tslug,
            "agent_id": tsession.get("agent_id"),
            "turn_id": str(uuid.uuid4()),
            "user_text": text[:200],
            "usage_source": "deliver",
        }
    }
    tok = _set_interactive_turn(False)
    try:
        await _run_stream(
            None, tsession["graph"], cfg, text, tsession,
            tsession.get("agent_id"), origin="deliver",
        )
    except Exception:
        _log.exception("deliver_turn_failed session=%s src_turn=%s", target_id, src_turn)
    finally:
        _reset_interactive_turn(tok)
        if _RUNNING_TURNS.get(target_id) == src_turn or target_id in _RUNNING_TURNS:
            _RUNNING_TURNS.pop(target_id, None)




async def handle_plan_deliveries(
    conn: WsConn, msg: dict, plan, turn_id: str, user_text: str
) -> None:
    session_id = conn.session_id
    session = conn.session
    # @session 投递短路（at-session-design §5.1）：prompt 属于
    # 目标会话，本会话不开 graph turn。目标 working → steer
    # 注入（[来自 X 的投递] 前缀）；idle → 后台 headless turn
    # （create_task——接收循环绝不 await 长任务，/compact 老坑）。
    # P0：投递模式下其余 mention 不随行（回执注明）。
    _body = (user_text or "").strip()
    _src_title = str(session.get("title") or session_id)[:48]
    _report: list[dict] = []
    for _d in plan.deliveries:
        _tid = _d["id"]
        if not _body:
            _report.append({"id": _tid, "label": _d["label"], "state": "no_body"})
            return
        _ts = sessions_mod._ensure_session(_tid)
        if not _ts:
            _report.append({"id": _tid, "label": _d["label"], "state": "missing"})
            return
        # @session delivery into a channel session is the
        # same readonly bypass — refuse that target.
        if sessions_mod.is_channel_session(_ts):
            _report.append({"id": _tid, "label": _d["label"], "state": "channel_readonly"})
            return
        _payload_text = f"[来自 {_src_title} 的投递]\n{_body}"
        if _tid in _RUNNING_TURNS:
            _steer_id = f"deliver-{turn_id[:8]}-{_tid[:8]}"
            shared.steer_enqueue(
                _tid,
                {
                    "steer_id": _steer_id,
                    "turn_id": _RUNNING_TURNS.get(_tid) or "",
                    "text": _payload_text,
                    "injected_at": time.time(),
                },
            )
            _report.append({"id": _tid, "label": _d["label"], "state": "queued"})
        else:
            _RUNNING_TURNS[_tid] = turn_id
            spawn_bg(
                _deliver_turn(_tid, _ts, _payload_text, turn_id, _d["label"])
            )
            _report.append({"id": _tid, "label": _d["label"], "state": "started"})
    _log.info(
        "deliver_report session=%s turn=%s targets=%d ctx_skipped=%d",
        session_id, turn_id, len(_report), len(plan.mention_ctx),
    )
    await _push_session_event(
        session_id,
        "deliver.report",
        {"targets": _report, "context_skipped": len(plan.mention_ctx) > 0},
        turn_id,
    )
    await _push_session_event(session_id, "message.end", {}, turn_id)
    return


async def handle_team_mention(
    conn: WsConn, msg: dict, plan, turn_id: str, user_text: str
) -> bool:
    session_id = conn.session_id
    session = conn.session
    from ....teams.deliver import (
        record_plaza_message,
        resolve_agent_mention,
    )

    # P1-9：@ 多人并行投递。此前只取首个 @agent，
    # 「@dev @writer 各自做 X」里 writer 被静默忽略。
    # route.py 的数组契约早已预留，无 @ 广播分支也已证明
    # 多目标循环可行。非团队会话仍走原 persona 路径。
    _targets = plan.agent_overrides or [plan.agent_override]
    # 先判定是否团队主会话：非团队会话走原 persona 路径，
    # 那条路径自己会持久化用户消息，这里绝不能抢先写（会重复）。
    from ....teams import store as _tstore

    _located = _tstore.team_of_session(session_id)
    _is_plaza = _located is not None and _located[1] is None
    if _is_plaza:
        # P0-1：**先落盘再投递**。此前放在投递之后，成员 turn
        # 可能已跑完并 post_reply 写同一 checkpoint → 并发写
        # 丢失用户消息（实测偶发），且历史顺序也错（回帖排在
        # 用户消息之前）。先写保证顺序正确并避开该竞态。
        await record_plaza_message(
            session_id, user_text, turn_id,
            routed_to=",".join(str(a) for a in _targets),
        )
    _receipts: list[tuple[str, dict]] = []
    for _aid in _targets:
        _r = await resolve_agent_mention(session_id, _aid, user_text)
        if _r is not None:
            _receipts.append((_aid, _r))
    if _receipts:
        for _aid, _r in _receipts:
            await _push_session_event(
                session_id,
                "team.delivered",
                {
                    "agent_id": _aid,
                    "text": user_text,
                    **_r,
                },
                turn_id,
            )
        # P0-1：必须收 message.end——投递分支不起主会话
        # turn，没有它前端 busy 恒真、composer 永久锁死。
        # 照上方 mods 命令分支的既有范式（同样 continue
        # 前先 message.end）。
        await _push_session_event(
            session_id, "message.end", {}, turn_id
        )
        return True

# ── AgentTeam 路由分支（路由模块 v1，2026-10-14）──
# 协作广场里**不 @ 任何人**的消息不再由主会话自跑 turn，
# 而是经 teams.route 决定响应成员：v1 默认路由给领队实例
#（领队实体化闭环：跟广场说话 = 跟领队对话，落在领队自己的
# 会话）。无领队实例的存量团队 → 路由返回空 → 旧语义不变。
    return False


async def handle_plaza_route(
    conn: WsConn, msg: dict, turn_id: str, user_text: str
) -> bool:
    session_id = conn.session_id
    from ....teams import store as _team_store
    from ....teams.deliver import deliver as _team_deliver
    from ....teams.route import route_targets

    _found_team = _team_store.team_of_session(session_id)
    if _found_team and _found_team[1] is None:
        _team, _ = _found_team
        _targets = route_targets(_team)
        for _iid in _targets:
            _receipt = await _team_deliver(
                str(_team.get("id")), _iid, user_text
            )
            await _push_session_event(
                session_id,
                "team.delivered",
                {
                    "agent_id": (
                        _team_store.get_member(
                            str(_team.get("id")), _iid
                        ) or {}
                    ).get("agent_id", ""),
                    "text": user_text,
                    "routed": True,
                    **_receipt,
                },
                turn_id,
            )
        if _targets:
            # P0-1：落盘一次（不是每个目标一次——用户只发了
            # 一条消息，广播给 N 个成员仍是一条）。
            from ....teams.deliver import record_plaza_message

            await record_plaza_message(
                session_id, user_text, turn_id,
                routed_to=",".join(str(i) for i in _targets),
            )
        if not _targets:
            # 空团队：广场无对话功能，提示先加成员（不起 turn）。
            from ....lang import t
            await _push_session_event(
                session_id,
                "notice",
                {"message": t(
                    "This team has no members yet — add a member first; "
                    "the main session is a group plaza, not a conversation.",
                    "团队还没有成员——先添加成员；主会话是群聊广场，"
                    "不承担对话功能。")},
                turn_id,
            )
        # 已路由给成员（或空团队提示）——主会话永不自跑 turn。
        # P0-1：同样必须收 message.end，否则 composer 锁死。
        await _push_session_event(
            session_id, "message.end", {}, turn_id
        )
        return True

    return False
