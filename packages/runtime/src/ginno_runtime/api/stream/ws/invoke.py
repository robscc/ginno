"""The ``invoke`` message: turn start + the command/mention routes that
short-circuit it (batch 4-2 split of ``session_ws``).

Structural move only — bodies are verbatim from the monolith; ``continue``
becomes ``return`` now that the branch is a handler. The heavy plan routes
(@session delivery, AgentTeam mention/plaza) live in :mod:`.routing`; this
module owns channel gating, builtin/mod commands, the busy check and the
``_turn_job`` spawn protocol.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

from .... import commands as _commands
from ....server_shared import (
    _COMPACTING,
    _MANAGED_TURNS,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _TURN_STOP,
    _TURN_TASKS,
    _ev,
    _log,
    _push_session_event,
    _turn_lock,
    spawn_bg,
)
from ....session_meta import _find_meta, _session_meta_patch
from ....subagent_plan import BACKGROUND_ASYNC_COMMANDS as _BACKGROUND_ASYNC_COMMANDS
from ... import sessions as sessions_mod
from ...sessions import _first_agent_id, _start_goal_driver
from ..engine import turn_error_fields
from ...messages_ui import skill_display_text
from ..turn import _run_stream
from .handshake import WsConn
from .routing import handle_plan_deliveries, handle_plaza_route, handle_team_mention


async def _touch_session_title(
    slug: str, session_id: str, session: dict, user_text: str, turn_id: str
) -> None:
    """Auto-title a session from its first user message; touch `updated`.

    While meta `title_auto` is set, the first non-empty user message becomes
    the title (single line, 40-char preview — same convention as goal-session
    titles) and a `session_title` event refreshes connected clients so the
    sidebar/TopBar rename live. The same patch arms `title_llm_pending` +
    `title_seed`: once this first turn completes, title_gen replaces the
    truncated placeholder with an LLM subject summary (a manual rename clears
    the flag and wins). Every turn — first or not — runs a (possibly
    empty) meta patch, which bumps `updated`; that timestamp is what the
    sidebar's day grouping sorts on.
    """
    found = _find_meta(session_id)
    meta = found[0] if found else {}
    text = (user_text or "").strip()
    # Slash-skill turns carry the SKILL.md injection as their text; title the
    # user's actual invocation ("/name request") instead of the raw wrapper.
    text = skill_display_text(text) or text
    if meta.get("title_auto", True) and text:
        title = text.replace("\n", " ")[:40]
        _session_meta_patch(
            slug,
            session_id,
            {
                "title": title,
                "title_auto": False,
                "title_llm_pending": True,
                "title_seed": text[:1200],
            },
        )
        session["title_auto"] = False
        await _push_session_event(session_id, "session_title", {"title": title}, turn_id)
    else:
        _session_meta_patch(slug, session_id, {})




async def reject_compacting_invoke(conn: WsConn, turn_id: str) -> bool:
    if conn.session_id not in _COMPACTING:
        return False
    # Only the originating socket has a placeholder for the rejected invoke.
    await conn.ws.send_text(_ev("notice", {
        "message": "Wait for compaction to finish before sending",
        "i18n_key": "stream.busy_compacting",
    }, turn_id))
    await conn.ws.send_text(_ev("message.end", {}, turn_id))
    return True


async def handle_invoke(conn: WsConn, msg: dict) -> None:
    ws = conn.ws
    session_id = conn.session_id
    session = conn.session
    config = conn.config
    # Channel readonly gate (2026-11-16): wecom/dingtalk sessions
    # converse on the IM side; the desktop is read-only. A UI-only
    # restriction was bypassable by any raw WS invoke — enforce it
    # here too. The adapters' own inbound path calls _run_stream
    # in-process and never passes through this gate.
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
    try:
        user_text = msg.get("message", "")
        # Per-turn trace id: prefer the client-supplied one (so the UUID
        # shown on the user bubble matches the one we log + put on every
        # event), else mint one. Forward it via config so _stream_graph
        # tags every emitted event + log line with it.
        turn_id = msg.get("turn_id") or str(uuid.uuid4())
        if await reject_compacting_invoke(conn, turn_id):
            return
        _imgs = msg.get("images") or []
        _log.info(
            "invoke session=%s turn=%s agent=%s imgs=%d text=%r",
            session_id,
            turn_id,
            msg.get("agent_id") or session.get("agent_id"),
            len([i for i in _imgs if isinstance(i, dict) and i.get("data")]),
            (user_text or "")[:120],
        )
        # Slash commands + @mentions → TurnPlan (docs §commands).
        plan = _commands.resolve_turn(msg, session)
        if plan.deliveries:
            # @session 投递短路（at-session-design §5.1）：prompt 属于目标会话，
            # 本会话不开 graph turn。目标 working → steer 注入；idle → 后台
            # headless turn。全部投递/回执/event 收尾在 routing 模块内完成。
            await handle_plan_deliveries(conn, msg, plan, turn_id, user_text)
            return
        if plan.builtin_async is not None:
            # Async builtin (e.g. /compact): awaits real server
            # work (LLM summary + thread-state rewrite) then
            # answers as a notice — still no graph turn. UNLIKE the
            # sync builtins this is busy-gated: a state rewrite
            # must never race a live turn's supersteps. Check BOTH
            # registries — _TURN_TASKS misses goal-driver turns
            # (they run inline under _turn_lock with no task
            # entry), _RUNNING_TURNS covers every streaming turn.
            _busy = _TURN_TASKS.get(session_id)
            if session_id in _RUNNING_TURNS or (
                _busy is not None and not _busy.done()
            ):
                await _push_session_event(
                    session_id,
                    "notice",
                    {
                        "message": f"A turn is already running; wait for it to finish before running /{plan.builtin_async}",
                        "i18n_key": "stream.busy_wait_command",
                        "params": {"command": plan.builtin_async},
                    },
                    turn_id,
                )
                await _push_session_event(
                    session_id, "message.end", {}, turn_id
                )
                # busy 拒绝也必须收尾：缺 message.end 时客户端把这次发送
                # 永远挂「运行中」（async 命令的 busy 分支有收尾，这里此前漏了）。
                await _push_session_event(session_id, "message.end", {}, turn_id)
                return
            if plan.builtin_async in _BACKGROUND_ASYNC_COMMANDS:
                # P3 contract 5 (P2 遗留): the subagent family's
                # decompose/spawn runs OFF the receive loop — the
                # LLM decompose takes tens of seconds and used to
                # block this socket's ping/stop/steer the whole
                # time. Every live socket gets message.end
                # immediately; the result notice (and, from the
                # split form, the subagent.plan broadcast inside
                # issue_plan) lands when the job finishes.
                # 2026-11-11: /compact 加入同一路径（异步化 P0）——
                # 60-90s 的摘要不再阻塞 receive loop，气泡秒收口，
                # 断线不再误标「未送达」。
                from ....subagent_plan import run_background_command

                if plan.builtin_async == "compact":
                    # Reserve before yielding so a second socket cannot start a
                    # turn or another compaction before the background job runs.
                    reservation = _COMPACTING[session_id] = {"stage": "summarizing"}
                    task = spawn_bg(run_background_command(
                        session, plan.builtin_async, plan.builtin_args, turn_id,
                        compaction_reservation=reservation,
                    ))

                    def release_unstarted_compaction(_task):
                        if _COMPACTING.get(session_id) is reservation:
                            _COMPACTING.pop(session_id)

                    task.add_done_callback(release_unstarted_compaction)
                    return
                spawn_bg(
                    run_background_command(session, plan.builtin_async, plan.builtin_args, turn_id)
                )
                await _push_session_event(
                    session_id, "message.end", {}, turn_id
                )
                return
            _reply_key: str | None = None
            _reply_params: dict | None = None
            try:
                _reply = await _commands.BUILTINS[
                    plan.builtin_async
                ].async_handler(
                    session.get("project_slug"),
                    session,
                    plan.builtin_args,
                )
            except Exception as _ce:
                _log.exception(
                    "builtin_async_failed name=%s session=%s",
                    plan.builtin_async, session_id,
                )
                _reply = (
                    f"/{plan.builtin_async} failed: "
                    f"{type(_ce).__name__}: {_ce}"
                )
                _reply_key = "stream.command_failed"
                _reply_params = {
                    "command": plan.builtin_async,
                    "error": f"{type(_ce).__name__}: {_ce}",
                }
            _notice_payload: dict = {"message": _reply}
            if _reply_key:
                _notice_payload["i18n_key"] = _reply_key
                _notice_payload["params"] = _reply_params
            # 会话级回复一律广播（与 turn 帧同一通道），绝不直发
            # 本 socket：命令可运行 60-90s（/compact 摘要 + 429 退避
            # 重试），期间客户端断线重连后旧 socket 已死——直发等于
            # 回复永久丢失，还炸 "send after websocket.close"，UI 永远
            # 卡「发送失败」（2026-10-12 实测，turn 帧早因 2026-08-05
            # 同类事故改广播，命令回复是漏网之鱼）。
            await _push_session_event(
                session_id, "notice", _notice_payload, turn_id
            )
            await _push_session_event(
                session_id, "message.end", {}, turn_id
            )
            return
        if plan.builtin_reply is not None:
            # Built-in command: reply directly, no graph turn, no agent
            # persistence, no checkpoint write (ephemeral by design).
            await _push_session_event(
                session_id, "notice", {"message": plan.builtin_reply}, turn_id
            )
            await _push_session_event(
                session_id, "message.end", {}, turn_id
            )
            return
        if plan.mod_command is not None:
            # Mod-registered slash command (claude-code-mods-design.md
            # §5.3 command.run): raise the event — the broker routes
            # it to the owning mod's runner — and deliver its reply
            # as a notice, exactly like a builtin. No graph turn.
            _mc_name, _mc_tail = plan.mod_command
            _mc_reply: str | None = None
            try:
                from ....mods.events import dispatch_command_run

                _mc_reply = await dispatch_command_run(
                    session_id, _mc_name, _mc_tail
                )
            except Exception:
                _log.exception("mod_command_failed name=%s session=%s", _mc_name, session_id)
            await _push_session_event(
                session_id,
                "notice",
                {
                    "message": _mc_reply
                    or f"/{_mc_name} executed by mod (no reply).",
                    # mod 无回复时的兜底文案走 key(message 是
                    # 旧 bundle 的英文回退);有回复时 reply 是
                    # mod 自己的成品文本,不翻译。
                    **(
                        {}
                        if _mc_reply
                        else {
                            "i18n_key": "chat.mods.commandNoReply",
                            "params": {"command": _mc_name},
                        }
                    ),
                },
                turn_id,
            )
            await _push_session_event(
                session_id, "message.end", {}, turn_id
            )
            return
        user_text = plan.text

        # ── AgentTeam 投递分支（agent-team-design.md §5.2 步骤 2）──
        # 在**团队主会话**里 @agent 不再是「本轮换 persona」，而是
        # 把消息投递给该成员自己的 session。放在 busy check 之前：
        # 投递目标是另一个 session，主会话自身忙不忙与它无关。
        # 非团队会话（found is None）走原路径，行为完全不变。
        if plan.agent_override:
            # ── AgentTeam 投递分支（agent-team-design.md §5.2 步骤 2）──
            # 在**团队主会话**里 @agent 不再是「本轮换 persona」，而是把消息
            # 投递给该成员自己的 session。有回执 = 已投递完（routing 内收
            # message.end）；无回执（非团队会话）落回原 persona 路径。
            if await handle_team_mention(conn, msg, plan, turn_id, user_text):
                return

        # ── AgentTeam 路由分支（路由模块 v1，2026-10-14）──
        # 协作广场里**不 @ 任何人**的消息不再由主会话自跑 turn，而是经
        # teams.route 决定响应成员。已路由（或空团队提示）= 已收口；非广场
        # 会话返回 False，走下方正常 turn 路径。
        if not plan.agent_override and user_text.strip():
            if await handle_plaza_route(conn, msg, turn_id, user_text):
                return

        if await reject_compacting_invoke(conn, turn_id):
            return

        # Busy check: a turn task is already live for this session
        # (multi-tab race — the frontend gates sends on `running`).
        # A goal turn running inline holds the lock but has no entry
        # here: our task simply queues on _turn_lock, as before.
        _busy = _TURN_TASKS.get(session_id)
        if _busy is not None and not _busy.done():
            await _push_session_event(
                session_id,
                "notice",
                {
                    "message": "A turn is already running; please wait for it to finish",
                    "i18n_key": "stream.busy_wait",
                },
                turn_id,
            )
            return
        if session_id in _MANAGED_TURNS:
            # A scheduler-managed turn (subagent brief / result
            # wake) is streaming on this session. It has no
            # _TURN_TASKS entry, so the busy check above can't see
            # it — without this guard the invoke would queue
            # invisibly on the turn lock for the rest of the
            # (possibly minutes-long) subagent turn and then fire
            # as a surprise new turn (2026-10-10 steering bug).
            # The composer routes mid-run input to `steer`
            # (subagent-design §6.2), so this only catches the
            # invoke↔turn.start race — say "busy", don't queue.
            await _push_session_event(
                session_id,
                "notice",
                {
                    "message": "A turn is already running; please wait for it to finish",
                    "i18n_key": "stream.busy_wait",
                },
                turn_id,
            )
            return
        # Pre-arm the cooperative stop signal BEFORE spawning the
        # task: a `stop` that lands before _RUNNING_TURNS is set
        # still finds the event; _stream_graph's setdefault never
        # clobbers it. Job's finally pops it — a stale set event
        # would kill the NEXT turn. setdefault (not assign): when
        # a goal turn runs inline it owns the current event —
        # overwriting it would orphan that waiter AND leave our
        # queued turn holding a pre-set event (instant suicide).
        _TURN_STOP.setdefault(session_id, asyncio.Event())

        # The turn runs as a background task so this receive loop
        # stays free to accept `stop` mid-turn (it used to await
        # _run_stream inline, which blocked ALL messages). The
        # loop vars (msg/plan/turn_id/user_text) are rebound by
        # later iterations, so bind them as defaults NOW.
        async def _turn_job(
            _msg=msg, _text=user_text, _tid=turn_id, _plan=plan
        ) -> None:
            # Captured BEFORE the finally pops the registry entry:
            # the completion gate needs the object to read is_set()
            # (did the user stop this turn?) after cleanup.
            _job_stop_evt = _TURN_STOP.get(session_id)
            try:
                await _touch_session_title(
                    session["project_slug"], session_id, session, _text, _tid
                )
                turn_agent = (
                    _plan.agent_override
                    or _msg.get("agent_id")
                    or session.get("agent_id")
                    or _first_agent_id()
                )
                if turn_agent != session.get("agent_id"):
                    session["agent_id"] = turn_agent
                    _session_meta_patch(
                        session["project_slug"], session_id, {"agent_id": turn_agent}
                    )
                turn_config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "agent_id": turn_agent,
                        "turn_id": _tid,
                        "user_text": _text or "",
                    },
                }
                async with _turn_lock(session_id):
                    await _run_stream(
                        None,  # events broadcast via _SESSION_WS
                        session["graph"],
                        turn_config,
                        _text,
                        session,
                        turn_agent,
                        images=_msg.get("images"),
                        files=(_msg.get("files") or []) + _plan.files_extra,
                        mention_context=_plan.mention_ctx,
                        skill_name=_plan.skill_name,
                    )
                # The turn may have created/resumed a goal (goal
                # tools) — (re)arm the continuation driver now that
                # we're idle. Success path only (a failed turn must
                # not re-arm the driver — pre-refactor semantics).
                _start_goal_driver(session_id)
            except Exception as e:
                # Lower-layer invoke failures (title touch, agent
                # resolution, stream setup) surface as an in-chat
                # error card instead of a silently dropped turn.
                _log.exception("invoke_error session=%s", session_id)
                await _push_session_event(
                    session_id,
                    "error",
                    {
                        **turn_error_fields(e),
                    },
                    _tid,
                )
            finally:
                # Drop the busy markers BEFORE post-turn housekeeping:
                # a new invoke arriving right after turn.stopped /
                # message.end must not hit a stale "busy". No awaits
                # between here and _stream_graph's own cleanup, so
                # this unwinds atomically from the loop's POV.
                if _TURN_TASKS.get(session_id) is asyncio.current_task():
                    _TURN_TASKS.pop(session_id, None)
                _TURN_STOP.pop(session_id, None)
                # (The steer stash is cleared by _stream_graph itself,
                # on the same not-parked gate — see the comment there.)
                # This job registered the turn at spawn (invoke
                # branch). If it dies before the graph ever starts,
                # nothing downstream will clear that entry and the
                # session reads "running" forever — every client probe
                # then waits on a stream that never comes. Identity-
                # checked, and skipped while a resume is pending: a
                # turn parked at an interrupt must STAY registered.
                if (
                    _RUNNING_TURNS.get(session_id) == _tid
                    and session_id not in _PENDING_RESUME
                ):
                    _RUNNING_TURNS.pop(session_id, None)
                # Subagent completion gate (subagent-design.md §5.6):
                # a subagent whose turn just settled re-evaluates
                # done / waiting / failed / stopped here. No-op for
                # main conversations (cheap in-memory short-circuit).
                try:
                    from ....subagent_scheduler import on_turn_settled

                    spawn_bg(on_turn_settled(session_id, _job_stop_evt, _tid))
                except Exception:
                    _log.exception(
                        "subagent_settle_hook_failed session=%s", session_id
                    )

        task = asyncio.create_task(_turn_job())
        _TURN_TASKS[session_id] = task
        # Register the turn as running the moment the invoke is
        # ACCEPTED — not when the graph happens to start. _stream_graph
        # only sets this ~250ms later (title touch, agent resolution,
        # world-state sync, MCP connect), and a client `turn_state`
        # probe answered inside that window says "not running": the
        # frontend then reconciles a brand-new session's just-sent
        # message against a /history that has no checkpoint yet and
        # stamps the bubble 「发送失败：连接中断，未送达」 although the
        # turn is fine (2026-09-21, session 52d54d…). _stream_graph
        # still owns the pop (it deliberately keeps it for a turn
        # parked at an interrupt); _turn_job drops it if the turn dies
        # before ever reaching the graph.
        _RUNNING_TURNS[session_id] = turn_id
        # Identity-checked pop: never delete a SUCCESSOR task that a
        # later invoke stored under the same session id.
        def _turn_done(t, _sid=session_id) -> None:
            if _TURN_TASKS.get(_sid) is t:
                _TURN_TASKS.pop(_sid, None)
            if not t.cancelled():
                exc = t.exception()  # 取回：未取回的异常可被彻底静默
                if exc is not None:
                    _log.error("turn_task_dead session=%s err=%r", _sid, exc)

        task.add_done_callback(_turn_done)
    except Exception as e:
        # Inline-phase failure (command/mention resolution, task
        # spawn) — broadcast: the turn never started, and the
        # invoking socket may already have reconnected.
        _log.exception("invoke_error session=%s", session_id)
        await _push_session_event(
            session_id, "error", {**turn_error_fields(e)}
        )
