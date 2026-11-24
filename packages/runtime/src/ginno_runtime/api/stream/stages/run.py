"""run.py — staged 引擎编排器（批次 5-2）。

legacy ``engine._stream_graph`` 的函数骨架（初始化 → try 主体 → except →
finally），逐段分派到四阶段：

- ``streamio`` — 帧出口/合帧/token.delta 文本流/看门狗；
- ``toolloop`` — 工具调用帧/args 流/工具结果与错误路径；
- ``permission`` — ``__interrupt__`` 停车（ask_user/permission/version）；
- ``finalize`` — 段末收尾/parked 退出/异常路径/finally 清理。

分段对照表（legacy engine.py 行号 → 本文）：

===============  ===========================  =============================
legacy 行号       内容                          本文位置
===============  ===========================  =============================
119-166          签名/预初始化                 StreamTurnCtx.__init__
168-186          turn_id/ui_turn_id 解析        _setup_identity
188-199          emit 闭包                      streamio.make_emit
200-217          slug/agent/usage_source/注册表① _setup_identity
209-217          note_turn_start/stop_evt       _setup_identity
218-224          ws_closed=False                _setup_identity
225-252          safe_send/DeltaCoalescer      _setup_io
254-269          keepalive                     streamio.keepalive + _setup_io
271-274          _pre_turn_todos               _setup_io
280-288          _steer_user_arrived           _setup_stream
289-345          _steer_absorbed 闭包           streamio.make_steer_absorbed
346-354          steer 注入/stream 建立         _setup_stream
355-432          循环前状态/accumulators       _setup_stream
433-484          turn.start / turn_resume      _emit_turn_start
485-507          stop_waiter                   主函数 try 体内
508-536          chunked_stream 闭包            streamio.chunked_stream
537-543          async for + type gate         主函数循环
544-651          messages 文本段               streamio.handle_messages_text
652-727          messages 工具段               toolloop.handle_messages_tool_chunks
728-759          custom 段                     streamio.handle_custom
760-1118         updates(agent/tools/perm)     toolloop.handle_updates
1119-1152        __interrupt__                 permission.handle_interrupt
1153-1181        attach 尾片/todo/changed      finalize.post_stream_flush
1182-1440        completed / parked            finalize.finalize_completed + 主函数
1441-1467        except TurnStopped            closeout.handle_turn_stopped
1468-1744        except Exception              closeout.handle_turn_error
1745-1769        finally                       finalize.finalize
===============  ===========================  =============================

注册表写入点对照（见各处行内注释）：

- ① ``_RUNNING_TURNS``/``_RUNNING_TURNS_AT`` 写入 — _setup_identity（legacy 207-212）
- ② ``_TURN_STOP.pop`` — 栈条目 E3 = finalize._release_stop_entry（legacy 1767）
- ③ ``_PENDING_RESUME``/``_PENDING_KIND`` 写入 — permission.handle_interrupt / publish_parked_question
- ④ ``_RUNNING_TURNS`` 等 pop/``_PENDING_*`` 清 — 栈条目 E2 =
  finalize._release_turn_registration（legacy 1772-1800；批次 5-3 起由
  AsyncExitStack LIFO 弹出，登记点在写入点①的紧邻行——异常路径同清）
"""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from ....tools.ask_tools import reset_ask_budget
from .. import engine as eng
from . import closeout
from . import finalize as finalize_mod
from .ctx import StreamTurnCtx
from .finalize import (
    _cancel_keepalive,
    _cancel_stop_waiter,
    _close_coalescer,
    _release_stop_entry,
    _release_turn_registration,
)
from .permission import publish_parked_question
from .streamio import (
    chunked_stream,
    handle_custom,
    handle_messages_text,
    keepalive,
    make_emit,
    make_safe_send,
    make_steer_absorbed,
)
from .toolloop import handle_messages_tool_chunks, handle_updates


def _setup_identity(ctx: StreamTurnCtx, stack: AsyncExitStack) -> None:
    """legacy engine.py:168-224 — turn 标识解析、注册表①、遗言、stop 事件。

    批次 5-3：注册表①写入后**立即**在获取点登记栈条目（E2/E3）——
    acquire 与 release 钉在同一函数邻域，异常路径的清理由栈保证。
    """
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
    ctx._cfg_conf = ctx.config.get("configurable") or {}
    ctx.turn_id = ctx._cfg_conf.get("turn_id") or str(eng.uuid.uuid4())
    ctx._cfg_conf["turn_id"] = ctx.turn_id
    ctx.ui_turn_id = ctx._cfg_conf.get("_ui_turn_id") or ctx.turn_id
    ctx.config["configurable"] = ctx._cfg_conf

    ctx.emit = make_emit(ctx)
    eng._ensure_turn_log()  # (re)point the trace file handler at the active home

    ctx.slug = (ctx.config.get("configurable") or {}).get("project_slug", "default")
    ctx.session_id = (ctx.config.get("configurable") or {}).get("thread_id", "")
    ctx.agent_id = (ctx.config.get("configurable") or {}).get("agent_id", "")
    # Usage telemetry: continuation turns are tagged by the goal driver;
    # everything else is user-driven "chat" (usage-stats-design.md §3.6).
    ctx.usage_source = (ctx.config.get("configurable") or {}).get("usage_source") or "chat"
    # 【注册表写入点①】running 登记 + 心跳戳(legacy 207-212)。
    eng._RUNNING_TURNS[ctx.session_id] = ctx.ui_turn_id
    # Heartbeat stamp for turn_gate_busy's staleness self-heal (see
    # server_shared): every turn execution re-arms here.
    eng._RUNNING_TURNS_AT[ctx.session_id] = eng.time.time()
    # 【栈条目 E2】条件收尾组（RUNNING/AT pop + PENDING_* 清 + 终态钩子），
    # 压栈后由 LIFO 在 E3(_TURN_STOP pop) 之后弹出——与 legacy finally 的
    # 段序逐行一致。登记即配对：此后本函数抛异常，注册表也必然回滚。
    stack.push_async_callback(_release_turn_registration, ctx)
    # 遗言 WAL 护体（docs/lastwords-design.md P2）：turn 启动即写初始
    # 遗言（reason=running）——此刻 checkpoint 还没有本 turn 的请求
    # （快照回落上一条用户消息），工具边界写会立刻覆盖补全；SIGKILL
    # 场景最坏只丢最后一次增量。best-effort，绝不拖累 turn。
    try:
        from ..lastwords import note_turn_start

        note_turn_start(ctx.session_id, ctx.turn_id, ctx.slug)
    except Exception:  # noqa: BLE001
        pass
    # Cooperative stop signal: setdefault so an event pre-armed by the WS
    # loop (created before this task spawned) is never clobbered.
    ctx.stop_evt = eng._TURN_STOP.setdefault(ctx.session_id, asyncio.Event())
    # 【栈条目 E3】_TURN_STOP 弹出（finally 中段；parked turn 由 WS stop
    # handler 直接 heal，不需要事件）。后于 E2 压栈 → 先于 E2 弹出。
    stack.callback(_release_stop_entry, ctx)

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
    ctx.ws_closed = False


def _setup_io(ctx: StreamTurnCtx, stack: AsyncExitStack) -> None:
    """legacy engine.py:225-274 — safe_send、合帧器、keepalive、TODO 快照。

    批次 5-3 栈登记顺序契约：E5(keepalive) 先压、E6(合帧器) 后压——
    LIFO 弹出序 coal.close → _ka.cancel 与 legacy finally 首两句对位
    （获取序 coal→ka 与清理序相反，故两条登记集中在获取完成后成对写）。
    """
    ctx.safe_send = make_safe_send(ctx)
    # token.delta/thinking.delta 合帧（delta_coalesce.py）：33ms 窗口
    # ≈30fps，N 路并发时 WS send/json.dumps/前端 setState 同比例降频。
    # 协议不变；非 delta 事件前 flush 保序；finally close 清残留。
    from ..delta_coalesce import DeltaCoalescer
    from ..reasoning_summary import begin_summary, end_summary

    ctx.reasoning_summary = begin_summary(ctx.session_id, ctx.ui_turn_id)
    stack.callback(end_summary, ctx.session_id, ctx.reasoning_summary)
    ctx._coal = DeltaCoalescer(ctx.safe_send, ctx.emit)
    ctx._ka = asyncio.create_task(keepalive(ctx))
    # 【栈条目 E5/E6】见 docstring 顺序契约：ka 先压（后弹）、coal 后压
    # （先弹）→ 弹出序 = coal.close → ka.cancel = legacy finally 序。
    stack.callback(_cancel_keepalive, ctx)
    stack.push_async_callback(_close_coalescer, ctx)

    # Snapshot the global TODO list before the turn: afterwards we diff it
    # to find the items the agent created/touched, and auto-link THIS
    # session to them (TODO panel → sessions association, docs "TODO 特性").
    ctx._pre_turn_todos = eng.todo_store.list_todos()


def _setup_stream(ctx: StreamTurnCtx) -> None:
    """legacy engine.py:280-432 — steer 回调、流建立、循环前状态。"""
    from .... import server_shared as shared

    # Image-emit baseline (image-emit-universal design): snapshot the
    # session's registered images at turn start. setdefault semantics — a
    # resume segment (parked turn) reuses the ORIGINAL snapshot so pictures
    # the pre-park tool steps produced still count as new for the agent-node
    # lift; the entry is cleared at the terminal release only. The stream
    # layer diffs its own COPY (mtime flattening after an emit must not hide
    # the picture from the lift, which reads the shared original).
    try:
        base = shared._TURN_IMAGE_BASELINE.get(ctx.session_id)
        if base is None:
            from .... import files as files_mod

            base = {
                e.get("id"): float(e.get("mtime") or 0.0)
                for e in files_mod.get_registry(ctx.slug).list_session(ctx.session_id)
                if e.get("kind") == "image" and e.get("id")
            }
            shared._TURN_IMAGE_BASELINE[ctx.session_id] = base
        ctx.baseline_image_mtimes = dict(base)
        ctx.emitted_image_ids = set()
    except Exception:  # noqa: BLE001 — 快照失败退化为空基线（多播不重播失败）
        ctx.baseline_image_mtimes = {}
        ctx.emitted_image_ids = set()

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
    ctx._steer_user_arrived = False
    _steer_absorbed = make_steer_absorbed(ctx)
    ctx.config.setdefault("configurable", {})["steer_absorbed"] = _steer_absorbed
    # Superstep budget — see turn_recursion_limit above.
    ctx.config.setdefault("recursion_limit", eng.turn_recursion_limit())

    if ctx.command is not None:
        ctx.stream = ctx.graph.astream(
            ctx.command, config=ctx.config,
            stream_mode=["messages", "updates", "custom"],
        )
    else:
        ctx.stream = ctx.graph.astream(
            ctx.input_state, config=ctx.config,
            stream_mode=["messages", "updates", "custom"],
        )
    ctx.saw_interrupt = False
    # Recursion-budget pre-warn (every session): once the consumed
    # supersteps come within the margin of the limit, one synthetic
    # steer tells the model to stop calling tools and wrap up — most
    # recursion deaths are then avoided outright. Subagents that still
    # hit the wall are additionally salvaged by the scheduler's wrap-up
    # continuation (subagent_scheduler on_turn_settled).
    ctx._r_limit = int(ctx.config.get("recursion_limit") or 0)
    ctx._r_warned = False
    ctx._agent_steps = 0
    ctx._found_meta = eng._find_meta(ctx.session_id)
    ctx._is_subagent_session = bool(
        ctx._found_meta and ctx._found_meta[0].get("type") == "subagent"
    )
    ctx.special_ids = {}  # tool_call id -> special tool name (no bubble)
    ctx.tool_args_by_id = {}  # id -> (name, args)
    # tool_call ids that already emitted a tool.start bubble. Parallel tool
    # calls each carry their own id (and a distinct streaming ``index``);
    # keying on id — not ``index == 0`` — ensures every one of a parallel
    # batch gets its bubble (the old ``not index`` check only surfaced the
    # first, silently dropping the rest from the live view).
    ctx.started_tool_ids = set()
    # Hits from the Anthropic server-side web_search tool, drained once the
    # final (non-streamed) assistant message arrives — see
    # _server_search_hits for why the streamed chunks aren't authoritative.
    ctx._SERVER_SEARCH_BUFFER = []
    # Z.ai/GLM gateway narration: block indexes confirmed to be machine
    # narration (their later stream fragments stay muted), and the
    # server_tool_use ids whose tool.start already went out.
    ctx._ZAI_NARRATION_IDX = set()
    # Muted fragments, per block index. A consolidated block carries the
    # model's prose AFTER its dumps, and muting the block drops that prose
    # from the live bubble (history replay restores it) — but the memory
    # pool reads turn_text, so the prose is recovered once the stream ends
    # instead of being lost with the JSON.
    ctx._ZAI_MUTED = {}
    ctx._glm_started = set()
    # Models that answer with XML-ish ``<attach_ref .../>`` tags instead of
    # calling the tool: the filter holds back a tag split across chunks,
    # and each completed tag becomes a ref chip (live) while the raw tag is
    # kept out of the bubble and the memory buffer. History replay parses
    # the persisted text again (messages_ui._ai_content_blocks).
    ctx._attach_filter = eng.AttachTagStreamFilter()
    ctx._attach_sent = set()
    # Track tool names by id so we can emit ``tool.args`` from the chunks
    # stream (the first chunk carries the name, subsequent ones don't).
    ctx._tool_name_by_id = {}
    # Accumulate tool_call args fragments from streaming chunks so we can
    # emit ``tool.args`` (the command text) as soon as the JSON is complete
    # — before the tool actually runs. Without this, ``tool.args`` only
    # fires in ``updates`` mode (after the agent node finishes), which for
    # some models arrives too late (or simultaneously with ``tool.end``).
    ctx._partial_tool_args = {}
    ctx._last_args_tail_ts = {}
    ctx._emitted_tool_args = set()
    # 已下发完整入参的 tool_call id（tool.input 事件，headless 消费方用；
    # updates 模式提交点 args 保证完整，每个 id 只发一次防 resume 重放重复）。
    ctx._emitted_tool_inputs = set()
    ctx.turn_text = []  # accumulate assistant text for memory capture


async def _emit_turn_start(ctx: StreamTurnCtx) -> None:
    """legacy engine.py:433-484 — turn.start 帧（fresh turn）或 resume 日志。"""
    # Fresh turn (not a permission resume): announce the resolved agent so the
    # UI can label the assistant bubble authoritatively (never the generic
    # "Agent" fallback).
    if ctx.command is None:
        # A new attempt supersedes any persisted last_error (empty dict =
        # cleared; _session_meta_patch skips None values).
        eng._session_meta_patch(
            ctx.slug, ctx.session_id, {"last_error": {}, "archived": False}
        )
        _aid = (ctx.config.get("configurable") or {}).get("agent_id")
        _ag = eng.agents_reg.get_agent(_aid) if _aid else None
        eng._log.info(
            "turn_start session=%s turn=%s agent=%s text=%r",
            ctx.session_id, ctx.turn_id, _aid,
            ((ctx.config.get("configurable") or {}).get("user_text") or "")[:120],
        )
        _ts_payload = {
            "turn_id": ctx.ui_turn_id,
            "agent_id": _aid or "",
            "name": _ag.name if _ag else "Agent",
        }
        # 回显用户原文与入站图片（echo_user 标记）：渠道轮由 channels/
        # base.py 置标记（桌面端 composer 没有乐观气泡可依赖，没有这些
        # 字段 IM 侧输入在实时流里永不显示）；调度器 wake turn（子代理
        # brief/wake、worker assign、goal 续跑）由 runner.py 置标记——
        # 开着会话页时注入信封实时落输入气泡，不再等 history 补上。
        if (ctx.config.get("configurable") or {}).get("echo_user"):
            _ts_payload["text"] = (
                (ctx.config.get("configurable") or {}).get("user_text") or ""
            )
            _echo_imgs = (ctx.config.get("configurable") or {}).get("echo_images")
            if _echo_imgs:
                _ts_payload["images"] = [
                    {
                        "data": str(i.get("data") or ""),
                        "media_type": str(i.get("media_type") or "image/png"),
                    }
                    for i in _echo_imgs
                    if isinstance(i, dict) and i.get("data")
                ]
        task_events = ctx._cfg_conf.get("task_events")
        if task_events:
            _ts_payload["task_events"] = task_events
        await ctx.safe_send(ctx.emit("turn.start", _ts_payload))
        # Mods turn.start tap (claude-code-mods-design.md §5.3, same
        # position as the WS event): observe-only, zero-cost with no mod.
        ctx._mods_t0 = eng.time.monotonic()
        try:
            from ....mods.events import dispatch_turn_start

            await dispatch_turn_start(ctx.session_id, ctx.ui_turn_id)
        except Exception:
            eng._log.exception("mods_turn_start_failed session=%s", ctx.session_id)
    else:
        eng._log.info(
            "turn_resume session=%s turn=%s agent=%s",
            ctx.session_id, ctx.turn_id, ctx.agent_id,
        )


async def stream_graph_staged(
    graph,
    config: dict,
    input_state: dict | None = None,
    command=None,
) -> None:
    """staged 版 ``_stream_graph`` — 语义复制，骨架见模块 docstring 对照表。

    批次 5-3：逐 turn 资源全走 ``AsyncExitStack``（入口 async with 进入）。
    压栈序 E1→E6，LIFO 弹出序 = legacy finally 的逐行清理序：

    =========  =============================  ==========================
    弹出序     条目                           legacy engine.py 行号
    =========  =============================  ==========================
    E6         coal.close（合帧器 flush）      1746-1749
    E5         _ka.cancel（keepalive）         1658-1661
    E4         stop_waiter.cancel              1662-1663
    E3         _TURN_STOP.pop                  1666-1668
    E2         条件收尾组（注册表④+终态钩子）  1669-1760
    E1         ask_budget token reset          121-126（legacy 不显式
                                               reset，靠 task 死亡；栈化
                                               后同 context LIFO reset，
                                               递归接管段的 token 链正确）
    =========  =============================  ==========================

    结构收益（根治三次泄漏事故的结构病）：每个资源的 acquire 与 release
    登记钉在同一行邻域（新增资源不再依赖「记得去 finally 补配对」），且
    AsyncExitStack 保证任一条目抛异常时其余条目仍执行——legacy finally
    里 steer_keep_channel 等未包 try 的调用一旦抛出会跳过其后全部清理。
    """
    # Arm the ask_user budget for this turn segment. Every segment (the invoke
    # and each resume) funnels through here, and each runs in its own asyncio
    # task whose context dies with it — a leaked budget used to rely on that
    # death; 批次 5-3 起同 context 显式 LIFO reset（栈条目 E1），嵌套的
    # steer-continue/auto-retry 递归段 token 链逐层正确回滚。
    async with AsyncExitStack() as stack:
        _ask_tok = eng.begin_ask_budget()
        # 【栈条目 E1】最先压 → 最后弹：预算贯穿整个收尾段（finalize 里
        # 若有人 ask_user 仍可用），全部资源清完才还。
        stack.callback(reset_ask_budget, _ask_tok)
        ctx = StreamTurnCtx(graph, config, input_state, command)
        try:
            _setup_identity(ctx, stack)
            _setup_io(ctx, stack)
            _setup_stream(ctx)
            await _emit_turn_start(ctx)

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
            # `stop` handler); cancelled by 栈条目 E4（批次 5-3 起，原 finally
            # 第三句）。Sharing the
            # per-chunk asyncio.wait below means a stop lands promptly even while
            # a long tool/LLM step holds __anext__ — no task.cancel() needed.
            ctx.stop_waiter = asyncio.ensure_future(ctx.stop_evt.wait())
            # 【栈条目 E4】stop 等待任务取消（finally 第三句）。E3 之前压栈
            # → E3 之后弹出；one-waiter-per-turn，登记即配对。
            stack.callback(_cancel_stop_waiter, ctx)

            async for mode, payload in chunked_stream(ctx):
                if mode == "messages":
                    chunk, _msg_meta = payload
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
                    await handle_messages_text(ctx, chunk)
                    await handle_messages_tool_chunks(ctx, chunk)
                elif mode == "custom":
                    # 工具心跳（graph.TOOL_HEARTBEAT_S）：chunked_stream 收到任何
                    # chunk 都会重置 stall 看门狗——custom 帧到这一步使命已完成，
                    # 不下发前端（前端对工具运行态已有 tool.start/end 表达）。
                    # 例外：模型退避透传（graph._ainvoke_rate_limited）——provider
                    # 的原始错误全文必须到前端（2026-10-27 e48be8a3：静默退避
                    # 把「套餐无权限」伪装成卡死），用户看着它决定 ⏹ 与否。
                    await handle_custom(ctx, payload)
                    continue
                elif mode == "updates":
                    await handle_updates(ctx, payload)

            for question in ctx.parked_questions:
                await publish_parked_question(ctx, question)
            await finalize_mod.post_stream_flush(ctx)
            if not ctx.saw_interrupt:
                await finalize_mod.finalize_completed(ctx)
            else:
                eng._log.info(
                    "turn_done session=%s turn=%s status=paused_at_interrupt",
                    ctx.session_id, ctx.turn_id,
                )
        except eng.TurnStopped:
            await closeout.handle_turn_stopped(ctx)
        except Exception as e:
            if await closeout.handle_turn_error(ctx, e):
                # steer-continue / auto-retry 分支已递归接管（legacy 的
                # except 内 return）；finally 仍照常执行。
                return
        # 批次 5-3：legacy 的 finally 清理段整体栈化（E1-E6，弹出序=
        # 原逐行清理序，见 stream_graph_staged docstring 表）——async with
        # 退出即全部弹出，except 分支 return 前亦然（Python 语义）。
