"""Steering + parked-interrupt answers: the steer message family.

Split out of ``session_ws`` (batch 4-2, structural only — no behavior
change): ``steer`` / ``steer.recall`` / ``permission_response`` /
``user_answer`` plus the parked-question probe they share. The
``_ANSWERED_STEERS`` dedup set stays module state, exactly as before.
"""

from __future__ import annotations

import time
import uuid

from .... import commands as _commands
from .... import server_shared as shared
from ....server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _TURN_TASKS,
    _ev,
    _log,
    _push_session_event,
)
from ... import sessions as sessions_mod
from ..turn import _attach_only_text, _prepare_steer_payload
from .handshake import WsConn

# 已被「steer→答案」转换过的 steer_id(去重)。客户端断线重发是常态设计
# (design §3.3),而转换路径不进 steer 队列(队列按 steer_id 去重),所以这里
# 自记一笔:重发的同 id steer 直接再答一次会让刚恢复的 turn 二次吞答案。
# 有界:达到上限整体清空——宁可极小概率重复,不冒无界增长的险。
_ANSWERED_STEERS: set[str] = set()




def _parked_user_question(session: dict, session_id: str) -> dict | None:
    """checkpointer 事实源:该 session 是否停在 ask_user 中断上。

    与 ws_open 的重发路径同源(get_pending_interrupt)。不用内存守卫
    (_PENDING_RESUME):守卫是进程内状态,重连/重启竞态下与 checkpointer
    脱节(2026-10-22 f5af1e9 实录:守卫为空但中断明确 parked,回答被静默
    丢弃,session 卡死)。无活任务的 parked turn 才算数——刚被回答、resume
    任务已在跑的窗口由调用方的任务检查排除。
    """
    try:
        graph = session.get("graph") if isinstance(session, dict) else None
        if graph is None or graph.checkpointer is None:
            return None
        pending = graph.checkpointer.get_pending_interrupt(
            {"configurable": {"thread_id": session_id}}
        )
    except Exception:
        return None
    for intr in pending or []:
        value = getattr(intr, "value", None) or intr
        if isinstance(value, dict) and value.get("kind") == "user_question":
            return value
    return None




async def handle_steer_recall(conn: WsConn, msg: dict) -> None:
    session_id = conn.session_id
    # ↑ 取回编辑（steering 取回协议 2026-11-05）：撤回尚未吸收的
    # steer。已吸收的不在 stash、无从撤回——真实删除集随事件回传，
    # 前端只弹回真正删掉的（用户按 ↑ 的瞬间恰被工具边界吸收的
    # 极窄竞态下，那条照常注入、composer 不出现重复文本）。
    _recall_ids = [str(x) for x in (msg.get("steer_ids") or []) if x]
    _removed = shared.steer_recall(session_id, _recall_ids)
    if _removed:
        await _push_session_event(
            session_id,
            "steer.recalled",
            {"steer_ids": _removed},
            _RUNNING_TURNS.get(session_id) or "",
        )
    return


async def handle_steer(conn: WsConn, msg: dict) -> None:
    ws = conn.ws
    session_id = conn.session_id
    session = conn.session
    # Same channel readonly gate as invoke — steering injects text
    # into a running turn, which is conversation by another name.
    if sessions_mod.is_channel_session(session):
        await ws.send_text(
            _ev(
                "error",
                {
                    "message": "channel sessions are read-only here; converse in the IM app",
                    "i18n_key": "stream.error_channel_readonly",
                },
            )
        )
        return
    # A message the user sent while a turn was running
    # (docs/steering-design.md §3.2). NOT a turn: it is stashed and
    # absorbed by the next `agent` superstep of the running turn, so
    # this branch never spawns a task, never registers in
    # _RUNNING_TURNS and NEVER touches _TURN_STOP — a set event here
    # would instantly kill the NEXT turn (the invoke branch above
    # warns about exactly that).
    steer_id = str(msg.get("steer_id") or uuid.uuid4())
    _steer_turn = msg.get("turn_id") or _RUNNING_TURNS.get(session_id) or ""
    plan = _commands.resolve_turn(msg, session)
    if plan.builtin_async is not None:
        # State-rewriting commands (/compact) can NEVER run here:
        # the steer branch by definition has a live turn whose
        # supersteps would race the rewrite. Tell the user to wait.
        await _push_session_event(
            session_id,
            "notice",
            {
                "message": f"/{plan.builtin_async} can only run after the current turn finishes",
                "i18n_key": "stream.command_only_after_turn",
                "params": {"command": plan.builtin_async},
            },
            _steer_turn,
        )
        await _push_session_event(
            session_id, "message.end", {}, _steer_turn
        )
        return
    if plan.builtin_reply is not None:
        # Built-in commands answer immediately and never queue — the
        # same rule `invoke` gets for free (its builtin branch sits
        # BEFORE the busy check) and the same as Claude Code's
        # immediate commands.
        await _push_session_event(
            session_id,
            "notice",
            {"message": plan.builtin_reply},
            _steer_turn,
        )
        await _push_session_event(
            session_id, "message.end", {}, _steer_turn
        )
        return
    # plan.mention_ctx / files_extra / agent_override are deliberately
    # DROPPED (design §3.1, decided): v1 steers plain text only, so an
    # @file in a mid-turn message reaches the model as literal text.
    # (Attachments are a separate, explicit channel since
    # steer-attachments-brief.md — they arrive as images/files.)
    steer_text = (plan.text or "").strip()
    _steer_imgs = [
        i
        for i in (msg.get("images") or [])
        if isinstance(i, dict) and i.get("data")
    ]
    _steer_files = [f for f in (msg.get("files") or []) if isinstance(f, dict)]
    # Attachments ALONE are a valid steer: a user may send only a
    # file / screenshot ("have a look at this"). Only a frame with
    # neither words nor attachments is dropped.
    if not steer_text and not _steer_imgs and not _steer_files:
        return
    # A turn parked at an interrupt counts as absorbable: the client
    # sends `steer` and THEN the resume message, and the resumed
    # segment's first agent_node drains it (design §3.4 — a permission
    # deny routes back to `agent`; an ask_user answer re-enters via
    # `tools → agent`).
    if steer_id in _ANSWERED_STEERS:
        # 该 steer 已被转换为答案(见下方 ask_user 分支):断线重发
        # 只补发 ack,绝不入队二次注入。
        await _push_session_event(
            session_id,
            "steer.answered",
            {"steer_id": steer_id},
            _steer_turn,
        )
        return
    _steer_task = _TURN_TASKS.get(session_id)
    if session_id not in _RUNNING_TURNS and (
        _steer_task is None or _steer_task.done()
    ):
        # Nothing to absorb into. The client gates on busy/parked, so
        # this is a race (the turn just ended): say so instead of
        # swallowing the message — the client falls back to `invoke`.
        await _push_session_event(
            session_id,
            "error",
            {
                "message": "No turn is currently running; please send again",
                "i18n_key": "stream.no_turn_running",
            },
            _steer_turn,
        )
        return
    # 停在 ask_user 上的 turn:纯文本 steer 按「输入即答案」
    # (design §3.4)直接当作自由文本回答恢复 turn,不入队。此前
    # 它只会进 steer 队列,而队列要等 resume 才被吸收,问题又要等
    # 回答才 resume——两边互相等,session 表现为永久卡死
    # (2026-10-22,f5af1e9 实录)。刻意不检查活任务:parked 出口
    # 后原任务还要收尾一会儿(真机上可到秒级),用它判「无人回答」
    # 会漏;极窄的「刚被卡片回答、resume 任务在跑」竞态由
    # _turn_lock 串行化,二次 resume 会以 resume_error 落日志,
    # 不会损伤图状态。
    if (
        steer_text
        and not _steer_imgs
        and not _steer_files
        and steer_id not in _ANSWERED_STEERS
    ):
        # 双信号判定:内存守卫优先(即时;test 里 steer 常在
        # suspension 落盘前 1ms 到达,只查盘会漏),checkpointer
        # 兜底(重启/重连后守卫丢失但中断明确 parked)。
        _guard_parked = (
            session_id in _PENDING_RESUME
            and _PENDING_KIND.get(session_id) == "user_question"
        )
        _parked_q = _parked_user_question(session, session_id)
        if _guard_parked or _parked_q is not None:
            _ANSWERED_STEERS.add(steer_id)
            if len(_ANSWERED_STEERS) > 256:
                _ANSWERED_STEERS.clear()
            _log.info(
                "steer_as_answer session=%s steer=%s turn=%s qid=%s text=%r",
                session_id,
                steer_id,
                _steer_turn,
                (_parked_q or {}).get("id"),
                steer_text[:120],
            )
            # 客户端据此收走队列条目;卡片本体随后随 ask_user 的
            # tool.end 折叠成回执,无需额外 UI 事件。
            await _push_session_event(
                session_id,
                "steer.answered",
                {"steer_id": steer_id, "question_id": (_parked_q or {}).get("id")},
                _steer_turn,
            )
            # resume_pending_interrupt 只认内存守卫;这里以
            # checkpointer 事实武装它,守卫丢失(重连/重启竞态)
            # 也能恢复——这正是本路径存在的意义。
            _PENDING_RESUME.add(session_id)
            _PENDING_KIND[session_id] = "user_question"
            conn.spawn_resume(
                {
                    "kind": "user_answer",
                    "answer": steer_text,
                    "option_index": None,
                    "skip": False,
                }
            )
            return
    try:
        _payload = await _prepare_steer_payload(
            steer_text,
            _steer_imgs,
            _steer_files,
            session.get("project_slug") or "default",
            session_id,
        )
    except Exception:
        # Attachments are best-effort; the user's words are not. Fall
        # back to the plain text (and the default intent when there is
        # none) rather than losing the message to a bad file.
        _log.exception(
            "steer_attach_failed session=%s steer=%s", session_id, steer_id
        )
        _payload = {
            "text": steer_text or _attach_only_text(),
            "content_blocks": None,
            "context_text": None,
            "files": [],
            "images": [],
        }
    # 未吸收注入合并（2026-11-05）：同 turn 连发修正型注入只落
    # 一条消息/一条带——并入前一条 pending 纯文本注入。带附件
    #（图片/文件/context）不合并，保持原独立语义。
    _merged_into = None
    if not _steer_imgs and not _steer_files and not _payload.get("context_text"):
        _merged_into = shared.steer_merge_pending(
            session_id, steer_text or "", _steer_turn
        )
    if _merged_into:
        _log.info(
            "steer_merged session=%s into=%s turn=%s text=%r",
            session_id, _merged_into, _steer_turn, (steer_text or "")[:120],
        )
    else:
        shared.steer_enqueue(
        session_id,
        {
            "steer_id": steer_id,
            "turn_id": _steer_turn,
            "text": _payload["text"],
            # Wall clock for the transcript label ("运行中注入 · 09:41").
            "injected_at": time.time(),
            # Model-facing payloads, built HERE (see
            # _prepare_steer_payload). Every path the entry travels —
            # drain, in-flight, restash-on-park, heal — moves the whole
            # dict, so the attachments survive all of them.
            "content_blocks": _payload["content_blocks"],
            "context_text": _payload["context_text"],
            # Compact chip summaries for REPLAY. Deliberately NOT
            # written to state["attached_files"]: that channel is
            # last-value-wins with no reducer and the replay hangs its
            # chips on the first user bubble of the session, so a
            # mid-turn write would attach them to the wrong message.
            "files": _payload["files"],
            "images": _payload["images"],
        },
    )
    _log.info(
        "steer_queued session=%s steer=%s turn=%s imgs=%d files=%d text=%r",
        session_id,
        steer_id,
        _steer_turn,
        len(_steer_imgs),
        len(_payload["files"]),
        _payload["text"][:120],
    )
    # Broadcast (not the invoking socket): the ack must also reach
    # the tab that RE-SENT this steer after a mid-enqueue reconnect
    # — the queue dedups by steer_id, so other tabs ignore it.
    await _push_session_event(
        session_id,
        "steer.accepted",
        {"steer_id": steer_id},
        _steer_turn,
    )


async def handle_permission_response(conn: WsConn, msg: dict) -> None:
    session_id = conn.session_id
    session = conn.session
    # The prompt broadcasts to every socket of the session (tabs),
    # so a second response can arrive after the first already
    # resumed the graph — ignore it instead of double-resuming.
    if session_id not in _PENDING_RESUME:
        # 远程会话兜底（2026-11-18 远程安全网 C1-P1）：远端
        # permission.request 已改为说明卡（不渲染可点按钮），正常
        # 不再有本类响应；若仍有（旧帧缓存/自定义客户端），留痕
        # 而非静默 continue——远程 turn 没有本地 interrupt 可 resume。
        if str(session.get("remote_peer") or "").strip():
            _log.warning(
                "permission_response_dropped_remote session=%s turn=%s",
                session_id, _RUNNING_TURNS.get(session_id) or "",
            )
        return
    # version.propose deliberately shares this resume channel, so
    # accept both — but NOT a parked question, whose payload shape
    # differs. Check WITHOUT popping: a mismatched message must
    # leave the parked interrupt answerable.
    if _PENDING_KIND.get(session_id) not in (
        "permission_request",
        "version_propose",
    ):
        return
    # 不在此处预 discard:resume_pending_interrupt 独占
    # check-and-pop(2026-10-22 f5af1e9 事故——这里先清一次,
    # 里面的守卫再查一次必然为空,所有桌面端 resume 全被静默拒绝)。
    decision = msg.get("decision", "deny")
    conn.spawn_resume({"decision": decision})


async def handle_user_answer(conn: WsConn, msg: dict) -> None:
    session_id = conn.session_id
    # An ask_user question is parked. A separate message type (not
    # an extra field on permission_response) because the resume
    # payload shape differs — overloading it would force the server
    # to introspect the checkpoint to guess which shape it owes.
    if session_id not in _PENDING_RESUME:
        # 静默丢弃曾让「卡死」无法排障(2026-10-22 f5af1e9):回答
        # 明明发出,服务端零痕迹。留下守卫快照定位脱节原因。
        _log.warning(
            "user_answer_dropped session=%s reason=no_pending_resume "
            "pending_kind=%r running=%r",
            session_id,
            _PENDING_KIND.get(session_id),
            _RUNNING_TURNS.get(session_id),
        )
        return
    # Parked on something else (or already answered): never
    # cross-resume one interrupt kind with another's payload. Peek
    # rather than pop so a stale message leaves the question live.
    if _PENDING_KIND.get(session_id) != "user_question":
        _log.warning(
            "user_answer_dropped session=%s reason=kind_mismatch pending_kind=%r",
            session_id,
            _PENDING_KIND.get(session_id),
        )
        return
    # 不在此处预 discard(同上 permission_response 分支):
    # resume_pending_interrupt 独占 check-and-pop,先清一次会让
    # 里面的守卫必查必空,回答被静默丢弃(f5af1e9 事故根因)。
    _ans = {"kind": "user_answer"}
    if msg.get("multi"):
        # 多问题（契约 v2）：逐题答案数组原样透传；ask_tools 的
        # _multi_receipt 逐题出回执。
        _ans["multi"] = True
        _ans["answers"] = [
            a for a in (msg.get("answers") or []) if isinstance(a, dict)
        ] or [{"answer": "", "option_index": None, "skip": True}]
    else:
        _ans.update(
            {
                "answer": str(msg.get("answer") or ""),
                "option_index": msg.get("option_index"),
                "skip": bool(msg.get("skip", False)),
            }
        )
    conn.spawn_resume(_ans)
