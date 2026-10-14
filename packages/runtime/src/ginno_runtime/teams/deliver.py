"""@ 投递 + 回帖（agent-team-design.md §5.2 / §5.3.3）。

把「@agent」从**本轮换 persona** 升级为**跨 session 消息投递**：用户在团队
主会话里 @ 某成员，消息进该成员自己的 session（独立上下文/记忆/工具白名单），
成员跑完把**回帖**写回主会话 checkpoint。

三条硬纪律（全部来自 herdr 的生产教训，§5.3.3）：

1. **blocked 时原子拒绝**——一个字都不发。herdr v0.8.2：*"Prompting a
   recognized approval/question dialog now fails atomically with
   ``agent_blocked``: no text and no Enter are sent."* 把消息塞进队列会让它
   和那张卡互相覆盖，且用户以为发出去了。
2. **超时 ≠ 未投递**——回执三态 ``delivered / stalled / failed``。herdr v0.9.0：
   *"A timeout or ``agent_prompt_stalled`` still does not prove that no input
   was sent, so read the agent before retrying to avoid a duplicate prompt."*
   ``stalled`` 时**不提供一键重发**，要求先看成员工作台。
3. **成功要有可验证证据**——以「steer 队列里有这个 steer_id」或「成员
   checkpoint 里出现了这条 HumanMessage」为凭据，不是「函数没抛异常」。

回帖是**结构化消息对象**而非对话内容注入：走 ``AIMessage.additional_kwargs
["agent_id"]``（前端已有 HandoffDivider 渲染），全文留在成员 session，主会话
只落摘要——既不撑爆主会话上下文，又让「谁说了什么」可检索（§5.2 要点 2）。
"""

from __future__ import annotations

import asyncio
import time
import uuid

from langchain_core.messages import AIMessage

from ..server_shared import (
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _TURN_TASKS,
    _log,
    _push_session_event,
    spawn_bg,
    steer_enqueue,
)
from . import status as team_status
from . import store

# 回帖摘要上限：超过就截断（全文永远在成员 session，点「展开全文」跳过去）。
# 与 subagent 的 SUMMARY_MAX_CHARS 同量级，但更短——回帖是对话消息不是报告。
REPLY_MAX_CHARS = 1200

# 等成员 turn 结束的上限。超时不代表失败（纪律 2）→ 回执 stalled。
DELIVER_WAIT_S = 1800.0

# 投递后轮询 turn 是否结束的间隔。用轮询而非 await task：steer 路径下 turn
# 任务不归我们所有（是用户那个 WS 起的），拿不到可靠的 task 句柄。
_POLL_S = 0.4

# 并发闸（§5.6）：team_id → 排队投递（FIFO）。满了不拒收——排队，turn
# 结束自动 drain（原型语义「队列中还有 1 条待处理投递」）。进程内态：
# 重启丢失可接受（steer stash 同款边界）。
_QUEUES: dict[str, list[dict]] = {}


def _running_members(team: dict) -> int:
    """团队里此刻有活 turn 的成员数（按成员计，不按 turn 计——一个成员
    同时只有一条活跃会话在跑，多会话成员也以「任一会话在跑」为 1）。"""
    n = 0
    for m in (team or {}).get("members") or []:
        sids = {*(m.get("session_ids") or []), m.get("session_id") or ""}
        if any(_is_live(s) for s in sids if s):
            n += 1
    return n


def clear_queue(team_id: str) -> None:
    _QUEUES.pop(team_id, None)


async def _drain_queue(team_id: str) -> None:
    """有成员 turn 结束后尝试放行排队投递。逐条走完整 deliver()（成员可能
    已被移除/已 blocked/已自己在跑——各分支 deliver 自己会处理）。"""
    queue = _QUEUES.get(team_id)
    if not queue:
        return
    team = store.get_team(team_id)
    if team is None:
        _QUEUES.pop(team_id, None)
        return
    while queue and _running_members(team) < store.MAX_CONCURRENT_RUNNING:
        entry = queue.pop(0)
        # 成员已被移除 → 丢弃这条（failed 语义，无人可通知也不再有回执通道）。
        if store.get_member(team_id, entry["instance_id"]) is None:
            continue
        _log.info(
            "team_deliver_drained team=%s instance=%s queued=%d",
            team_id, entry["instance_id"], len(queue),
        )
        # 走完整 deliver：此刻额度已空，正常路径是新开 turn。
        await deliver(team_id, entry["instance_id"], entry["text"],
                     client=entry.get("client") or "main")
        team = store.get_team(team_id) or team
    if not queue:
        _QUEUES.pop(team_id, None)


class DeliveryError(Exception):
    """投递失败（带机器可读的 reason，供 API 原样返回）。"""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message


def _is_live(session_id: str) -> bool:
    if session_id in _RUNNING_TURNS:
        return True
    task = _TURN_TASKS.get(session_id)
    return task is not None and not task.done()


async def _await_turn_end(session_id: str, timeout_s: float) -> bool:
    """等该 session 的 turn 结束。True = 确实结束了；False = 超时。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if not _is_live(session_id):
            return True
        await asyncio.sleep(_POLL_S)
    return not _is_live(session_id)


async def _final_text(slug: str, session_id: str, team_id: str = "") -> str:
    """成员 checkpoint 里最后一条 assistant 文本 = 回帖正文。

    与 subagent 的 ``_final_report_text`` 同一取法（读 FileCheckpointer 的最后
    一条 AIMessage），但独立实现：team 不依赖 subagent_scheduler 的私有函数。
    """
    from ..checkpointer import FileCheckpointer
    from ..graph import text_of_content

    try:
        ckpt = FileCheckpointer(slug)
        tup = ckpt.get_tuple({"configurable": {"thread_id": session_id}})
        values = (getattr(tup, "checkpoint", None) or {}).get("channel_values") or {}
        for m in reversed(list(values.get("messages") or [])):
            if isinstance(m, AIMessage):
                text = text_of_content(m.content).strip()
                if text:
                    return text
    except Exception:  # noqa: BLE001 — 取不到正文不该炸投递
        _log.exception("team_reply_text_failed team=%s session=%s", team_id or "?", session_id)
    return ""


def _truncate(text: str) -> tuple[str, bool]:
    text = (text or "").strip()
    if len(text) <= REPLY_MAX_CHARS:
        return text, False
    return text[:REPLY_MAX_CHARS].rstrip() + "…", True


async def post_reply(
    team_id: str,
    instance_id: str,
    text: str = "",
    truncated: bool = False,
) -> bool:
    """把成员的产出作为**回帖**写进主会话 checkpoint（§5.2 步骤 6）。

    走 ``graph.aupdate_state(..., as_node="agent")``——与 ``_heal_interrupted_turn``
    同一范式（turn 外写 checkpoint 的既有先例），用派生 turn_id 避开
    FileCheckpointer 对已放弃 turn 的拒绝。

    绝不抛异常：回帖失败只记日志，成员的成果仍完整保留在它自己的 session 里。
    """
    team = store.get_team(team_id)
    if team is None:
        return False
    member = store.get_member(team_id, instance_id)
    if member is None:
        return False
    main_sid = str(team.get("main_session_id") or "")
    slug = str(team.get("project_slug") or "default")
    agent_id = str(member.get("agent_id") or "")

    body = text if text else await _final_text(slug, member.get("session_id") or "", team_id=team_id)
    body, cut = _truncate(body)
    truncated = truncated or cut
    if not body:
        body = "（本轮无文本产出，详见成员工作台）"

    session = _SESSIONS.get(main_sid)
    if session is None or session.get("graph") is None:
        _log.warning("team_reply_no_session team=%s main=%s", team_id, main_sid)
        return False

    reply_id = f"team-reply-{instance_id}-{uuid.uuid4().hex[:8]}"
    msg = AIMessage(
        content=body,
        id=reply_id,
        additional_kwargs={
            # 前端凭这个渲染「谁说的」（graph.py 同一约定）。
            "agent_id": agent_id,
            # team 专属标记：区分「本会话内 agent 交接」与「成员回帖」，
            # 前端据此折叠成回帖卡而不是普通气泡。
            "ginno_team_reply": {
                "team_id": team_id,
                "instance_id": instance_id,
                "member_session_id": member.get("session_id"),
                "display_name": member.get("display_name") or agent_id,
                "truncated": truncated,
            },
        },
    )
    try:
        config = {
            "configurable": {
                "thread_id": main_sid,
                "project_slug": slug,
                "agent_id": agent_id,
                # 派生 id：原始 turn 可能已在 ABANDONED_TURNS 里被拒。
                "turn_id": f"{reply_id}:reply",
            }
        }
        await session["graph"].aupdate_state(
            config, {"messages": [msg], "pending_tool_calls": []}, as_node="agent"
        )
    except Exception:  # noqa: BLE001
        _log.exception("team_reply_write_failed team=%s main=%s", team_id, main_sid)
        return False

    # 广播给主会话的所有 socket（断线重连不丢，2026-10-05 起是会话级广播）。
    await _push_session_event(
        main_sid,
        "team.reply",
        {
            "team_id": team_id,
            "instance_id": instance_id,
            "agent_id": agent_id,
            "display_name": member.get("display_name") or agent_id,
            "message_id": reply_id,
            "text": body,
            "truncated": truncated,
            "member_session_id": member.get("session_id"),
        },
    )
    _log.info(
        "team_reply_posted team=%s instance=%s main=%s len=%d truncated=%s",
        team_id, instance_id, main_sid, len(body), truncated,
    )
    return True


async def _run_member_turn(team_id: str, instance_id: str, text: str) -> None:
    """跑成员的一个 turn，结束后回帖 + 置 done。

    复用 subagent 调度器的 ``_run_managed_turn``（它已经把 managed turn 的
    全部脏活做对了：清 LLM run context 防串线、_MANAGED_TURNS 标记、turn lock、
    stop 事件、settle 钩子）。team 只在其外面套「等结束 → 回帖」。
    """
    from ..subagent_scheduler import _run_managed_turn  # lazy: 避免 import 环

    team = store.get_team(team_id)
    member = store.get_member(team_id, instance_id)
    if team is None or member is None:
        return
    sid = str(member.get("session_id") or "")
    stop_evt = _TURN_STOP_setdefault(sid)
    store.patch_member(team_id, instance_id, {
        "status": "working",
        "status_detail": "正在处理投递",
    })
    await _broadcast_status(team_id)
    try:
        await _run_managed_turn(sid, text, None, stop_evt)
    except Exception:  # noqa: BLE001
        _log.exception("team_member_turn_failed team=%s instance=%s", team_id, instance_id)
    # turn 结束后回帖（无论成败都回，失败也要让主会话知道）。
    await post_reply(team_id, instance_id)
    # 置 done（未读）——用户看过工作台才转 idle（§3.3 收件箱语义）。
    # last_completed 是 done 的判据（status.py 步骤 5），必须在这里落。
    store.patch_member(team_id, instance_id, {
        "status": "done", "last_completed": time.time(),
    })
    team_status.refresh_team(team_id)
    await _broadcast_status(team_id)
    # 并发闸：腾出的额度放行下一条排队投递。
    await _drain_queue(team_id)


def _TURN_STOP_setdefault(session_id: str):
    from ..server_shared import _TURN_STOP

    return _TURN_STOP.setdefault(session_id, asyncio.Event())


async def _broadcast_status(team_id: str, client: str = "main") -> None:
    """推 ``team.changed``：成员面板与总览页共用这一个事件（§5.3）。"""
    team = store.get_team(team_id)
    if team is None:
        return
    await _push_session_event(
        str(team.get("main_session_id") or ""),
        "team.changed",
        {"team_id": team_id, "team": team_status.team_payload(team, client)},
    )


async def deliver(
    team_id: str,
    instance_id: str,
    text: str,
    client: str = "main",
    wait: bool = True,
) -> dict:
    """投递一条消息给成员实例。返回三态回执（纪律 2）。

    ``{ok, receipt: delivered|stalled|failed, reason?, instance_id, session_id}``

    - ``delivered`` — 有可验证证据：steer 队列里有 steer_id，或成员 turn 已由
      我们启动。
    - ``stalled``   — 发出去了但成员没进入 working（可能已生效）。**不要自动
      重发**，先看成员工作台。
    - ``failed``    — 确定没发（blocked 原子拒绝 / 成员不存在 / 无 session）。
    """
    text = (text or "").strip()
    if not text:
        return {"ok": False, "receipt": "failed", "reason": "empty_text",
                "error": "投递内容不能为空"}

    team = store.get_team(team_id)
    if team is None:
        return {"ok": False, "receipt": "failed", "reason": "unknown_team",
                "error": f"未知团队 {team_id}"}
    member = store.get_member(team_id, instance_id)
    if member is None:
        return {"ok": False, "receipt": "failed", "reason": "unknown_member",
                "error": f"团队里没有成员实例 {instance_id}"}

    sid = str(member.get("session_id") or "")
    if not sid:
        return {"ok": False, "receipt": "failed", "reason": "no_session",
                "error": "该成员没有绑定 session"}

    # ── 纪律 1：blocked 时原子拒绝，一个字都不发 ──
    if sid in _PENDING_RESUME:
        _log.info(
            "team_deliver_refused_blocked team=%s instance=%s", team_id, instance_id
        )
        return {
            "ok": False,
            "receipt": "failed",
            "reason": "agent_blocked",
            "instance_id": instance_id,
            "session_id": sid,
            "error": (
                "该成员正停在权限/提问确认上，投递已原子拒绝（未发送任何内容）。"
                "请先回答它的问题。"
            ),
        }

    slug = str(team.get("project_slug") or "default")

    # ── 成员在跑 → steer（不打断，下个超步边界吸收）──
    if _is_live(sid):
        steer_id = f"team-{instance_id}-{uuid.uuid4().hex[:8]}"
        steer_enqueue(sid, {
            "steer_id": steer_id,
            "turn_id": _RUNNING_TURNS.get(sid) or "",
            "text": text,
            "injected_at": time.time(),
            "extra_kwargs": {"ginno_team_delivery": {
                "team_id": team_id, "instance_id": instance_id,
            }},
        })
        # 纪律 3：证据 = steer 队列里确实有这个 id。
        from ..server_shared import _STEER_STASH

        queued = any(
            e.get("steer_id") == steer_id for e in (_STEER_STASH.get(sid) or [])
        )
        receipt = "delivered" if queued else "stalled"
        _log.info(
            "team_deliver_steered team=%s instance=%s steer=%s receipt=%s",
            team_id, instance_id, steer_id, receipt,
        )
        if wait:
            # 等这个 turn 结束后回帖（steer 的 turn 不归我们所有，只能等）。
            spawn_bg(_reply_after_current_turn(team_id, instance_id, sid))
        return {"ok": receipt == "delivered", "receipt": receipt,
                "instance_id": instance_id, "session_id": sid,
                "steer_id": steer_id, "channel": "steer"}

    # ── 并发闸（§5.6）：满员排队，turn 结束自动 drain ──
    if _running_members(team) >= store.MAX_CONCURRENT_RUNNING:
        _QUEUES.setdefault(team_id, []).append({
            "instance_id": instance_id, "text": text, "client": client,
        })
        store.patch_member(team_id, instance_id, {
            "status_detail": "排队中（团队并发已满）",
        })
        await _broadcast_status(team_id, client)
        _log.info(
            "team_deliver_queued team=%s instance=%s running=%d queue=%d",
            team_id, instance_id, store.MAX_CONCURRENT_RUNNING,
            len(_QUEUES.get(team_id) or []),
        )
        # 证据 = 队列里确实有这条（纪律 3 的队列版）。
        return {"ok": True, "receipt": "delivered", "channel": "queue",
                "instance_id": instance_id, "session_id": sid,
                "queued": len(_QUEUES.get(team_id) or [])}

    # ── 成员空闲 → 起一个 managed turn ──
    session = _SESSIONS.get(sid)
    if session is None:
        from ..api.sessions import _ensure_session  # lazy: cycle

        session = _ensure_session(sid)
    if session is None:
        return {"ok": False, "receipt": "failed", "reason": "session_unavailable",
                "error": f"成员 session {sid} 不可用"}

    spawn_bg(_run_member_turn(team_id, instance_id, text))
    # 证据 = turn 确实起来了（给 spawn_bg 一点时间登记 _RUNNING_TURNS）。
    started = await _await_turn_start(sid)
    receipt = "delivered" if started else "stalled"
    _log.info(
        "team_deliver_new_turn team=%s instance=%s session=%s receipt=%s slug=%s",
        team_id, instance_id, sid, receipt, slug,
    )
    return {"ok": receipt == "delivered", "receipt": receipt,
            "instance_id": instance_id, "session_id": sid, "channel": "turn"}


async def _await_turn_start(session_id: str, timeout_s: float = 5.0) -> bool:
    """等 managed turn 登记进 _RUNNING_TURNS（投递证据，纪律 3）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _is_live(session_id):
            return True
        await asyncio.sleep(0.05)
    return _is_live(session_id)


async def _reply_after_current_turn(team_id: str, instance_id: str, sid: str) -> None:
    """steer 路径的回帖：等当前 turn 结束，然后把成员最后的产出回帖到主会话。"""
    ended = await _await_turn_end(sid, DELIVER_WAIT_S)
    if not ended:
        _log.warning(
            "team_reply_stalled_timeout team=%s instance=%s session=%s",
            team_id, instance_id, sid,
        )
        return
    await post_reply(team_id, instance_id)
    store.patch_member(team_id, instance_id, {
        "status": "done", "last_completed": time.time(),
    })
    team_status.refresh_team(team_id)
    await _broadcast_status(team_id)
    # 并发闸：steer 的 turn 结束同样腾额度。
    await _drain_queue(team_id)


async def resolve_agent_mention(
    main_session_id: str, agent_id: str, text: str
) -> dict | None:
    """@agent 在**团队主会话**里的投递解析（§5.2 步骤 2）。

    返回 None 表示「这不是团队主会话，走旧的 persona 覆盖语义」——普通会话
    行为完全不变（向后兼容，零回归）。
    """
    found = store.team_of_session(main_session_id)
    if found is None:
        return None
    team, member = found
    if member is not None:
        # 在成员工作台里 @ 别的 agent：本期不支持成员间直连（§5.2 要点 3，
        # 一切经主会话，可解释性优先）。按旧语义处理。
        return None
    team_id = str(team.get("id"))
    target = store.member_of_agent(team_id, agent_id)
    if target is None:
        return {
            "ok": False,
            "receipt": "failed",
            "reason": "not_a_member",
            "error": f"「{agent_id}」不是本团队成员。@ 只能投递给已加入的成员。",
        }
    return await deliver(team_id, str(target.get("instance_id")), text)
