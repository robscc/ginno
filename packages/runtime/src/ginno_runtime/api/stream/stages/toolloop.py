"""toolloop 阶段 — 工具调用帧、args 流、工具结果与错误路径（批次 5-2）。

复制自 legacy ``engine._stream_graph`` 的对应段（原行号见各函数 docstring）。
变换规则同 ``ctx.py`` 模块 docstring：局部变量 → ``ctx.``；engine 模块级
名字 → ``eng.`` 属性。
"""

from __future__ import annotations

from langchain_core.messages import ToolMessage

from pathlib import Path

from .. import engine as eng
from ..engine import _session_workspace  # noqa: TC001 — re-exported helper
from .ctx import StreamTurnCtx
from .permission import handle_interrupt
from ....editdiff import make_edit_diff


def _maybe_edit_diff(
    nm: str, ctx: StreamTurnCtx, tc_id: str
) -> dict | None:
    """编辑类工具的展示 diff（best-effort，调用方已 try/except 兜底）。

    edit_file：old/new 直取本 turn 的工具参数（ctx.tool_args_by_id，
    精确到这次调用，无并发竞态）；write_file：新内容取参数 content，
    旧内容取 files/baselines.py 的首写前快照（无快照 = 新文件全绿）。
    相对路径按 builtin._ws 同款语义绑到 session workspace（与
    effects.py 的 touched 解析对齐），baseline key 才能对上。
    """
    if nm not in ("edit_file", "write_file", "bash"):
        return None
    # ctx.tool_args_by_id 的两种形态（e2e 实录翻车点）：流式 chunk 路径
    # 存 dict（handle_messages_tool_chunks）；agent updates 路径存
    # (nm, args) 二元组（本文件 350 行）。tuple 里连工具名都有——一并
    # 作为名字兜底链的一环。
    raw = ctx.tool_args_by_id.get(tc_id)
    args: dict = {}
    if isinstance(raw, tuple) and len(raw) == 2 and isinstance(raw[1], dict):
        args = raw[1]
    elif isinstance(raw, dict):
        args = raw
    # bash 补丁识别（P2 #3）：heredoc 里的 git apply/apply_patch 补丁体
    # 本身就是 unified diff——直接包装，不重算。
    if nm == "bash":
        from ....editdiff import _PATCH_HEREDOC, wrap_patch_diff

        cmd = args.get("command") or ""
        m = _PATCH_HEREDOC.search(cmd)
        if m:
            return wrap_patch_diff(m.group(2))
        return None
    path = args.get("path") or ""
    if not path:
        return None
    if nm == "edit_file":
        old, new = args.get("old"), args.get("new")
        if old is None or new is None:
            return None
        return make_edit_diff("edit", path, old, new)
    new = args.get("content")
    if new is None:
        return None
    old: str | None = None
    try:
        pp = Path(path).expanduser()
        if not pp.is_absolute():
            pp = Path(str(_session_workspace(ctx.slug, ctx.session_id) / path)).expanduser()
        from ....files.baselines import get_baseline

        entry = get_baseline(ctx.session_id, pp)
        if entry and entry.get("content") is not None:
            old = entry["content"]
    except Exception:  # noqa: BLE001 — baseline 缺失按新文件处理
        old = None
    return make_edit_diff("add" if old is None else "overwrite", path, old, new)


async def handle_messages_tool_chunks(ctx: StreamTurnCtx, chunk) -> None:
    """legacy engine.py:652-727 的 messages 模式 tool_call_chunks 段。"""
    tool_calls = getattr(chunk, "tool_call_chunks", None)
    if tool_calls:
        for tc in tool_calls:
            tc_id = tc.get("id")
            tc_index = tc.get("index")
            tc_name = tc.get("name")
            # Fire one tool.start per distinct tool_call id (the
            # chunk that carries the name). See started_tool_ids —
            # keying on id lets parallel tool calls each surface.
            if tc_name and tc_id and tc_id not in ctx.started_tool_ids:
                ctx.started_tool_ids.add(tc_id)
                ctx._tool_name_by_id[tc_id] = tc_name
                if (
                    tc_name in eng.RENDER_TOOL_NAMES
                    or tc_name in eng.WORKFLOW_TOOL_NAMES
                    or tc_name in eng.ARTIFACT_TOOL_NAMES
                ):
                    ctx.special_ids[tc_id] = tc_name
                    continue  # surfaced as widget/ref/workflow block, not a tool bubble
                # 保序（2026-11-15 事故根因）：tool_call chunk 到达时
                # 正文尾片可能还在 33ms 合帧缓冲里。裸发 tool.start 会
                # 插队 → 前端先建工具块、后收文本尾片 → 正文被工具行
                # 拦腰切断（尾片甚至以句号开头）。updates 分支早有此
                # flush，messages 模式这条路径漏了。
                await ctx._coal.flush()
                await ctx.safe_send(
                    ctx.emit("tool.start", {"name": tc_name, "id": tc_id})
                )
                # AgentTeam working 展示文本的数据源（backlog P2-10）：
                # teams.status._working_detail 读 last_tool，但此前
                # 全仓无任何写入点 → working 永远显示「正在运行」。
                # 纯展示字段，绝不参与语义态判断（设计 §3.4）。
                _sess_entry = eng.shared._SESSIONS.get(ctx.session_id)
                if _sess_entry is not None:
                    _sess_entry["last_tool"] = tc_name
            # Accumulate args fragments from streaming chunks.
            # Streaming chunks only carry ``id`` on the first
            # fragment; subsequent ones use ``index``. Resolve
            # the id by matching the index to the most recent
            # tool call that hasn't yet received complete args.
            if not tc_id and tc_index is not None:
                for sid in reversed(list(ctx.started_tool_ids)):
                    if sid not in ctx._emitted_tool_args:
                        tc_id = sid
                        break
            args_frag = tc.get("args") or ""
            if tc_id and args_frag and tc_id not in ctx._emitted_tool_args:
                ctx._partial_tool_args[tc_id] = (
                    ctx._partial_tool_args.get(tc_id, "") + args_frag
                )
                raw = ctx._partial_tool_args[tc_id]
                # 流式参数预览（2026-11-21 用户需求「能显示多少信息就显示
                # 多少信息」）：大 JSON args 生成期可达数十秒，此前只有
                # tool.start 的 "…" 气泡干等。这里按 300ms 节流下发累计
                # raw 的尾部快照（替换式、无需 JSON 可解析，重连/乱序均
                # 幂等）；完整可解析后 tool.args 终态接管，前端清流态。
                import time as _time

                _now = _time.monotonic()
                if _now - ctx._last_args_tail_ts.get(tc_id, 0.0) >= 0.3:
                    ctx._last_args_tail_ts[tc_id] = _now
                    # 保序对齐（2026-11-15 纪律）：非 delta 事件直发前先
                    # flush 合帧缓冲——args 生成期与 text delta 互斥，理论
                    # 无序风险≈0，但对齐 tool.start/tool.args 的既有写法。
                    await ctx._coal.flush()
                    await ctx.safe_send(
                        ctx.emit(
                            "tool.args_delta",
                            {"id": tc_id, "tail": raw[-240:], "total": len(raw)},
                        )
                    )
                try:
                    from ginno_runtime import jsonx as _json

                    parsed = _json.loads(raw)
                    if isinstance(parsed, dict):
                        nm = ctx._tool_name_by_id.get(tc_id, "")
                        preview = eng._tool_args_preview(nm, parsed)
                        if preview:
                            ctx._emitted_tool_args.add(tc_id)
                            await ctx._coal.flush()  # 保序：同 tool.start
                            await ctx.safe_send(
                                ctx.emit(
                                    "tool.args", {"id": tc_id, "preview": preview}
                                )
                            )
                except (ValueError, TypeError):
                    pass  # incomplete JSON, keep accumulating


async def handle_updates(ctx: StreamTurnCtx, payload) -> None:
    """legacy engine.py:760-765 的 updates 模式分发(节点路由)。

    agent/tools/permission 节点在本模块;``__interrupt__`` 分派到
    permission 阶段(同一 for 循环内的 elif,拆分后路由顺序不变)。
    """
    # 合帧保序：updates 携带 tool.start/end、permission.request 等
    # 非 delta 事件——先把缓冲里的文本 flush 出去，保证「文本在
    # 工具事件前」的因果顺序不被窗口打乱。
    await ctx._coal.flush()
    # payload is {node_name: state_delta} OR {"__interrupt__": (Interrupt, ...)}
    for node_name, delta in (payload or {}).items():
        if node_name == "agent":
            await _updates_agent(ctx, delta)
        elif node_name == "tools":
            await _updates_tools(ctx, delta)
        elif node_name == "permission":
            await _updates_permission(ctx, delta)
        elif node_name == "__interrupt__":
            await handle_interrupt(ctx, delta)


async def _updates_agent(ctx: StreamTurnCtx, delta) -> None:
    """legacy engine.py:766-995 的 updates/agent 节点段。"""
    from .... import server_shared as shared

    # Recursion pre-warn: agent supersteps ≈ half the
    # budget (each round is agent + tools), warn once when
    # the remainder narrows to the margin.
    ctx._agent_steps += 1
    if (
        ctx._r_limit
        and not ctx._r_warned
        and ctx._agent_steps >= eng.recursion_warn_threshold(ctx._r_limit)
    ):
        ctx._r_warned = True
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
            if ctx._is_subagent_session
            else (
                "Stop issuing new tool calls and immediately"
                " answer the user with what you have gathered"
                " so far.",
                "请停止发起新的工具调用，基于已有材料"
                "立即给用户答复。",
            )
        )
        shared.steer_enqueue(
            ctx.session_id,
            {
                "steer_id": (
                    f"{eng.STEER_ID_SYSTEM_PREFIX}{ctx.turn_id}"
                ),
                # Model-facing steering (i18n 分流规则):
                # inline bilingual t(), never a catalog key.
                "text": eng.t(
                    "[System reminder] This turn is about to "
                    f"hit the step limit (~{2 * ctx._agent_steps}/"
                    f"{ctx._r_limit} steps used). {_wrap_en}",
                    "【系统提醒】本回合即将达到步数上限"
                    f"（已用约 {2 * ctx._agent_steps}/{ctx._r_limit} 步）。"
                    f"{_wrap_zh}",
                ),
            },
        )
    # Superstep COMMIT point: everything streamed so far is
    # checkpointed, so the uncommitted segment restarts here
    # (a user stop only persists what's still uncommitted).
    ctx.seg_text.clear()
    for m in (delta or {}).get("messages", []):
        # Server-side web_search (Anthropic protocol): the
        # hits were buffered off the stream and belong to
        # the assistant message committing here. Register
        # them as turn sources so the [sN] ids the model
        # cites from its answer resolve. Best-effort — the
        # registry is a no-op outside a live turn.
        if (
            getattr(m, "type", None) in ("ai", "AIMessage")
            and ctx._SERVER_SEARCH_BUFFER
        ):
            registered = [
                s
                for hits in ctx._SERVER_SEARCH_BUFFER
                for s in eng.cit.register_server_search_hits(ctx.session_id, hits)
            ]
            if registered:
                # The one visible trace of a gateway-side
                # search: no local tool runs, so without
                # this line there is nothing in the log to
                # tell server search from parametric recall.
                eng._log.info(
                    "server_search session=%s turn=%s hits=%d urls=%s",
                    ctx.session_id,
                    ctx.turn_id,
                    len(registered),
                    ",".join(s["identity"] for s in registered)[:400],
                )
            ctx._SERVER_SEARCH_BUFFER.clear()
        # Z.ai/GLM gateway server-side search — register the
        # hits and close the tool bubble (see the helper).
        if getattr(m, "type", None) in ("ai", "AIMessage"):
            await eng._emit_glm_server_search(
                ctx.safe_send, ctx.emit, ctx.session_id, ctx.turn_id, m,
                ctx._glm_started,
            )
        # A steered message COMMITTED with this superstep
        # (docs/steering-design.md §3.2): its ack was already
        # sent at drain time (see _steer_absorbed), so all
        # that is left is to retire the uncommitted-drain
        # record — from here on the message is in the
        # checkpoint, and the heal must not touch it again.
        if (getattr(m, "additional_kwargs", None) or {}).get("ginno_steer"):
            shared.steer_clear_inflight(ctx.session_id)
        # D2 — per-call usage + session accumulator. Only
        # complete AIMessages carry usage_metadata (never
        # streamed chunks), so this fires once per LLM call.
        u = eng.extract_usage(m)
        if getattr(m, "id", None):
            ctx._last_ai_id = m.id
        if u:
            acc = eng._USAGE_BY_SESSION.setdefault(
                ctx.session_id, eng.empty_usage()
            )
            eng.add_usage(acc, u)
            eng.add_usage(ctx._turn_usage, u)
            # ContextRing 平滑基线（见 _turn_ctx_tokens 声明处
            # 注释）：漏报轮（input=0）保旧值不动。
            _in_tok = u["input_tokens"] or 0
            if _in_tok > 0:
                if ctx._turn_ctx_seen:
                    ctx._turn_ctx_tokens = max(ctx._turn_ctx_tokens, _in_tok)
                else:
                    ctx._turn_ctx_tokens = _in_tok
                    ctx._turn_ctx_seen = True
            # Persist the call into the global usage log
            # (usage-stats-design.md §4). Best-effort: the
            # store never raises, so this cannot break the
            # turn. provider/model come from the session's
            # meta (resolved at create/rebuild time);
            # resolve_session_model additionally falls back
            # to the on-disk meta so a restarted sidecar
            # still attributes calls correctly (the ring
            # window would otherwise collapse to the 200k
            # fallback — 2026-11-07).
            _prov, _model = eng.resolve_session_model(
                ctx.session_id, eng._SESSIONS.get(ctx.session_id)
            )
            eng.usage_store.record(
                input_tokens=u["input_tokens"],
                output_tokens=u["output_tokens"],
                cache_read_tokens=u["cache_read_tokens"],
                cache_creation_tokens=u["cache_creation_tokens"],
                reasoning_tokens=u.get("reasoning_tokens", 0),
                provider=_prov,
                model=_model,
                source=ctx.usage_source,
                session_id=ctx.session_id or None,
                project_slug=ctx.slug or None,
                agent_id=ctx.agent_id or None,
                turn_id=ctx.turn_id,
            )
            await ctx.safe_send(
                ctx.emit(
                    "usage",
                    {
                        "turn": u,
                        "session": dict(acc),
                        "cache_hit_ratio": eng.cache_hit_ratio(acc),
                        # 上下文占用（TopBar 圆环）：ContextRing 的
                        # 分子用 _turn_ctx_tokens（turn 级平滑：首
                        # 个 usage 定基线、步间取 max、漏报轮保旧），
                        # 而非单次调用的原始 input——修复「占比
                        # 一会儿 10% 以下一会儿 60%+」的锯齿。
                        # 窗口走 context_window_for 解析链不变；
                        # 占用是「当前状态」不是累计，前端每轮
                        # 直接替换（usage.stats-design）。
                        "context": eng.context_state(
                            ctx._turn_ctx_tokens, _prov, _model,
                        ),
                    },
                )
            )
        for tc in getattr(m, "tool_calls", []) or []:
            nm = tc.get("name")
            args = tc.get("args") or {}
            tc_id = tc.get("id")
            ctx.tool_args_by_id[tc_id] = (nm, args)
            if (
                nm in eng.RENDER_TOOL_NAMES
                or nm in eng.WORKFLOW_TOOL_NAMES
                or nm in eng.ARTIFACT_TOOL_NAMES
            ):
                ctx.special_ids[tc_id] = nm
            elif tc_id:
                # Full tool-call input on its own event
                # (ginno exec 的 stream-json 转录用它回填
                # tool_use.input；前端不消费该事件，属
                # 纯增量）。updates 提交点 args 一定完整。
                if tc_id not in ctx._emitted_tool_inputs:
                    ctx._emitted_tool_inputs.add(tc_id)
                    await ctx.safe_send(
                        ctx.emit("tool.input", {"id": tc_id, "input": args})
                    )
                # Show WHAT is running: surface the tool call's
                # args (e.g. the bash command) on the pending
                # tool bubble. Fires from the agent update —
                # after args are complete, before the tool runs.
                # Skip if already emitted from the chunks stream
                # (streaming args accumulation above).
                if tc_id not in ctx._emitted_tool_args:
                    preview = eng._tool_args_preview(nm, args)
                    if preview:
                        await ctx.safe_send(
                            ctx.emit(
                                "tool.args", {"id": tc_id, "preview": preview}
                            )
                        )
            if nm == "render_widget":
                await ctx.safe_send(
                    ctx.emit("widget.emit", eng.widget_event(args, tc_id))
                )
            elif nm == "attach_ref":
                from .streamio import _emit_attach_tags

                await _emit_attach_tags(
                    ctx,
                    [
                        {
                            "kind": args.get("kind", "file"),
                            "name": args.get("name", ""),
                            "ref_id": args.get("ref_id", ""),
                        }
                    ],
                )
            elif nm == "artifact_register":
                kind = args.get("kind", "file")
                ref = args.get("ref", "")
                if kind == "file":
                    ref = eng._normalize_file_ref(ctx.slug, ctx.session_id, ref)
                art = eng.art_store.add_artifact(
                    ctx.slug,
                    kind,
                    args.get("name", ""),
                    ref,
                    ctx.session_id,
                )
                if kind == "file":
                    eng._register_artifact_file(ctx.slug, ctx.session_id, art, ref)


async def _updates_tools(ctx: StreamTurnCtx, delta) -> None:
    """legacy engine.py:996-1058 的 updates/tools 节点段。"""
    import re

    msgs = (delta or {}).get("messages", [])
    for m in msgs:
        tc_id = getattr(m, "tool_call_id", None)
        raw = getattr(m, "content", "") or ""
        nm = ctx.special_ids.get(tc_id)
        if nm in eng.WORKFLOW_TOOL_NAMES:
            mm = re.search(r"run_id=([0-9a-f]{6,})", raw)
            rid = mm.group(1) if mm else None
            # 盘上 run 行优先(2026-11-02 双跑事故):RUN_CACHE
            # 只在工具创建时写一次,run 失败后快照仍是创建时
            # 的 "running"——workflow_run_status 的输出同样
            # 命中本正则,拿过期快照过状态守卫会把终态 run
            # 再 spawn 一遍(转录会话因此翻倍落进侧栏)。
            run = (eng.wf_store.get_run(rid) if rid else None) or (
                eng.RUN_CACHE.get(rid) if rid else None
            )
            if run:
                # 唤起 in-session (design A): bind the chat-triggered
                # run to this session and drive it with the real engine.
                if not run.get("present_in_session_id"):
                    run["session_id"] = ctx.session_id
                    run["present_in_session_id"] = ctx.session_id
                    run["updated"] = eng.time.time()
                    eng.wf_storemod._write_json(
                        eng.wf_storemod._run_path(run["id"]), run
                    )
                await ctx.safe_send(
                    ctx.emit(
                        "run.bind",
                        {
                            "run_id": run["id"],
                            "workflow_id": run["workflow_id"],
                            "present_in_session_id": ctx.session_id,
                        },
                    )
                )
                _wf_task = eng._WF_RUN_TASKS.get(run["id"])
                if (
                    _wf_task is None or _wf_task.done()
                ) and run.get("status") == "running":
                    # 2026-10-29 NameError 实录：此分支曾
                    # 裸引用两个未导入的名字（ae47b90 引入，
                    # 默认 ruff F821 抓不到）。这里懒加载，
                    # 顺便避免与 workflows_runs 的潜在环。
                    from ...workflows_runs import (  # noqa: F401
                        _run_workflow_bg,
                        _spawn_run_task,
                    )

                    _spawn_run_task(
                        run["id"],
                        _run_workflow_bg(
                            run["id"],
                            run["workflow_id"],
                            run.get("context_override"),
                            ctx.session_id,
                        ),
                    )
                await ctx.safe_send(ctx.emit("workflow.emit", {"run": run}))
            # no ordinary tool bubble for workflow tools
        elif nm in eng.RENDER_TOOL_NAMES or nm in eng.ARTIFACT_TOOL_NAMES:
            pass  # widget/ref emitted at agent-update; no bubble
        elif tc_id:
            payload = {
                "id": tc_id,
                "content": eng._truncate_for_ws(eng._tool_content_str(raw)),
            }
            # 编辑 diff 显示（2026-11-21 P0/P1）：edit_file/write_file 成功
            # 时附纯展示 diff——edit 从 args 取 old/new（零竞态），write 的
            # 旧内容取首写前 baseline。best-effort：绝不因 diff 失败破坏
            # tool.end；diff 不进 content（不回灌模型上下文）。
            # ⚠ nm 来自 special_ids（仅 workflow/render 特殊工具映射）——
            # 普通工具名为 None，必须回退 _tool_name_by_id（tool.start 时
            # 记录），否则 edit_file 永远命中不了（e2e 实录首跑翻车点）。
            try:
                # 名字三级兜底：special_ids（特殊工具）→ ToolMessage.name
                # （LangGraph 执行后必带，fake/一次性 tool_calls 场景的唯一
                # 来源，e2e 实录翻车点）→ _tool_name_by_id（流式 chunk 路径）。
                tool_nm = (
                    nm
                    or getattr(m, "name", None)
                    or ctx._tool_name_by_id.get(tc_id)
                )
                d = _maybe_edit_diff(tool_nm, ctx, tc_id)
                if d is not None:
                    payload["diff"] = d
            except Exception:  # noqa: BLE001
                pass
            await ctx.safe_send(ctx.emit("tool.end", payload))
            # File-reactive side effects (docs §7.5): invalidate
            # previews of files this tool touched; auto-register
            # + open derived analysis results.
            await eng._tool_file_effects(
                ctx.safe_send, ctx.emit, ctx.slug, ctx.session_id,
                ctx.tool_args_by_id.get(tc_id), raw,
                ctx=ctx,
            )
            # Code-panel S3: agent file writes/edits →
            # ``code.changed`` (parsed from the marker
            # write_file / edit_file append to their own
            # result; there is no Hook for this — its
            # PostToolUse is declared but never dispatched).
            await eng._emit_code_changes(
                ctx.safe_send, ctx.emit, ctx.slug, ctx.session_id, raw,
                ctx.ui_turn_id,
            )


async def _updates_permission(ctx: StreamTurnCtx, delta) -> None:
    """legacy engine.py:1059-1118 的 updates/permission 节点段。"""
    # resolve "running" tool bubbles that were denied by
    # tools_allow / hooks / policy / user (not streamed)
    for m in (delta or {}).get("messages", []):
        c = getattr(m, "content", "")
        if isinstance(c, str) and c.startswith(eng.BLOCK_PREFIX):
            rest = c[len(eng.BLOCK_PREFIX):]
            name, _, reason = rest.partition("]")
            await ctx.safe_send(
                ctx.emit(
                    "tool.end",
                    {
                        # 2026-11-15：必须带 tool_call_id。
                        # _blocked_tool_msg 早已把它放进 ToolMessage
                        # （graph.py:110），此前这里只从 content 字符串
                        # 抠出 name 就把 id 丢了 → 前端退化到「按工具名
                        # 匹配」，而那个匹配不要求 pending、又从数组头
                        # 取第一个：denial 文案会覆盖到**前面某个已完成
                        # 的同名工具气泡**上，真正被拦的那个永远停在
                        # 「执行命令中…」。同名工具在一个 turn 内多次
                        # 调用（bash 重试最常见）即触发。
                        # 紧邻的 elif 分支本来就读了 tool_call_id，
                        # 同一循环两条分支一条带 id 一条不带是纯遗漏。
                        "id": getattr(m, "tool_call_id", None),
                        "name": name.strip(),
                        "content": reason.strip() or c,
                    },
                )
            )
        elif isinstance(m, ToolMessage):
            # Mods {result} takeover (design §15.5): the
            # tool never executed — the mod's value rides a
            # synthetic ToolMessage. Close the bubble that
            # tool.start opened with it (special tools keep
            # their no-bubble contract).
            tc_id = getattr(m, "tool_call_id", None)
            nm = ctx.special_ids.get(tc_id)
            if tc_id and nm not in (
                eng.WORKFLOW_TOOL_NAMES
                | eng.RENDER_TOOL_NAMES
                | eng.ARTIFACT_TOOL_NAMES
            ):
                await ctx.safe_send(
                    ctx.emit(
                        "tool.end",
                        {
                            "id": tc_id,
                            "content": eng._truncate_for_ws(
                                eng._tool_content_str(getattr(m, "content", ""))
                            ),
                        },
                    )
                )
