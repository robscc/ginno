"""Session WebSocket endpoint: the receive loop and the title touch.

Split out of the former single-module ``api/stream.py`` (pure structural
move — no behavior change). The package ``__init__`` re-exports the whole
historical namespace and mirrors patch writes into the submodules, so
``ginno_runtime.api.stream.X`` keeps working as the import/patch surface.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ... import commands as _commands
from ... import files as files_mod
from ... import server_shared as shared
from ...checkpointer import ABANDONED_TURNS
from ...goals import store as goal_store
from ...server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _TURN_STOP,
    _TURN_TASKS,
    _ev,
    _log,
    _push_session_event,
    _turn_lock,
    spawn_bg,
)
from ...session_meta import _find_meta, _session_meta_patch
from ...subagent_plan import BACKGROUND_ASYNC_COMMANDS as _BACKGROUND_ASYNC_COMMANDS
from ..messages_ui import skill_display_text
from ..sessions import _emit_goal_event, _ensure_session, _first_agent_id, _start_goal_driver
from .engine import _stop_parked_turn, _stream_graph
from .turn import _ATTACH_ONLY_TEXT, _prepare_steer_payload, _run_resume, _run_stream

router = APIRouter()

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


@router.websocket("/api/ws/sessions/{session_id}")
async def session_ws(ws: WebSocket, session_id: str) -> None:
    session = _ensure_session(session_id)
    if not session:
        await ws.accept()
        await ws.send_text(_ev("error", {"message": f"unknown session: {session_id}"}))
        await ws.close()
        return

    await ws.accept()
    _SESSION_WS.setdefault(session_id, []).append(ws)
    _log.info("ws_open session=%s agent=%s", session_id, session.get("agent_id"))
    graph = session["graph"]
    config = {"configurable": {"thread_id": session_id, "project_slug": session["project_slug"]}}

    # File watcher (docs §7.5): stat the session's registered files every 5s.
    # A changed mtime → preview.invalidate (the UI refreshes if that file is
    # open) + a stale badge on the artifact (cleared on next preview fetch).
    # Sends share _ws_lock so watcher frames never interleave with turn frames.
    _ws_lock = asyncio.Lock()
    _watch_stop = asyncio.Event()

    async def _file_watcher() -> None:
        reg = files_mod.get_registry(session["project_slug"])
        while not _watch_stop.is_set():
            try:
                await asyncio.wait_for(_watch_stop.wait(), timeout=5)
                break  # stop set
            except TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            artifacts_dirty = False
            try:
                for e in reg.list_session(session_id):
                    try:
                        m = Path(e["path"]).stat().st_mtime
                    except OSError:
                        continue
                    if m != e.get("mtime", 0):
                        e["mtime"] = m
                        if not e.get("stale"):
                            reg.mark_stale(e["id"], True)
                            artifacts_dirty = True
                        async with _ws_lock:
                            await ws.send_text(
                                _ev(
                                    "preview.invalidate",
                                    {"file_id": e["id"], "reason": "mtime"},
                                )
                            )
                if artifacts_dirty:
                    async with _ws_lock:
                        await ws.send_text(_ev("artifacts.changed", {}))
            except Exception:
                continue

    _watcher_task = asyncio.create_task(_file_watcher())

    # Re-emit any permission interrupt left pending from a previous connection:
    # the graph pauses at permission_node awaiting a resume, so a reconnect or a
    # session switch mid-permission would otherwise orphan the turn (the prompt
    # is gone and there is no way to resume). The payload mirrors the
    # __interrupt__ handling in _run_stream below, so the client's existing
    # permission.request handler applies unchanged.
    try:
        # Read the parked interrupt straight off the checkpointer rather than
        # via graph.aget_state().tasks[].interrupts: that route needs pending
        # writes to be surfaced, and even then misses every interrupt raised
        # inside the tools node (ask_user, workflow_propose_edit) because
        # langgraph commits one extra write-less checkpoint on top of it. See
        # FileCheckpointer.get_pending_interrupt.
        _pending = graph.checkpointer.get_pending_interrupt(config)
        for intr in _pending or []:
            value = getattr(intr, "value", None) or intr
            if not isinstance(value, dict):
                continue
            kind = value.get("kind")
            payload: dict | None = None
            if kind == "permission_request":
                payload = {"tool": value.get("tool"), "args": value.get("args")}
                event = "permission.request"
            elif kind == "version_propose":
                # A workflow-dev diff proposal pending at disconnect: re-show it
                # so the turn isn't orphaned (same resume channel as permission).
                payload = {
                    "workflow_id": value.get("workflow_id"),
                    "from_version": value.get("from_version"),
                    "diff": value.get("diff"),
                    "rationale": value.get("rationale"),
                }
                event = "version.propose"
            elif kind == "user_question":
                # An ask_user question parked at disconnect. The payload carries
                # the tool_call id, which is what lets the client MERGE this
                # re-emit into the block it already rebuilt from history instead
                # of showing the question twice.
                payload = {
                    "id": value.get("id"),
                    "question": value.get("question", ""),
                    "header": value.get("header", ""),
                    "options": value.get("options") or [],
                    "allow_free_text": value.get("allow_free_text", True),
                }
                event = "user.question"
            if payload is None:
                continue
            # Re-arm the resume guard: after a runtime restart the in-memory
            # flags are gone even though the interrupt persists in the file.
            _PENDING_RESUME.add(session_id)
            _PENDING_KIND[session_id] = kind
            _RUNNING_TURNS.setdefault(session_id, "")
            await ws.send_text(_ev(event, payload))
    except Exception:
        # introspecting resume state must never stop the socket from opening
        pass

    # Cross-restart goal resume (design §4.3.3): an active goal re-arms its
    # continuation driver as soon as the session is loaded again.
    _start_goal_driver(session_id)

    def _spawn_resume(resume_value: dict) -> None:
        """Resume the parked graph as its own task.

        Resume is a turn too — it must not block the receive loop (a `stop`
        during a resumed turn has to get through). Shared by every interrupt
        kind; only the resume payload differs.
        """
        # Resume under the agent that was active when the interrupt fired.
        resume_agent = session.get("agent_id") or _first_agent_id()
        resume_config = {
            **config,
            "configurable": {**config["configurable"], "agent_id": resume_agent},
        }
        _TURN_STOP.setdefault(session_id, asyncio.Event())
        # Same capture-before-cleanup protocol as _turn_job's stop event above.
        _resume_stop_evt = _TURN_STOP.get(session_id)

        async def _resume_job(_cfg=resume_config, _val=resume_value) -> None:
            try:
                # Serialize behind _turn_lock: the paused turn's task may still
                # be draining its stream generator, and the suspension
                # checkpoint is only flushed during that drain. Resuming before
                # it lands reads the PRE-interrupt checkpoint and re-runs the
                # interrupted tool from scratch (it interrupts again — the turn
                # never advances). The lock is held until the turn's
                # _run_stream returns, i.e. after the drain. (The old inline
                # receive loop got this serialization for free; task-ified
                # turns need it explicitly.)
                async with _turn_lock(session_id):
                    await _run_resume(None, session["graph"], _cfg, _val)
                _start_goal_driver(session_id)  # success path only
            except Exception as e:
                _log.exception("resume_error session=%s", session_id)
                await _push_session_event(
                    session_id, "error", {"message": f"{type(e).__name__}: {e}"}
                )
            finally:
                if _TURN_TASKS.get(session_id) is asyncio.current_task():
                    _TURN_TASKS.pop(session_id, None)
                _TURN_STOP.pop(session_id, None)
                # Subagent completion gate for the resumed segment (a resumed
                # subagent turn finishing is a turn settling like any other).
                if session_id not in _PENDING_RESUME:
                    try:
                        from ...subagent_scheduler import on_turn_settled

                        spawn_bg(
                            on_turn_settled(
                                session_id,
                                _resume_stop_evt,
                                str((_cfg.get("configurable") or {}).get("turn_id") or ""),
                            )
                        )
                    except Exception:
                        _log.exception(
                            "subagent_settle_hook_failed session=%s", session_id
                        )

        task = asyncio.create_task(_resume_job())
        _TURN_TASKS[session_id] = task
        task.add_done_callback(
            lambda t: _TURN_TASKS.pop(session_id, None)
            if _TURN_TASKS.get(session_id) is t
            else None
        )

    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_text(_ev("error", {"message": "invalid JSON"}))
                continue

            kind = msg.get("type")
            if kind == "invoke":
                try:
                    user_text = msg.get("message", "")
                    # Per-turn trace id: prefer the client-supplied one (so the UUID
                    # shown on the user bubble matches the one we log + put on every
                    # event), else mint one. Forward it via config so _stream_graph
                    # tags every emitted event + log line with it.
                    turn_id = msg.get("turn_id") or str(uuid.uuid4())
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
                            await ws.send_text(
                                _ev(
                                    "notice",
                                    {"message": f"当前有回合正在进行，请等待其结束再执行 /{plan.builtin_async}"},
                                    turn_id,
                                )
                            )
                            await ws.send_text(_ev("message.end", {}, turn_id))
                            continue
                        if plan.builtin_async in _BACKGROUND_ASYNC_COMMANDS:
                            # P3 contract 5 (P2 遗留): the subagent family's
                            # decompose/spawn runs OFF the receive loop — the
                            # LLM decompose takes tens of seconds and used to
                            # block this socket's ping/stop/steer the whole
                            # time. The invoking socket gets message.end
                            # immediately; the result notice (and, from the
                            # split form, the subagent.plan broadcast inside
                            # issue_plan) lands when the job finishes.
                            from ...subagent_plan import run_background_command

                            spawn_bg(
                                run_background_command(
                                    session,
                                    plan.builtin_async,
                                    plan.builtin_args,
                                    turn_id,
                                )
                            )
                            await ws.send_text(_ev("message.end", {}, turn_id))
                            continue
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
                                f"/{plan.builtin_async} 执行失败："
                                f"{type(_ce).__name__}: {_ce}"
                            )
                        await ws.send_text(_ev("notice", {"message": _reply}, turn_id))
                        await ws.send_text(_ev("message.end", {}, turn_id))
                        continue
                    if plan.builtin_reply is not None:
                        # Built-in command: reply directly, no graph turn, no agent
                        # persistence, no checkpoint write (ephemeral by design).
                        await ws.send_text(
                            _ev("notice", {"message": plan.builtin_reply}, turn_id)
                        )
                        await ws.send_text(_ev("message.end", {}, turn_id))
                        continue
                    user_text = plan.text
                    # Busy check: a turn task is already live for this session
                    # (multi-tab race — the frontend gates sends on `running`).
                    # A goal turn running inline holds the lock but has no entry
                    # here: our task simply queues on _turn_lock, as before.
                    _busy = _TURN_TASKS.get(session_id)
                    if _busy is not None and not _busy.done():
                        await ws.send_text(
                            _ev("notice", {"message": "当前有回合正在进行，请等待其结束"}, turn_id)
                        )
                        continue
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
                                session_id, "error", {"message": f"{type(e).__name__}: {e}"}, _tid
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
                                from ...subagent_scheduler import on_turn_settled

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
                    task.add_done_callback(
                        lambda t: _TURN_TASKS.pop(session_id, None)
                        if _TURN_TASKS.get(session_id) is t
                        else None
                    )
                except Exception as e:
                    # Inline-phase failure (command/mention resolution, task
                    # spawn) — report on this socket; the turn never started.
                    _log.exception("invoke_error session=%s", session_id)
                    try:
                        await ws.send_text(
                            _ev("error", {"message": f"{type(e).__name__}: {e}"})
                        )
                    except Exception:
                        return  # socket died while reporting; nothing to do
            elif kind == "steer":
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
                    await ws.send_text(
                        _ev(
                            "notice",
                            {"message": f"/{plan.builtin_async} 需要在回合结束后执行"},
                            _steer_turn,
                        )
                    )
                    await ws.send_text(_ev("message.end", {}, _steer_turn))
                    continue
                if plan.builtin_reply is not None:
                    # Built-in commands answer immediately and never queue — the
                    # same rule `invoke` gets for free (its builtin branch sits
                    # BEFORE the busy check) and the same as Claude Code's
                    # immediate commands.
                    await ws.send_text(
                        _ev("notice", {"message": plan.builtin_reply}, _steer_turn)
                    )
                    await ws.send_text(_ev("message.end", {}, _steer_turn))
                    continue
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
                    continue
                # A turn parked at an interrupt counts as absorbable: the client
                # sends `steer` and THEN the resume message, and the resumed
                # segment's first agent_node drains it (design §3.4 — a permission
                # deny routes back to `agent`; an ask_user answer re-enters via
                # `tools → agent`).
                _steer_task = _TURN_TASKS.get(session_id)
                if session_id not in _RUNNING_TURNS and (
                    _steer_task is None or _steer_task.done()
                ):
                    # Nothing to absorb into. The client gates on busy/parked, so
                    # this is a race (the turn just ended): say so instead of
                    # swallowing the message — the client falls back to `invoke`.
                    await ws.send_text(
                        _ev(
                            "error",
                            {"message": "当前没有正在进行的回合，请重新发送"},
                            _steer_turn,
                        )
                    )
                    continue
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
                        "text": steer_text or _ATTACH_ONLY_TEXT,
                        "content_blocks": None,
                        "context_text": None,
                        "files": [],
                        "images": [],
                    }
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
                await ws.send_text(
                    _ev("steer.accepted", {"steer_id": steer_id}, _steer_turn)
                )
            elif kind == "permission_response":
                # The prompt broadcasts to every socket of the session (tabs),
                # so a second response can arrive after the first already
                # resumed the graph — ignore it instead of double-resuming.
                if session_id not in _PENDING_RESUME:
                    continue
                # version.propose deliberately shares this resume channel, so
                # accept both — but NOT a parked question, whose payload shape
                # differs. Check WITHOUT popping: a mismatched message must
                # leave the parked interrupt answerable.
                if _PENDING_KIND.get(session_id) not in (
                    "permission_request",
                    "version_propose",
                ):
                    continue
                _PENDING_RESUME.discard(session_id)
                _PENDING_KIND.pop(session_id, None)
                decision = msg.get("decision", "deny")
                _spawn_resume({"decision": decision})
            elif kind == "user_answer":
                # An ask_user question is parked. A separate message type (not
                # an extra field on permission_response) because the resume
                # payload shape differs — overloading it would force the server
                # to introspect the checkpoint to guess which shape it owes.
                if session_id not in _PENDING_RESUME:
                    continue
                # Parked on something else (or already answered): never
                # cross-resume one interrupt kind with another's payload. Peek
                # rather than pop so a stale message leaves the question live.
                if _PENDING_KIND.get(session_id) != "user_question":
                    continue
                _PENDING_RESUME.discard(session_id)
                _PENDING_KIND.pop(session_id, None)
                _spawn_resume(
                    {
                        "kind": "user_answer",
                        "answer": str(msg.get("answer") or ""),
                        "option_index": msg.get("option_index"),
                        "skip": bool(msg.get("skip", False)),
                    }
                )
            elif kind == "subagent.plan.confirm":
                # The user confirmed (possibly edited) the /subagent 拆分 plan
                # card (P2 contract 2): spawn every subtask through the existing
                # spawn path (origin=user), then answer with ONE summary notice.
                # Off the receive loop — each spawn creates a session + graph.
                _plan_id = str(msg.get("plan_id") or "")
                _override = msg.get("subtasks")
                _override = _override if isinstance(_override, list) else None

                async def _plan_confirm_job(
                    _sid=session_id, _pid=_plan_id, _ov=_override
                ) -> None:
                    try:
                        from ...subagent_plan import confirm_plan

                        await confirm_plan(_sid, _pid, _ov)
                    except Exception:
                        _log.exception(
                            "subagent_plan_confirm_failed session=%s plan=%s",
                            _sid, _pid,
                        )
                        try:
                            await _push_session_event(
                                _sid,
                                "notice",
                                {"message": "拆分方案确认失败，请重试或重新拆分"},
                            )
                        except Exception:
                            pass

                spawn_bg(_plan_confirm_job())
            elif kind == "subagent.plan.cancel":
                # Discard the pending plan (P2 contract 2) + broadcast the
                # cancellation (P3 contract 5) so every tab's pending card
                # flips to 已取消. Idempotent end to end.
                from ...subagent_plan import cancel_plan_and_broadcast

                spawn_bg(
                    cancel_plan_and_broadcast(
                        session_id, str(msg.get("plan_id") or "")
                    )
                )
            elif kind == "stop":
                # Stop the running turn (user hit ⏹). Semantics: hard stop —
                # the current model/tool step is abandoned (ABANDONED_TURNS
                # blocks its late writes); streamed text + completed steps are
                # kept; state is healed so the next turn starts clean.
                slug = session.get("project_slug")
                # An active goal would auto-continue ~3s after the stopped
                # turn unwinds — pause it first so "stop" means stop. Do NOT
                # cancel the driver task: it awaits the goal turn inline, and
                # task.cancel() would inject CancelledError into the running
                # turn (the swallow-prone cancellation this design avoids).
                try:
                    goal = goal_store.get_goal(slug, session_id)
                    if goal and goal.get("status") == goal_store.STATUS_ACTIVE:
                        paused = goal_store.update_status(
                            slug,
                            session_id,
                            goal_store.STATUS_PAUSED,
                            expected_goal_id=goal.get("goal_id"),
                        )
                        if paused:
                            await _emit_goal_event(slug, session_id, paused)
                except Exception:
                    _log.exception("stop_goal_pause_failed session=%s", session_id)
                # Subagent stop cascade (subagent-design.md §5.6 配套规则): every
                # running descendant of a stopped running/waiting subagent stops
                # with it — cooperative events only; each turn's own machinery
                # finalizes its status. Also covers the WAITING subagent whose
                # own turn is not live: it is marked stopped here. Scoped to
                # subagent sessions: stopping a MAIN conversation's turn must
                # not kill its independently running subagents.
                if session.get("type") == "subagent":
                    try:
                        from ...subagent_scheduler import (
                            on_stop_if_waiting,
                            stop_descendants,
                        )

                        async def _stop_cascade(_sid=session_id) -> None:
                            # Off the receive loop: the cascade may heal a
                            # parked descendant's checkpoint (file IO) and
                            # finalize waiting ones.
                            try:
                                await stop_descendants(_sid)
                            except Exception:
                                _log.exception(
                                    "subagent_stop_cascade_failed session=%s", _sid
                                )

                        spawn_bg(_stop_cascade())
                        spawn_bg(on_stop_if_waiting(session_id))
                    except Exception:
                        _log.exception(
                            "subagent_stop_cascade_failed session=%s", session_id
                        )
                if session_id in _PENDING_RESUME:
                    # Parked at a permission/version/ask_user interrupt: no
                    # live task — heal + broadcast directly. Check-and-discard
                    # (sync, so exactly one of two tabs wins; the loser no-ops).
                    _PENDING_RESUME.discard(session_id)
                    _PENDING_KIND.pop(session_id, None)
                    tid = _RUNNING_TURNS.pop(session_id, "")
                    spawn_bg(_stop_parked_turn(session, session_id, tid))
                    continue
                _t = _TURN_TASKS.get(session_id)
                _live = (_t is not None and not _t.done()) or session_id in _RUNNING_TURNS
                if _live:
                    evt = _TURN_STOP.get(session_id)
                    if evt is None:
                        evt = _TURN_STOP[session_id] = asyncio.Event()
                    evt.set()
                # idle → no-op (never leave a set event for the next turn)
            elif kind == "turn_state":
                # Post-reconnect probe (frontend ChatStream): is a turn still
                # streaming (or parked at an interrupt) for this session? If
                # not, the client reconciles against /history instead of
                # waiting on a stream that will never resume.
                try:
                    await ws.send_text(
                        _ev(
                            "turn.state",
                            {
                                "running": session_id in _RUNNING_TURNS,
                                "turn_id": _RUNNING_TURNS.get(session_id, ""),
                            },
                        )
                    )
                except Exception:
                    return  # socket died between recv and send
            elif kind == "ping":
                try:
                    await ws.send_text(_ev("pong", {}))
                except Exception:
                    return  # socket died between recv and send
            elif kind == "client_diag":
                # Client-reported anomaly (2026-10-01 跨会话渲染串线排查）：
                # release webviews have no readable console, so the client
                # reports its own detection here and it lands in sidecar.log
                # where the developer can read it.
                _log.warning(
                    "client_diag session=%s kind=%s detail=%s",
                    session_id, msg.get("diag_kind"), str(msg.get("detail"))[:400],
                )
            elif kind == "retry_from_checkpoint":
                # Retry the failed turn from its latest checkpoint (P2):
                # instead of re-executing from the start, resume from the last
                # successful state. This preserves tool calls and intermediate
                # results, avoiding redundant work on long-running turns.
                turn_id = msg.get("turn_id") or str(uuid.uuid4())
                # Client-facing id (the error card's retry round-trips it). If
                # the failed attempt was ABANDONED (stall watchdog / user stop)
                # its id is poisoned for checkpoint writes — FileCheckpointer
                # would silently refuse ALL state from this retry, so the retry
                # would run yet persist nothing. Execute under a derived id and
                # carry _ui_turn_id so every client-facing surface (events,
                # last_error) stays on the original.
                ui_turn_id_r = turn_id
                if turn_id in ABANDONED_TURNS:
                    turn_id = f"{ui_turn_id_r}--r{uuid.uuid4().hex[:6]}"
                _busy = _TURN_TASKS.get(session_id)
                if _busy is not None and not _busy.done():
                    await ws.send_text(
                        _ev("notice", {"message": "当前有回合正在进行，请等待其结束"}, turn_id)
                    )
                    continue
                slug = session.get("project_slug")
                # Clear last_error and announce the retry
                _session_meta_patch(slug, session_id, {"last_error": {}})
                # A FAILED subagent was already finalized terminal ("failed");
                # revive it to running so the retry's settle gate re-runs and a
                # successful retry actually injects its result (minor-6).
                try:
                    from ...subagent_scheduler import revive_failed_for_retry

                    await revive_failed_for_retry(session_id)
                except Exception:
                    _log.exception(
                        "subagent_retry_revive_failed session=%s", session_id
                    )
                _TURN_STOP.setdefault(session_id, asyncio.Event())
                retry_agent = session.get("agent_id") or _first_agent_id()
                retry_config = {
                    **config,
                    "configurable": {
                        **config["configurable"],
                        "agent_id": retry_agent,
                        "turn_id": turn_id,
                        "_ui_turn_id": ui_turn_id_r,
                    },
                }
                _log.info(
                    "retry_from_checkpoint session=%s turn=%s ui_turn=%s agent=%s",
                    session_id, turn_id, ui_turn_id_r, retry_agent,
                )

                async def _checkpoint_retry_job(
                    _cfg=retry_config, _tid=turn_id, _ui_tid=ui_turn_id_r
                ) -> None:
                    # Same capture-before-cleanup protocol as _turn_job: the
                    # settle gate needs the object to read is_set() after the
                    # finally pops the registry entry.
                    _job_stop_evt = _TURN_STOP.get(session_id)
                    try:
                        await ws.send_text(
                            _ev("turn.start", {"turn_id": _ui_tid, "agent_id": retry_agent or "", "name": retry_agent or "Agent", "from_checkpoint": True})
                        )
                        async with _turn_lock(session_id):
                            # Pass input_state=None to resume from latest checkpoint
                            await _stream_graph(
                                session["graph"],
                                _cfg,
                                input_state=None,  # Resume from checkpoint
                                command=None,
                            )
                        _start_goal_driver(session_id)
                    except Exception as e:
                        _log.exception("checkpoint_retry_error session=%s turn=%s", session_id, _tid)
                        await _push_session_event(
                            session_id, "error", {"message": f"{type(e).__name__}: {e}"}
                        )
                    finally:
                        if _TURN_TASKS.get(session_id) is asyncio.current_task():
                            _TURN_TASKS.pop(session_id, None)
                        _TURN_STOP.pop(session_id, None)
                        # Subagent completion gate for the retried segment — a
                        # retry settling is a turn settling like any other; the
                        # failed attempt's "failed" meta was revived to running
                        # before the job spawned, so a successful retry here
                        # re-runs the gate and injects its result (minor-6).
                        # The gate keys _turn_last_error on the CLIENT-facing
                        # id (_stream_graph persists last_error under
                        # ``_ui_turn_id``) — passing the derived exec ``_tid``
                        # would miss the record whenever the original turn was
                        # abandoned (its id is the ``--r`` derivation base), and
                        # a permanently failed retry would settle "done" with a
                        # phantom summary injected into the parent.
                        if session_id not in _PENDING_RESUME:
                            try:
                                from ...subagent_scheduler import on_turn_settled

                                spawn_bg(
                                    on_turn_settled(session_id, _job_stop_evt, _ui_tid)
                                )
                            except Exception:
                                _log.exception(
                                    "subagent_settle_hook_failed session=%s",
                                    session_id,
                                )

                task = asyncio.create_task(_checkpoint_retry_job())
                _TURN_TASKS[session_id] = task
                task.add_done_callback(
                    lambda t: _TURN_TASKS.pop(session_id, None)
                    if _TURN_TASKS.get(session_id) is t
                    else None
                )
            else:
                try:
                    await ws.send_text(_ev("error", {"message": f"unknown type: {kind}"}))
                except Exception:
                    return
    except WebSocketDisconnect:
        return
    finally:
        _log.info("ws_close session=%s", session_id)
        _watch_stop.set()
        _watcher_task.cancel()
        # Drop this socket from the broadcast registry (a dead entry would
        # otherwise linger until the next send attempt pruned it).
        _SESSION_WS[session_id] = [
            w for w in (_SESSION_WS.get(session_id) or []) if w is not ws
        ]
