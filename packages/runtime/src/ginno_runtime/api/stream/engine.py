"""The streaming engine: _stream_graph, the chunked stream + stall watchdog.

Split out of the former single-module ``api/stream.py`` (pure structural
move — no behavior change). The helper groups it used to carry now live in
sibling modules (``.errors`` turn-error classification/budget/auto-retry,
``.narration`` server-search + steer helpers, ``.effects`` per-turn side
effects, ``.heal`` stop/interrupted-turn repair); they are re-imported below
so ``engine.X`` keeps working. The package ``__init__`` re-exports the whole
historical namespace and mirrors patch writes into the submodules, so
``ginno_runtime.api.stream.X`` keeps working as the import/patch surface.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage
from langgraph.types import Command

from ginno_runtime import jsonx as json

from ... import agents as agents_reg
from ... import artifacts as art_store
from ... import files as files_mod
from ... import paths, usage_store
from ... import server_shared as shared
from ... import vibe_island as _vibe_island
from ... import workflows as wf_store
from ...checkpointer import ABANDONED_TURNS
from ...graph import BLOCK_PREFIX
from ...knowledge import citations as cit
from ...lang import t
from ...server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _SESSIONS,
    _TURN_STOP,
    _USAGE_BY_SESSION,
    _WF_RUN_TASKS,
    _ensure_turn_log,
    _ev,
    _log,
    _push_global_event,
    _push_session_event,
    _try_send,
    _turn_lock,
    spawn_bg,
)
from ...session_meta import _find_meta, _session_meta_patch
from ...session_unread import note_turn_finished
from ...todos import store as todo_store
from ...tools.artifact_tools import ARTIFACT_TOOL_NAMES
from ...tools.ask_tools import begin_ask_budget
from ...tools.render_tools import (
    RENDER_TOOL_NAMES,
    AttachTagStreamFilter,
    widget_event,
)
from ...tools.workflow_tools import RUN_CACHE, WORKFLOW_TOOL_NAMES
from ...usage import add_usage, cache_hit_ratio, empty_usage, extract_usage
from ...workflows import store as wf_storemod
from .. import server_tools
from ..files import _normalize_file_ref, _register_artifact_file, _session_workspace
from ..messages_ui import _tool_args_preview, _tool_content_str, _truncate_for_ws
from .effects import (
    _AUTO_DISTILL_FAIL_AT,
    _AUTO_DISTILL_THROTTLE_S,
    _emit_code_changes,
    _maybe_auto_distill,
    _process_turn_citations,
    _tool_file_effects,
)
from .errors import (
    _CONTENT_FILTER_MARKERS,
    _TRANSIENT_EXC_NAMES,
    AUTO_RETRY_BACKOFF_S,
    AUTO_RETRY_MAX,
    TURN_RECURSION_LIMIT_DEFAULT,
    TurnStopped,
    _is_content_filter_error,
    _is_transient_model_error,
    _langgraph_error_hint,
    format_turn_error,
    recursion_warn_threshold,
    turn_error_fields,
    turn_recursion_limit,
)
from .heal import _heal_interrupted_turn, _stop_parked_turn
from .narration import (
    _SERVER_SEARCH_BLOCK_TYPES,
    STEER_CONT_MAX,
    STEER_ID_SYSTEM_PREFIX,
    _emit_glm_server_search,
    _is_user_steer,
    _server_search_hits,
)

# Max seconds between stream chunks before the stall watchdog aborts the turn
# (see chunked_stream in _stream_graph). Module-level so tests can shrink it.
# 流断流看门狗:chunk 间隔超过该值即判死(防挂死 turn)。慢网关/限流退避
# (SDK+代理重试可达数分钟无 chunk)会误伤,可用 GINNO_CHUNK_TIMEOUT_S 抬高
# (ginno exec benchmark 模式默认抬高到 1200s,见 cli.py)。
CHUNK_TIMEOUT_S = float(os.environ.get("GINNO_CHUNK_TIMEOUT_S", "180"))


async def _stream_graph(
    graph,
    config: dict,
    input_state: dict | None = None,
    command: Command | None = None,
) -> None:
    """Drive the graph and emit token / tool / permission events."""
    # Arm the ask_user budget for this turn segment. Every segment (the invoke
    # and each resume) funnels through here, and each runs in its own asyncio
    # task whose context dies with it — so no reset is needed and a budget
    # never leaks into an unrelated turn.
    begin_ask_budget()
    # Pre-initialized so the finally-block bookkeeping below can never raise a
    # NameError that masks the original failure.
    saw_interrupt = False
    ws_closed = False
    # Uncommitted text of the CURRENT agent superstep (reset at each agent
    # commit): what a user stop must persist as a partial AIMessage.
    seg_text: list[str] = []
    stop_waiter: Any = None
    # mods turn.complete durationMs (claude-code-mods-design.md §15.7); reset
    # at the turn.start tap below (resumes keep the segment-entry stamp).
    _mods_t0 = time.monotonic()
    session_id = (config.get("configurable") or {}).get("thread_id", "")
    # Pre-init so the except/finally blocks below can never NameError-mask the
    # original failure when the error lands before the try-body assigns them.
    # (The placeholder Event is replaced by the session's shared stop event
    # inside the try; a fresh one simply reads as "no stop requested".)
    _cfg_conf: dict = config.get("configurable") or {}
    turn_id = _cfg_conf.get("turn_id") or ""
    ui_turn_id = turn_id
    stop_evt = asyncio.Event()
    try:
        # Per-turn trace id (from invoke, or fresh on a bare resume). `emit`
        # wraps _ev so EVERY event of this turn carries it — the frontend shows
        # it on the bubble and we log it, so a user-supplied UUID greps the logs.
        #
        # turn_id here is the EXECUTION id: an auto/checkpoint retry of an
        # abandoned turn runs under a derived id ("…--a1"/"…--r<hex>") because
        # the original sits in ABANDONED_TURNS, where FileCheckpointer would
        # silently refuse every checkpoint write of the retry (the retry would
        # run but persist nothing). ui_turn_id is the stable CLIENT-facing id:
        # events, last_error and _RUNNING_TURNS keep carrying it so the
        # frontend sees one continuous turn and its retry button round-trips.
        _cfg_conf = config.get("configurable") or {}
        turn_id = _cfg_conf.get("turn_id") or str(uuid.uuid4())
        _cfg_conf["turn_id"] = turn_id
        ui_turn_id = _cfg_conf.get("_ui_turn_id") or turn_id
        config["configurable"] = _cfg_conf

        def emit(event: str, data: dict) -> str:
            # Vibe Island notch 状态收口(engine 的 safe_send 不走
            # _push_session_event,所以这里单独埋):开关关闭时 no-op,绝不抛。
            _vibe_island.on_turn_event(session_id, event, data)
            # frame_session is stamped on every turn frame: the client drops any
            # frame whose frame_session isn't the socket's own session, making a
            # cross-session render leak structurally impossible (2026-10-01).
            # Separate key from data["session_id"] (subagent payload semantics).
            return _ev(event, {"frame_session": session_id, **data}, ui_turn_id)

        _ensure_turn_log()  # (re)point the trace file handler at the active home

        slug = (config.get("configurable") or {}).get("project_slug", "default")
        session_id = (config.get("configurable") or {}).get("thread_id", "")
        agent_id = (config.get("configurable") or {}).get("agent_id", "")
        # Usage telemetry: continuation turns are tagged by the goal driver;
        # everything else is user-driven "chat" (usage-stats-design.md §3.6).
        usage_source = (config.get("configurable") or {}).get("usage_source") or "chat"
        _RUNNING_TURNS[session_id] = ui_turn_id
        # Cooperative stop signal: setdefault so an event pre-armed by the WS
        # loop (created before this task spawned) is never clobbered.
        stop_evt = _TURN_STOP.setdefault(session_id, asyncio.Event())

        # The client (app webview / browser tab) can close the socket mid-turn
        # (refresh, navigate, sleep). Turn events therefore broadcast to EVERY
        # live socket of the session instead of only the invoking one: when the
        # invoke socket dies the client reconnects a fresh socket and keeps
        # receiving the running stream (2026-08-05 incident: the turn completed
        # server-side but its second half went nowhere because delivery was
        # tied to the dead socket). safe_send swallows per-socket send errors
        # and prunes dead sockets; ws_closed records whether the most recent
        # attempt found ANY live socket (diagnostic only — never latches, so a
        # reconnect mid-turn resumes delivery).
        ws_closed = False

        async def safe_send(data: str) -> None:
            nonlocal ws_closed
            socks = _SESSION_WS.get(session_id) or []
            alive: list[Any] = []
            for w in socks:
                if await _try_send(w, data):
                    alive.append(w)
            _SESSION_WS[session_id] = alive
            ws_closed = not alive

        async def keepalive() -> None:
            # The WS receive loop is sequential, so it can't answer the client's
            # app-level pings while a turn runs. If a tool/LLM step goes silent
            # for >45s the frontend's watchdog closes the socket (the "stuck at
            # 'now creating doc'" symptom). Send a harmless keepalive frame well
            # under that window so the client's lastSeen keeps resetting — even
            # while no socket is connected, so a reconnecting client never lands
            # in a >45s silent gap during a long tool step.
            try:
                while True:
                    await asyncio.sleep(15)
                    await safe_send(emit("keepalive", {}))
            except asyncio.CancelledError:
                raise
            except Exception:
                pass

        _ka = asyncio.create_task(keepalive())

        # Snapshot the global TODO list before the turn: afterwards we diff it
        # to find the items the agent created/touched, and auto-link THIS
        # session to them (TODO panel → sessions association, docs "TODO 特性").
        _pre_turn_todos = todo_store.list_todos()

        # Steering ack, injected into config so graph.agent_node can fire it at the
        # exact moment it absorbs (docs/steering-design.md §3.2). Emitting it HERE
        # rather than waiting for the superstep's update is what puts the client's
        # transcript band at the injection point: the band must land right after
        # the tool batch that was running, and the model's continuation tokens
        # stream immediately after the drain. Acking at commit time put the band
        # AFTER that continuation (caught by driving the real UI), and since the
        # replayed view reads the state order, live and replay disagreed.
        # In-flight bookkeeping below makes the ack durable again: it is cleared
        # by the commit, committed by the heal if the turn is stopped first, and
        # re-stashed when a parked exit means the resumed segment will re-drain.
        # Did the USER add work mid-turn? Distinguishes a human steer from the
        # system's own recursion-warn steer (see the pre-warn below): a user
        # steer legitimately needs more budget, the system one must NOT get it
        # (its whole job is to break a runaway loop — more budget would let the
        # loop run longer). A steered turn that still hits the wall is resumed
        # from its checkpoint with a fresh budget, up to STEER_CONT_MAX times.
        _steer_user_arrived = False

        async def _steer_absorbed(entries: list[dict]) -> None:
            nonlocal _steer_user_arrived
            shared.steer_mark_inflight(session_id, entries)
            for _e in entries:
                if _is_user_steer(_e):
                    _steer_user_arrived = True
            for _e in entries:
                _log.info(
                    "steer_absorbed session=%s steer=%s turn=%s",
                    session_id,
                    _e.get("steer_id"),
                    turn_id,
                )
                await safe_send(
                    emit(
                        "steer.absorbed",
                        {
                            "steer_id": _e.get("steer_id"),
                            "injected_at": _e.get("injected_at"),
                            # 原文随事件回传：渠道侧注入的 steer（channels.steer_channel_turn）
                            # 在桌面端没有本地队列条目，带 text 才能实时渲染注入带。
                            "text": _e.get("text") or "",
                            # 后台任务注入（tasks/inject）：origin + ginno_task_event
                            # 让前端把带折进 TaskCard 而非裸文本带（评审决策 4）。
                            "origin": _e.get("origin") or "",
                            "task": (
                                (_e.get("extra_kwargs") or {}).get("ginno_task_event")
                                if isinstance(_e.get("extra_kwargs"), dict)
                                else None
                            ),
                        },
                    )
                )

        config.setdefault("configurable", {})["steer_absorbed"] = _steer_absorbed
        # Superstep budget — see turn_recursion_limit above.
        config.setdefault("recursion_limit", turn_recursion_limit())

        if command is not None:
            stream = graph.astream(command, config=config, stream_mode=["messages", "updates", "custom"])
        else:
            stream = graph.astream(input_state, config=config, stream_mode=["messages", "updates", "custom"])
        saw_interrupt = False
        # Recursion-budget pre-warn (every session): once the consumed
        # supersteps come within the margin of the limit, one synthetic
        # steer tells the model to stop calling tools and wrap up — most
        # recursion deaths are then avoided outright. Subagents that still
        # hit the wall are additionally salvaged by the scheduler's wrap-up
        # continuation (subagent_scheduler on_turn_settled).
        _r_limit = int(config.get("recursion_limit") or 0)
        _r_warned = False
        _agent_steps = 0
        _found_meta = _find_meta(session_id)
        _is_subagent_session = bool(
            _found_meta and _found_meta[0].get("type") == "subagent"
        )
        special_ids: dict[str, str] = {}  # tool_call id -> special tool name (no bubble)
        tool_args_by_id: dict[str, tuple[str, dict]] = {}  # id -> (name, args)
        # tool_call ids that already emitted a tool.start bubble. Parallel tool
        # calls each carry their own id (and a distinct streaming ``index``);
        # keying on id — not ``index == 0`` — ensures every one of a parallel
        # batch gets its bubble (the old ``not index`` check only surfaced the
        # first, silently dropping the rest from the live view).
        started_tool_ids: set[str] = set()
        # Hits from the Anthropic server-side web_search tool, drained once the
        # final (non-streamed) assistant message arrives — see
        # _server_search_hits for why the streamed chunks aren't authoritative.
        _SERVER_SEARCH_BUFFER: list[list[dict]] = []
        # Z.ai/GLM gateway narration: block indexes confirmed to be machine
        # narration (their later stream fragments stay muted), and the
        # server_tool_use ids whose tool.start already went out.
        _ZAI_NARRATION_IDX: set = set()
        # Muted fragments, per block index. A consolidated block carries the
        # model's prose AFTER its dumps, and muting the block drops that prose
        # from the live bubble (history replay restores it) — but the memory
        # pool reads turn_text, so the prose is recovered once the stream ends
        # instead of being lost with the JSON.
        _ZAI_MUTED: dict[Any, list[str]] = {}
        _glm_started: set[str] = set()
        # Models that answer with XML-ish ``<attach_ref .../>`` tags instead of
        # calling the tool: the filter holds back a tag split across chunks,
        # and each completed tag becomes a ref chip (live) while the raw tag is
        # kept out of the bubble and the memory buffer. History replay parses
        # the persisted text again (messages_ui._ai_content_blocks).
        _attach_filter = AttachTagStreamFilter()
        _attach_sent: set[tuple[str, str, str]] = set()

        async def _emit_attach_tags(refs: list[dict]) -> None:
            for r in refs:
                kind = r.get("kind") or "file"
                ref_id = r.get("ref_id") or ""
                name = r.get("name") or ""
                if kind == "file":
                    # Same normalization the real tool call gets: a relative
                    # path resolves against the session workspace.
                    ref_id = _normalize_file_ref(slug, session_id, ref_id)
                key = (kind, name, ref_id)
                if key in _attach_sent:
                    continue
                _attach_sent.add(key)
                await safe_send(
                    emit("ref.emit", {"kind": kind, "name": name, "ref_id": ref_id})
                )
                if kind in ("file", "doc", "workflow", "link"):
                    art = art_store.add_artifact(
                        slug, kind, name, ref_id, session_id
                    )
                    if kind == "file":
                        _register_artifact_file(slug, session_id, art, ref_id)
        # Track tool names by id so we can emit ``tool.args`` from the chunks
        # stream (the first chunk carries the name, subsequent ones don't).
        _tool_name_by_id: dict[str, str] = {}
        # Accumulate tool_call args fragments from streaming chunks so we can
        # emit ``tool.args`` (the command text) as soon as the JSON is complete
        # — before the tool actually runs. Without this, ``tool.args`` only
        # fires in ``updates`` mode (after the agent node finishes), which for
        # some models arrives too late (or simultaneously with ``tool.end``).
        _partial_tool_args: dict[str, str] = {}
        _emitted_tool_args: set[str] = set()
        # 已下发完整入参的 tool_call id（tool.input 事件，headless 消费方用；
        # updates 模式提交点 args 保证完整，每个 id 只发一次防 resume 重放重复）。
        _emitted_tool_inputs: set[str] = set()
        turn_text: list[str] = []  # accumulate assistant text for memory capture
        # Fresh turn (not a permission resume): announce the resolved agent so the
        # UI can label the assistant bubble authoritatively (never the generic
        # "Agent" fallback).
        if command is None:
            # A new attempt supersedes any persisted last_error (empty dict =
            # cleared; _session_meta_patch skips None values).
            _session_meta_patch(slug, session_id, {"last_error": {}, "archived": False})
            _aid = (config.get("configurable") or {}).get("agent_id")
            _ag = agents_reg.get_agent(_aid) if _aid else None
            _log.info(
                "turn_start session=%s turn=%s agent=%s text=%r",
                session_id, turn_id, _aid,
                ((config.get("configurable") or {}).get("user_text") or "")[:120],
            )
            _ts_payload = {
                "turn_id": ui_turn_id,
                "agent_id": _aid or "",
                "name": _ag.name if _ag else "Agent",
            }
            # 渠道轮次（echo_user，channels/base.py）：随 turn.start 回传用户
            # 原文与入站图片——桌面端 composer 没有乐观气泡可依赖，没有这些
            # 字段 IM 侧输入在实时流里永不显示（只在重启后由 history 补上）。
            # 机器发起的 turn（子代理 brief/wake、goal 续跑）不带标记，不回显。
            if (config.get("configurable") or {}).get("echo_user"):
                _ts_payload["text"] = (config.get("configurable") or {}).get("user_text") or ""
                _echo_imgs = (config.get("configurable") or {}).get("echo_images")
                if _echo_imgs:
                    _ts_payload["images"] = [
                        {
                            "data": str(i.get("data") or ""),
                            "media_type": str(i.get("media_type") or "image/png"),
                        }
                        for i in _echo_imgs
                        if isinstance(i, dict) and i.get("data")
                    ]
            await safe_send(emit("turn.start", _ts_payload))
            # Mods turn.start tap (claude-code-mods-design.md §5.3, same
            # position as the WS event): observe-only, zero-cost with no mod.
            _mods_t0 = time.monotonic()
            try:
                from ...mods.events import dispatch_turn_start

                await dispatch_turn_start(session_id, ui_turn_id)
            except Exception:
                _log.exception("mods_turn_start_failed session=%s", session_id)
        else:
            _log.info("turn_resume session=%s turn=%s agent=%s", session_id, turn_id, agent_id)

        # Wall-clock stall watchdog: the SDK httpx read-timeout only covers
        # *network* reads, NOT a stall inside the model generator or graph (the
        # 7m49s "stuck at 'now creating doc'" case). Wrap the stream iterator so
        # any chunk that takes longer than CHUNK_TIMEOUT_S ends the stream ->
        # the except below surfaces a fast `error` event instead of hanging.
        # A legitimately long tool call is fine because its tool-result
        # `updates` chunk resets the per-chunk clock.
        # NOTE: asyncio.wait_for is NOT used here — cancellation of the stuck
        # __anext__ can be swallowed by retry layers (they catch CancelledError
        # and keep retrying), which makes wait_for wait forever and the
        # watchdog never fires. Instead await with asyncio.wait and ABANDON the
        # stuck task on timeout (fire-and-forget cancel); at most one stuck
        # task leaks per stall, and the turn still errors out fast.
        # One waiter per turn on the cooperative stop signal (armed by the WS
        # `stop` handler); cancelled in the finally block. Sharing the
        # per-chunk asyncio.wait below means a stop lands promptly even while
        # a long tool/LLM step holds __anext__ — no task.cancel() needed.
        stop_waiter = asyncio.ensure_future(stop_evt.wait())

        async def chunked_stream():
            it = stream.__aiter__()
            while True:
                nxt = asyncio.ensure_future(it.__anext__())
                nxt.add_done_callback(
                    lambda t: None if t.cancelled() else t.exception()
                )  # mark result retrieved, silence "never retrieved" warnings
                done, _ = await asyncio.wait(
                    {nxt, stop_waiter},
                    timeout=CHUNK_TIMEOUT_S,
                    # FIRST_COMPLETED, not the ALL_COMPLETED default: the
                    # stop waiter stays pending until a stop lands, and with
                    # ALL_COMPLETED every chunk would block to the timeout.
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if stop_waiter in done:
                    nxt.cancel()
                    # Same abandonment protocol as the stall watchdog below:
                    # block the detached run's late checkpoint writes, then
                    # unwind through the dedicated TurnStopped handler (state
                    # heal + turn.stopped event). Cooperative event, NOT
                    # task.cancel — retry layers swallow CancelledError.
                    ABANDONED_TURNS.add(turn_id)
                    raise TurnStopped()
                if not done:
                    nxt.cancel()
                    # Block any late checkpoint writes from this detached run
                    # (see ABANDONED_TURNS) so it can't roll back a retry.
                    ABANDONED_TURNS.add(turn_id)
                    raise RuntimeError(
                        f"model/stream stall: no chunk for {CHUNK_TIMEOUT_S:.0f}s"
                    )
                try:
                    yield nxt.result()
                except StopAsyncIteration:
                    return

        async for mode, payload in chunked_stream():
            if mode == "messages":
                chunk, msg_meta = payload
                # Only AI message chunks carry streaming text / thinking /
                # tool-call chunks. ToolMessage chunks (the result emitted by
                # the tools node, type "tool") must NOT be streamed as
                # token.delta — that would leak the tool output into the
                # assistant's text bubble (and into the memory-capture buffer).
                # Tool results reach the UI via the `updates` mode -> tool.end.
                # Streamed AI chunks report type "AIMessageChunk"; a final
                # non-streamed AIMessage reports "ai" — allow both.
                if getattr(chunk, "type", None) not in ("ai", "AIMessageChunk"):
                    continue
                content = getattr(chunk, "content", "")
                if isinstance(content, list):
                    for b in content:
                        btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
                        if btype == "thinking":
                            txt = b.get("thinking") or b.get("text") or ""
                            if txt:
                                await safe_send(emit("thinking.delta", {"content": txt}))
                        elif btype == "text":
                            txt = b.get("text") or ""
                            # Z.ai/GLM gateway narration ("**🌐 Z.ai Built-in
                            # Tool: …** / **Output:** …_result_summary: […json…]")
                            # is machine metadata, not prose — never stream it
                            # into the bubble (nor the memory buffer). Blocks
                            # stream in fragments, so a block index confirmed
                            # as narration stays muted for its later fragments.
                            bid = b.get("index") if isinstance(b, dict) else None
                            if bid is not None and bid in _ZAI_NARRATION_IDX:
                                if txt:
                                    _ZAI_MUTED.setdefault(bid, []).append(txt)
                                continue
                            if server_tools.is_server_tool_narration(txt):
                                if bid is not None:
                                    _ZAI_NARRATION_IDX.add(bid)
                                if txt:
                                    _ZAI_MUTED.setdefault(bid, []).append(txt)
                                continue
                            if server_tools.narration_tainted(txt):
                                # A fragment that MIXES prose and narration (the
                                # gateway consolidates a step into one block):
                                # forward the prose prefix only, then mute the
                                # block — the rest is JSON dumps, and the tail
                                # prose comes back on history replay.
                                _ZAI_MUTED.setdefault(bid, []).append(txt)
                                txt = server_tools.split_narration(txt)[0]
                                if bid is not None:
                                    _ZAI_NARRATION_IDX.add(bid)
                            if txt:
                                txt, _refs = _attach_filter.feed(txt)
                                await _emit_attach_tags(_refs)
                            if txt:
                                turn_text.append(txt)
                                seg_text.append(txt)
                                await safe_send(emit("token.delta", {"content": txt}))
                        elif btype == "server_tool_use":
                            # Z.ai/GLM gateway server-side search request: open
                            # the tool bubble the moment the model "calls" it
                            # (the real execution happens inside the model);
                            # tool.end with the formatted hits lands when the
                            # committing message arrives in `updates` mode.
                            tid = b.get("id") if isinstance(b, dict) else None
                            if tid and tid not in _glm_started:
                                _glm_started.add(tid)
                                await safe_send(
                                    emit(
                                        "tool.start",
                                        {"name": server_tools.DISPLAY_TOOL_NAME, "id": tid},
                                    )
                                )
                        elif btype in _SERVER_SEARCH_BLOCK_TYPES:
                            # Anthropic-protocol server-side web_search: the
                            # gateway ran the search, so the hits ride back
                            # inside the assistant message. Register them as
                            # turn sources or every [sN] the model cites
                            # resolves as unverified. Accumulated then drained
                            # on the non-streaming final message, which
                            # carries the complete block (streamed chunks
                            # repeat partial result payloads).
                            _SERVER_SEARCH_BUFFER.append(
                                _server_search_hits(b, _SERVER_SEARCH_BLOCK_TYPES)
                            )
                elif isinstance(content, str) and content:
                    # Same narration gate for a plain-string AI chunk (models
                    # that don't stream blocks); recovered cards are a replay
                    # concern — live only needs the prose.
                    if server_tools.narration_tainted(content):
                        content = server_tools.split_narration(content)[0]
                    if content:
                        content, _refs = _attach_filter.feed(content)
                        await _emit_attach_tags(_refs)
                    if content:
                        turn_text.append(content)
                        seg_text.append(content)
                        await safe_send(emit("token.delta", {"content": content}))
                rk = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
                if rk:
                    await safe_send(emit("thinking.delta", {"content": rk}))
                tool_calls = getattr(chunk, "tool_call_chunks", None)
                if tool_calls:
                    for tc in tool_calls:
                        tc_id = tc.get("id")
                        tc_index = tc.get("index")
                        tc_name = tc.get("name")
                        # Fire one tool.start per distinct tool_call id (the
                        # chunk that carries the name). See started_tool_ids —
                        # keying on id lets parallel tool calls each surface.
                        if tc_name and tc_id and tc_id not in started_tool_ids:
                            started_tool_ids.add(tc_id)
                            _tool_name_by_id[tc_id] = tc_name
                            if (
                                tc_name in RENDER_TOOL_NAMES
                                or tc_name in WORKFLOW_TOOL_NAMES
                                or tc_name in ARTIFACT_TOOL_NAMES
                            ):
                                special_ids[tc_id] = tc_name
                                continue  # surfaced as widget/ref/workflow block, not a tool bubble
                            await safe_send(
                                emit("tool.start", {"name": tc_name, "id": tc_id})
                            )
                        # Accumulate args fragments from streaming chunks.
                        # Streaming chunks only carry ``id`` on the first
                        # fragment; subsequent ones use ``index``. Resolve
                        # the id by matching the index to the most recent
                        # tool call that hasn't yet received complete args.
                        if not tc_id and tc_index is not None:
                            for sid in reversed(list(started_tool_ids)):
                                if sid not in _emitted_tool_args:
                                    tc_id = sid
                                    break
                        args_frag = tc.get("args") or ""
                        if tc_id and args_frag and tc_id not in _emitted_tool_args:
                            _partial_tool_args[tc_id] = _partial_tool_args.get(tc_id, "") + args_frag
                            raw = _partial_tool_args[tc_id]
                            try:
                                from ginno_runtime import jsonx as _json
                                parsed = _json.loads(raw)
                                if isinstance(parsed, dict):
                                    nm = _tool_name_by_id.get(tc_id, "")
                                    preview = _tool_args_preview(nm, parsed)
                                    if preview:
                                        _emitted_tool_args.add(tc_id)
                                        await safe_send(
                                            emit("tool.args", {"id": tc_id, "preview": preview})
                                        )
                            except (ValueError, TypeError):
                                pass  # incomplete JSON, keep accumulating
            elif mode == "custom":
                # 工具心跳（graph.TOOL_HEARTBEAT_S）：chunked_stream 收到任何
                # chunk 都会重置 stall 看门狗——custom 帧到这一步使命已完成，
                # 不下发前端（前端对工具运行态已有 tool.start/end 表达）。
                # 例外：模型退避透传（graph._ainvoke_rate_limited）——provider
                # 的原始错误全文必须到前端（2026-10-27 e48be8a3：静默退避
                # 把「套餐无权限」伪装成卡死），用户看着它决定 ⏹ 与否。
                if isinstance(payload, dict) and payload.get("ginno_model_error"):
                    me = payload["ginno_model_error"]
                    await safe_send(
                        emit(
                            "notice",
                            {
                                "message": (
                                    f"Model API error — retrying in "
                                    f"{me.get('wait_s', 0):.0f}s "
                                    f"({me.get('attempt')}/{me.get('max')}): "
                                    f"{me.get('error', '')}"
                                ),
                                "i18n_key": "stream.model_error_backoff",
                                "params": {
                                    "error": str(me.get("error", "")),
                                    "seconds": f"{me.get('wait_s', 0):.0f}",
                                    "attempt": int(me.get("attempt") or 0),
                                    "max": int(me.get("max") or 0),
                                },
                            },
                        )
                    )
                continue
            elif mode == "updates":
                # payload is {node_name: state_delta} OR {"__interrupt__": (Interrupt, ...)}
                for node_name, delta in (payload or {}).items():
                    if node_name == "agent":
                        # Recursion pre-warn: agent supersteps ≈ half the
                        # budget (each round is agent + tools), warn once when
                        # the remainder narrows to the margin.
                        _agent_steps += 1
                        if (
                            _r_limit
                            and not _r_warned
                            and _agent_steps >= recursion_warn_threshold(_r_limit)
                        ):
                            _r_warned = True
                            # Was subagent-only; 2026-10-08 turn 4b3a219e died
                            # at the wall with no warning because MAIN sessions
                            # were excluded. The steer matters MORE here — only
                            # the subagent scheduler has a wrap-up continuation
                            # to salvage what the wall doesn't kill.
                            _wrap_en, _wrap_zh = (
                                (
                                    "Stop issuing new tool calls and immediately"
                                    " write your final report from the material"
                                    " gathered so far.",
                                    "请停止发起新的工具调用，基于已有材料"
                                    "立即输出最终报告。",
                                )
                                if _is_subagent_session
                                else (
                                    "Stop issuing new tool calls and immediately"
                                    " answer the user with what you have gathered"
                                    " so far.",
                                    "请停止发起新的工具调用，基于已有材料"
                                    "立即给用户答复。",
                                )
                            )
                            shared.steer_enqueue(
                                session_id,
                                {
                                    "steer_id": (
                                        f"{STEER_ID_SYSTEM_PREFIX}{turn_id}"
                                    ),
                                    # Model-facing steering (i18n 分流规则):
                                    # inline bilingual t(), never a catalog key.
                                    "text": t(
                                        "[System reminder] This turn is about to "
                                        f"hit the step limit (~{2 * _agent_steps}/"
                                        f"{_r_limit} steps used). {_wrap_en}",
                                        "【系统提醒】本回合即将达到步数上限"
                                        f"（已用约 {2 * _agent_steps}/{_r_limit} 步）。"
                                        f"{_wrap_zh}",
                                    ),
                                },
                            )
                        # Superstep COMMIT point: everything streamed so far is
                        # checkpointed, so the uncommitted segment restarts here
                        # (a user stop only persists what's still uncommitted).
                        seg_text.clear()
                        for m in (delta or {}).get("messages", []):
                            # Server-side web_search (Anthropic protocol): the
                            # hits were buffered off the stream and belong to
                            # the assistant message committing here. Register
                            # them as turn sources so the [sN] ids the model
                            # cites from its answer resolve. Best-effort — the
                            # registry is a no-op outside a live turn.
                            if getattr(m, "type", None) in ("ai", "AIMessage") and _SERVER_SEARCH_BUFFER:
                                registered = [
                                    s
                                    for hits in _SERVER_SEARCH_BUFFER
                                    for s in cit.register_server_search_hits(session_id, hits)
                                ]
                                if registered:
                                    # The one visible trace of a gateway-side
                                    # search: no local tool runs, so without
                                    # this line there is nothing in the log to
                                    # tell server search from parametric recall.
                                    _log.info(
                                        "server_search session=%s turn=%s hits=%d urls=%s",
                                        session_id,
                                        turn_id,
                                        len(registered),
                                        ",".join(s["identity"] for s in registered)[:400],
                                    )
                                _SERVER_SEARCH_BUFFER.clear()
                            # Z.ai/GLM gateway server-side search — register the
                            # hits and close the tool bubble (see the helper).
                            if getattr(m, "type", None) in ("ai", "AIMessage"):
                                await _emit_glm_server_search(
                                    safe_send, emit, session_id, turn_id, m, _glm_started
                                )
                            # A steered message COMMITTED with this superstep
                            # (docs/steering-design.md §3.2): its ack was already
                            # sent at drain time (see _steer_absorbed), so all
                            # that is left is to retire the uncommitted-drain
                            # record — from here on the message is in the
                            # checkpoint, and the heal must not touch it again.
                            if (getattr(m, "additional_kwargs", None) or {}).get(
                                "ginno_steer"
                            ):
                                shared.steer_clear_inflight(session_id)
                            # D2 — per-call usage + session accumulator. Only
                            # complete AIMessages carry usage_metadata (never
                            # streamed chunks), so this fires once per LLM call.
                            u = extract_usage(m)
                            if u:
                                acc = _USAGE_BY_SESSION.setdefault(session_id, empty_usage())
                                add_usage(acc, u)
                                # Persist the call into the global usage log
                                # (usage-stats-design.md §4). Best-effort: the
                                # store never raises, so this cannot break the
                                # turn. provider/model come from the session's
                                # meta (resolved at create/rebuild time).
                                _sreg = _SESSIONS.get(session_id) or {}
                                usage_store.record(
                                    input_tokens=u["input_tokens"],
                                    output_tokens=u["output_tokens"],
                                    cache_read_tokens=u["cache_read_tokens"],
                                    cache_creation_tokens=u["cache_creation_tokens"],
                                    provider=_sreg.get("model_provider") or "",
                                    model=_sreg.get("model_name") or "",
                                    source=usage_source,
                                    session_id=session_id or None,
                                    project_slug=slug or None,
                                    agent_id=agent_id or None,
                                    turn_id=turn_id,
                                )
                                await safe_send(
                                    emit(
                                        "usage",
                                        {
                                            "turn": u,
                                            "session": dict(acc),
                                            "cache_hit_ratio": cache_hit_ratio(acc),
                                        },
                                    )
                                )
                            for tc in getattr(m, "tool_calls", []) or []:
                                nm = tc.get("name")
                                args = tc.get("args") or {}
                                tc_id = tc.get("id")
                                tool_args_by_id[tc_id] = (nm, args)
                                if (
                                    nm in RENDER_TOOL_NAMES
                                    or nm in WORKFLOW_TOOL_NAMES
                                    or nm in ARTIFACT_TOOL_NAMES
                                ):
                                    special_ids[tc_id] = nm
                                elif tc_id:
                                    # Full tool-call input on its own event
                                    # (ginno exec 的 stream-json 转录用它回填
                                    # tool_use.input；前端不消费该事件，属
                                    # 纯增量）。updates 提交点 args 一定完整。
                                    if tc_id not in _emitted_tool_inputs:
                                        _emitted_tool_inputs.add(tc_id)
                                        await safe_send(
                                            emit("tool.input", {"id": tc_id, "input": args})
                                        )
                                    # Show WHAT is running: surface the tool call's
                                    # args (e.g. the bash command) on the pending
                                    # tool bubble. Fires from the agent update —
                                    # after args are complete, before the tool runs.
                                    # Skip if already emitted from the chunks stream
                                    # (streaming args accumulation above).
                                    if tc_id not in _emitted_tool_args:
                                        preview = _tool_args_preview(nm, args)
                                        if preview:
                                            await safe_send(
                                                emit("tool.args", {"id": tc_id, "preview": preview})
                                            )
                                if nm == "render_widget":
                                    await safe_send(
                                        emit("widget.emit", widget_event(args, tc_id))
                                    )
                                elif nm == "attach_ref":
                                    await _emit_attach_tags(
                                        [
                                            {
                                                "kind": args.get("kind", "file"),
                                                "name": args.get("name", ""),
                                                "ref_id": args.get("ref_id", ""),
                                            }
                                        ]
                                    )
                                elif nm == "artifact_register":
                                    kind = args.get("kind", "file")
                                    ref = args.get("ref", "")
                                    if kind == "file":
                                        ref = _normalize_file_ref(slug, session_id, ref)
                                    art = art_store.add_artifact(
                                        slug,
                                        kind,
                                        args.get("name", ""),
                                        ref,
                                        session_id,
                                    )
                                    if kind == "file":
                                        _register_artifact_file(slug, session_id, art, ref)
                    elif node_name == "__interrupt__":
                        items = delta if isinstance(delta, (list, tuple)) else [delta]
                        for intr in items:
                            value = getattr(intr, "value", None) or intr
                            if isinstance(value, dict) and value.get("kind") == "permission_request":
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _PENDING_KIND[session_id] = "permission_request"
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s",
                                    session_id, turn_id, value.get("kind"),
                                )
                                await safe_send(
                                    emit("permission.request", {
                                        "tool": value.get("tool"),
                                        "args": value.get("args"),
                                    })
                                )
                            elif isinstance(value, dict) and value.get("kind") == "version_propose":
                                # P5: workflow edit awaiting diff confirmation.
                                # Independent of the permission system; resumed via
                                # the same permission_response WS message.
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _PENDING_KIND[session_id] = "version_propose"
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s",
                                    session_id, turn_id, value.get("kind"),
                                )
                                await safe_send(
                                    emit("version.propose", {
                                        "workflow_id": value.get("workflow_id"),
                                        "from_version": value.get("from_version"),
                                        "diff": value.get("diff", ""),
                                        "rationale": value.get("rationale", ""),
                                    })
                                )
                            elif isinstance(value, dict) and value.get("kind") == "user_question":
                                # ask_user parked the turn on an ambiguity. The
                                # card is a transcript block (not a ref like the
                                # permission prompt), so this event carries the
                                # tool_call id the client merges on.
                                saw_interrupt = True
                                _PENDING_RESUME.add(session_id)
                                _PENDING_KIND[session_id] = "user_question"
                                _log.info(
                                    "turn_interrupt session=%s turn=%s kind=%s set_id=%d",
                                    session_id, turn_id, value.get("kind"),
                                    id(_PENDING_RESUME),
                                )
                                await safe_send(
                                    emit("user.question", {
                                        "id": value.get("id"),
                                        "question": value.get("question", ""),
                                        "header": value.get("header", ""),
                                        "options": value.get("options") or [],
                                        "allow_free_text": value.get("allow_free_text", True),
                                    })
                                )
                    elif node_name == "tools":
                        msgs = (delta or {}).get("messages", [])
                        for m in msgs:
                            tc_id = getattr(m, "tool_call_id", None)
                            raw = getattr(m, "content", "") or ""
                            nm = special_ids.get(tc_id)
                            if nm in WORKFLOW_TOOL_NAMES:
                                mm = re.search(r"run_id=([0-9a-f]{6,})", raw)
                                rid = mm.group(1) if mm else None
                                run = (RUN_CACHE.get(rid) if rid else None) or (
                                    wf_store.get_run(rid) if rid else None
                                )
                                if run:
                                    # 唤起 in-session (design A): bind the chat-triggered
                                    # run to this session and drive it with the real engine.
                                    if not run.get("present_in_session_id"):
                                        run["session_id"] = session_id
                                        run["present_in_session_id"] = session_id
                                        run["updated"] = time.time()
                                        wf_storemod._write_json(wf_storemod._run_path(run["id"]), run)
                                    await safe_send(
                                        emit("run.bind", {
                                            "run_id": run["id"],
                                            "workflow_id": run["workflow_id"],
                                            "present_in_session_id": session_id,
                                        })
                                    )
                                    _wf_task = _WF_RUN_TASKS.get(run["id"])
                                    if (_wf_task is None or _wf_task.done()) and run.get("status") == "running":
                                        _spawn_run_task(
                                            run["id"],
                                            _run_workflow_bg(
                                                run["id"],
                                                run["workflow_id"],
                                                run.get("context_override"),
                                                session_id,
                                            ),
                                        )
                                    await safe_send(emit("workflow.emit", {"run": run}))
                                # no ordinary tool bubble for workflow tools
                            elif nm in RENDER_TOOL_NAMES or nm in ARTIFACT_TOOL_NAMES:
                                pass  # widget/ref emitted at agent-update; no bubble
                            elif tc_id:
                                await safe_send(
                                    emit("tool.end", {"id": tc_id, "content": _truncate_for_ws(_tool_content_str(raw))})
                                )
                                # File-reactive side effects (docs §7.5): invalidate
                                # previews of files this tool touched; auto-register
                                # + open derived analysis results.
                                await _tool_file_effects(
                                    safe_send, emit, slug, session_id,
                                    tool_args_by_id.get(tc_id), raw,
                                )
                                # Code-panel S3: agent file writes/edits →
                                # ``code.changed`` (parsed from the marker
                                # write_file / edit_file append to their own
                                # result; there is no Hook for this — its
                                # PostToolUse is declared but never dispatched).
                                await _emit_code_changes(safe_send, emit, slug, session_id, raw)
                    elif node_name == "permission":
                        # resolve "running" tool bubbles that were denied by
                        # tools_allow / hooks / policy / user (not streamed)
                        for m in (delta or {}).get("messages", []):
                            c = getattr(m, "content", "")
                            if isinstance(c, str) and c.startswith(BLOCK_PREFIX):
                                rest = c[len(BLOCK_PREFIX):]
                                name, _, reason = rest.partition("]")
                                await safe_send(
                                    emit("tool.end", {
                                        "name": name.strip(),
                                        "content": reason.strip() or c,
                                    })
                                )
                            elif isinstance(m, ToolMessage):
                                # Mods {result} takeover (design §15.5): the
                                # tool never executed — the mod's value rides a
                                # synthetic ToolMessage. Close the bubble that
                                # tool.start opened with it (special tools keep
                                # their no-bubble contract).
                                tc_id = getattr(m, "tool_call_id", None)
                                nm = special_ids.get(tc_id)
                                if tc_id and nm not in WORKFLOW_TOOL_NAMES | RENDER_TOOL_NAMES | ARTIFACT_TOOL_NAMES:
                                    await safe_send(
                                        emit("tool.end", {
                                            "id": tc_id,
                                            "content": _truncate_for_ws(_tool_content_str(getattr(m, "content", ""))),
                                        })
                                    )
        # Release whatever the attach-tag filter held back: a completed tag
        # becomes its chip, and an unterminated "<attach_ref ..." fragment is
        # plain prose that must still reach the bubble.
        _tail, _tail_refs = _attach_filter.flush()
        await _emit_attach_tags(_tail_refs)
        if _tail:
            turn_text.append(_tail)
            seg_text.append(_tail)
            await safe_send(emit("token.delta", {"content": _tail}))
        # Auto-associate this session with every TODO the turn created or
        # touched — the TODO panel surfaces these sessions as jump targets.
        # Gated on an actual todo_* tool call so unrelated edits don't count.
        if session_id and any(
            (nm or "").startswith("todo_") for nm, _args in tool_args_by_id.values()
        ):
            for t in todo_store.touched_since(_pre_turn_todos):
                if t.get("id") and session_id not in (t.get("session_ids") or []):
                    todo_store.link_session(t["id"], session_id)
        # refresh the right-panel TODO list after every turn (the agent may
        # have mutated it via the todo_* tools); the checkbox path is optimistic
        # and doesn't need this.
        await safe_send(emit("todos.changed", {}))
        await safe_send(emit("workflows.changed", {}))
        await safe_send(emit("artifacts.changed", {}))
        # skills too: a turn may have installed/uninstalled skills (the
        # install_skills tool — or even bash), and the slash menu / next
        # turn's skills index must reflect it without a manual refresh.
        await safe_send(emit("skills.changed", {}))
        if not saw_interrupt:
            # Prose stranded behind muted narration (see _ZAI_MUTED) is only
            # for the memory pool — the live bubble already showed everything
            # before the first dump.
            for _frags in _ZAI_MUTED.values():
                _prose = server_tools.split_narration("".join(_frags))[0]
                if _prose.strip():
                    turn_text.append(_prose)
            _final_text = "".join(turn_text)
            # Citation framework: parse/validate the trailing block against the
            # turn's registered sources, record the usage ledger. Runs before
            # memory capture so the block can be stripped from the pool text.
            _verified = 0
            try:
                _verified = await _process_turn_citations(session_id, turn_id, _final_text)
            except Exception:
                _log.exception("citations_failed session=%s turn=%s", session_id, turn_id)
            # Capture sanitized assistant text for memory summarization (P2).
            # Also reused as the desktop notification body below (the web shell
            # shows a turn-done notification when the user looked away).
            # Gate 1: `capture` config (revived) + verified-citation quality
            # signal travels with the entry for evidence-weighted distilling.
            from ...knowledge.citations import strip_citation_block

            _clean_text = strip_citation_block(_final_text)
            if turn_text:
                from ...knowledge.config import load_knowledge_config
                from ...memory import append_to_pool

                _mem_cfg = load_knowledge_config()
                if _mem_cfg.capture:
                    append_to_pool(session_id, agent_id, _clean_text, cited=_verified > 0)
                    _maybe_auto_distill(_mem_cfg)
            # LLM subject title (title_gen): armed by _touch_session_title on
            # the first invoke, fired by the first turn that completes without
            # an interrupt. Background + best-effort — failure keeps the
            # truncated placeholder and retries on the next completed turn.
            if _clean_text.strip():
                from ...title_gen import spawn_title_gen

                _found = _find_meta(session_id)
                if _found and _found[0].get("title_llm_pending"):
                    _session_meta_patch(
                        slug, session_id, {"title_assistant": _clean_text[:2000]}
                    )
                    spawn_title_gen(session_id, turn_id)
            _log.info(
                "turn_done session=%s turn=%s status=completed text_len=%d",
                session_id, turn_id, len("".join(turn_text)),
            )
            # Session unread inbox (session_unread.py): completed turns make
            # the session unread unless the user is watching — the frontend
            # clears it via POST /api/sessions/{id}/seen. Only this explicit
            # terminal branch (not the finally leg: retry/interrupt parking
            # traverse it too) and not paused_at_interrupt / TurnStopped.
            note_turn_finished(slug, session_id)
            # Empty text (tool-only turn) → the UI falls back to a generic body.
            # Mods turn.complete (claude-code-mods-design.md §5.3/§15.7):
            # detached dispatch BEFORE message.end; observe-only in P0.
            try:
                from ...mods.events import spawn_turn_complete

                spawn_turn_complete(
                    session_id,
                    ui_turn_id,
                    answer=_clean_text,
                    duration_ms=int((time.monotonic() - _mods_t0) * 1000),
                    is_aborted=False,
                    reason="answer",
                )
            except Exception:
                _log.exception("mods_turn_complete_failed session=%s", session_id)
            await safe_send(emit("message.end", {"text": _clean_text.strip()[:200]}))
        else:
            _log.info(
                "turn_done session=%s turn=%s status=paused_at_interrupt",
                session_id, turn_id,
            )
    except TurnStopped:
        # User pressed stop: heal the persisted state (dangling tool_calls /
        # uncommitted partial text), tell the clients, exit quietly. No error
        # card, no last_error, no completion side effects (memory/title/
        # citations live under the normal-completion branch below).
        await _heal_interrupted_turn(graph, config, "".join(seg_text))
        _log.info(
            "turn_stopped session=%s turn=%s seg_len=%d",
            session_id, turn_id, len("".join(seg_text)),
        )
        await safe_send(emit("turn.stopped", {}))
        # Mods turn.complete for the aborted close-out (§5.3: all three
        # engine close-outs dispatch; detached, observe-only in P0).
        try:
            from ...mods.events import spawn_turn_complete

            spawn_turn_complete(
                session_id,
                ui_turn_id,
                answer="".join(seg_text),
                duration_ms=int((time.monotonic() - _mods_t0) * 1000),
                is_aborted=True,
                reason="aborted",
            )
        except Exception:
            _log.exception("mods_turn_complete_failed session=%s", session_id)
    except Exception as e:
        # A turn the USER steered mid-flight outran the budget it was sized
        # for: resume from the checkpoint with a fresh budget instead of
        # failing, or the user's own request dies at the wall. Bounded by
        # STEER_CONT_MAX. Only for user steers — the system's recursion-warn
        # steer deliberately gets no extension (it exists to stop runaway
        # loops, and more budget would just let one run longer).
        _steer_cont = int(_cfg_conf.get("_steer_cont") or 0)
        if _steer_user_arrived and _steer_cont < STEER_CONT_MAX:
            from langgraph.errors import GraphRecursionError

            if isinstance(e, GraphRecursionError):
                try:
                    _snap = await graph.aget_state(config)
                    _has_ckpt = bool(getattr(_snap, "values", None) or {})
                except Exception:
                    _has_ckpt = False
                if _has_ckpt:
                    _log.warning(
                        "turn_steer_continue session=%s turn=%s cont=%d/%d",
                        session_id, turn_id, _steer_cont + 1, STEER_CONT_MAX,
                    )
                    await safe_send(
                        emit(
                            "notice",
                            {
                                "message": (
                                    "You steered this turn past its step budget; "
                                    "continuing with a fresh budget"
                                    f" ({_steer_cont + 1}/{STEER_CONT_MAX})…"
                                ),
                                "i18n_key": "stream.steer_continue",
                                "params": {
                                    "attempt": _steer_cont + 1,
                                    "max": STEER_CONT_MAX,
                                },
                            },
                        )
                    )
                    await _stream_graph(
                        graph,
                        {
                            **config,
                            "configurable": {
                                **_cfg_conf,
                                # Derived exec id: the abandoned original's
                                # checkpoint writes would be refused.
                                "turn_id": (
                                    f"{ui_turn_id}--s{_steer_cont + 1}-"
                                    f"{uuid.uuid4().hex[:6]}"
                                ),
                                "_ui_turn_id": ui_turn_id,
                                "_steer_cont": _steer_cont + 1,
                            },
                        },
                        input_state=None,
                        command=None,
                    )
                    return
        # Transient provider/network failure (SSL drop, connection error,
        # 429/5xx, stall watchdog): auto-retry from the latest checkpoint with
        # a backoff instead of failing the turn — see AUTO_RETRY_MAX notes.
        # The retry runs under a DERIVED exec turn_id (the abandoned original
        # would have its checkpoint writes refused) while every client-facing
        # surface keeps ui_turn_id. The caller still holds _turn_lock, so the
        # recursive call must NOT re-acquire it (it doesn't — the lock lives in
        # the job wrappers). A user stop during the backoff aborts the retry.
        if not stop_evt.is_set() and _is_transient_model_error(e):
            _attempt = int(_cfg_conf.get("_auto_retry_attempt") or 0)
            if _attempt < AUTO_RETRY_MAX:
                _backoff = AUTO_RETRY_BACKOFF_S[
                    min(_attempt, len(AUTO_RETRY_BACKOFF_S) - 1)
                ]
                _log.warning(
                    "turn_auto_retry session=%s turn=%s attempt=%d/%d backoff=%.0fs err=%s: %s",
                    session_id, turn_id, _attempt + 1, AUTO_RETRY_MAX,
                    _backoff, type(e).__name__, str(e)[:200],
                )
                await safe_send(
                    emit(
                        "notice",
                        {
                            # Event contract (i18n-design.md §3): English
                            # fallback text + i18n_key + params for the UI.
                            # error 参数带 provider 原始信息（e48be8a3 决策：
                            # 接口错误永远亮给前端，用户自己决定中断与否）。
                            "message": (
                                f"Model connection error, auto-retrying in "
                                f"{_backoff:.0f}s ({_attempt + 1}/{AUTO_RETRY_MAX}): "
                                f"{str(e)[:300]}"
                            ),
                            "i18n_key": "stream.auto_retry",
                            "params": {
                                "error": f"{type(e).__name__}: {str(e)[:300]}",
                                "seconds": f"{_backoff:.0f}",
                                "attempt": _attempt + 1,
                                "max": AUTO_RETRY_MAX,
                            },
                        },
                    )
                )
                await asyncio.sleep(_backoff)
                if not stop_evt.is_set():
                    _retry_conf = {
                        **config,
                        "configurable": {
                            **_cfg_conf,
                            # Random suffix: a later retry chain of the same
                            # ui turn must never collide with an earlier
                            # chain's abandoned exec id (exact-match gate in
                            # FileCheckpointer.aput/aput_writes).
                            "turn_id": f"{ui_turn_id}--a{_attempt + 1}-{uuid.uuid4().hex[:6]}",
                            "_ui_turn_id": ui_turn_id,
                            "_auto_retry_attempt": _attempt + 1,
                        },
                    }
                    # Resume from the latest checkpoint when one exists (tool
                    # work preserved). A failure on the FIRST model call of a
                    # fresh session commits nothing — resuming with None would
                    # die on EmptyInputError — so re-run the original input
                    # instead. Neither available: fall through to the error.
                    try:
                        _snap = await graph.aget_state(config)
                        _has_ckpt = bool(getattr(_snap, "values", None) or {})
                    except Exception:
                        _has_ckpt = False
                    if _has_ckpt or input_state is not None:
                        await _stream_graph(
                            graph,
                            _retry_conf,
                            input_state=None if _has_ckpt else input_state,
                            command=None,
                        )
                        return
        _log.exception("turn_error session=%s turn=%s", session_id, turn_id)
        # Event contract (i18n-design.md §3): the raw exception dump (+ remedy
        # hint for known failures) stays the English fallback; the key lets the
        # UI render a localized version.
        _err_fields = turn_error_fields(e)
        # Persist the failure on the session meta so the error card (with its
        # retry action) survives webview reloads and route/session switches —
        # the history endpoint re-surfaces it as the last message. The retry
        # button round-trips ui_turn_id; the handler derives a fresh exec id
        # when this turn was abandoned.
        _session_meta_patch(
            slug,
            session_id,
            {
                "last_error": {
                    "turn_id": ui_turn_id,
                    "message": _err_fields["message"],
                    # key+params 随行持久化:history 回放重放错误卡时按事件
                    # 契约落地翻译,而不是退化为英文 message(i18n-design §3)。
                    "i18n_key": _err_fields.get("i18n_key"),
                    "params": _err_fields.get("params"),
                    "at": time.time(),
                },
                # Session unread inbox: error turns count as a finished turn
                # (same patch — one index write). The auto-retry branch above
                # `return`s before this point, so retried turns settle once.
                "last_finished": time.time(),
            },
        )
        await safe_send(
            emit(
                "error",
                {
                    **_err_fields,
                },
            )
        )
        # Mods turn.complete for the error close-out (§5.3). The auto-retry
        # branch above `return`s before this point, so a retried turn only
        # dispatches once its final segment settles.
        try:
            from ...mods.events import spawn_turn_complete

            spawn_turn_complete(
                session_id,
                ui_turn_id,
                answer="".join(seg_text),
                duration_ms=int((time.monotonic() - _mods_t0) * 1000),
                is_aborted=stop_evt.is_set(),
                reason="error",
            )
        except Exception:
            _log.exception("mods_turn_complete_failed session=%s", session_id)
    finally:
        try:
            _ka.cancel()
        except Exception:
            pass
        if stop_waiter is not None and not stop_waiter.done():
            stop_waiter.cancel()
        # major-B: capture BEFORE the pop — a set event here means the USER
        # stopped this turn (the TurnStopped unwind, or a stop landing during
        # retry backoff). The reconcile below must not spawn result-redelivery
        # wakes ahead of the stopped finalize on such a turn.
        _user_stopped = stop_evt.is_set()
        # A parked-at-interrupt turn no longer needs the stop event either —
        # the WS `stop` handler heals parked turns directly (no live stream).
        _TURN_STOP.pop(session_id, None)
        # Ended or errored (not paused at an interrupt): unregister so a client
        # `turn_state` query reports "not running". A turn parked at a
        # permission/version-propose/ask_user interrupt stays registered — its
        # resume hasn't happened yet.
        if not saw_interrupt:
            _RUNNING_TURNS.pop(session_id, None)
            _PENDING_RESUME.discard(session_id)
            _PENDING_KIND.pop(session_id, None)
            # Steering entries live exactly one turn segment (steering-design
            # §3.2, §6). Clearing on the SAME gate as the unregistering above is
            # load-bearing: the client sends `steer` and the resume
            # back-to-back, and that steer can land while this segment is still
            # draining its generator — i.e. before this line runs. A parked exit
            # must therefore KEEP the stash for the resumed segment to absorb
            # (e2e test_steer_at_a_permission_card_redirects_the_turn catches the
            # version that cleared here unconditionally). Dropping an unabsorbed
            # entry is safe because the client re-sends it (design §3.3).
            #
            # EXCEPT the scheduler's own injections (steer entries tagged
            # ``ginno_subagent_result``): nothing ever re-sends those, so one
            # landing after this turn's LAST agent superstep drain would be
            # lost. Hand them back to the scheduler for a wake-turn redelivery
            # first (major-1 reconciliation; the settle hook re-checks for
            # entries a raced wake restashed).
            # EXCEPT on a user-stopped turn (major-B): a redelivery wake spawned
            # by the plain reconcile wins the just-freed turn lock AHEAD of this
            # job's settle hook — it opens a GHOST turn (fresh stop event,
            # _RUNNING_TURNS re-set), the settle hook then bounces off the
            # _RUNNING_TURNS guard, the stopped finalize never runs, and the
            # ghost turn's settle marks the stopped session done and injects it
            # upward. Finalize the stopped subagent FIRST; the reconcile below
            # then redelivers only what can still absorb it (a now-terminal
            # parent refuses its wakes; a main conversation keeps the major-1
            # redelivery untouched).
            if _user_stopped:
                try:
                    from ...subagent_scheduler import on_turn_stopped

                    await on_turn_stopped(session_id)
                except Exception:
                    _log.exception(
                        "subagent_stop_finalize_failed session=%s", session_id
                    )
            try:
                from ...subagent_scheduler import reconcile_stash_injections

                reconcile_stash_injections(session_id)
            except Exception:
                _log.exception("subagent_reconcile_failed session=%s", session_id)
            # 渠道来源的未吸收 steer 不清场（steer_keep_channel）：turn 已
            # 收尾、注入无下家，run_channel_turn 尾部排水后由 adapter 补投
            # 为后续 turn——「已追加」的回执不能是空头支票。
            shared.steer_keep_channel(session_id)
            # The uncommitted-drain record is either retired by the commit or
            # committed by the heal (both ran above) — never left behind.
            shared.steer_clear_inflight(session_id)
        else:
            # Parked: the interrupt fires after the agent commit, so a pending
            # drain should not exist — but if one ever did, the resumed segment
            # re-runs the node that drains, so hand it back rather than dropping
            # the user's message on the floor.
            shared.steer_restash_inflight(session_id)
        if ws_closed:
            _log.info(
                "turn_client_gone session=%s turn=%s (no live client socket at the "
                "last send; turn completed server-side)",
                session_id,
                turn_id,
            )
