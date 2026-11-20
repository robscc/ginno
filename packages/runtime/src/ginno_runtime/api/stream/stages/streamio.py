"""streamio 阶段 — 帧出口、合帧、token.delta 文本流、看门狗（批次 5-2）。

复制自 legacy ``engine._stream_graph`` 的对应段（原行号见各函数 docstring）。
变换仅两类，见 ``ctx.py`` 模块 docstring 的纪律说明：

1. 局部变量 → ``ctx.`` 属性（同名）；
2. legacy 模块级名字 → ``eng.`` 属性（engine 命名空间，monkeypatch 等价）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from .. import engine as eng
from .ctx import StreamTurnCtx


def make_emit(ctx: StreamTurnCtx):
    """legacy engine.py:190-199 的 emit 闭包（闭包变量→ctx 属性）。"""

    def emit(event: str, data: dict) -> str:
        # Vibe Island notch 状态收口(engine 的 safe_send 不走
        # _push_session_event,所以这里单独埋):开关关闭时 no-op,绝不抛。
        eng._vibe_island.on_turn_event(ctx.session_id, event, data)
        # frame_session is stamped on every turn frame: the client drops any
        # frame whose frame_session isn't the socket's own session, making a
        # cross-session render leak structurally impossible (2026-10-01).
        # Separate key from data["session_id"] (subagent payload semantics).
        eng._journal_frame(ctx.session_id, event, data)
        return eng._ev(
            event, {"frame_session": ctx.session_id, **data}, ctx.ui_turn_id
        )

    return emit


def make_safe_send(ctx: StreamTurnCtx):
    """legacy engine.py:225-247 的 safe_send 闭包(ws_closed nonlocal→ctx)。"""

    async def safe_send(data: str) -> None:
        socks = eng._SESSION_WS.get(ctx.session_id) or []
        alive: list[Any] = []
        for w in socks:
            if await eng._try_send(w, data):
                alive.append(w)
        eng._SESSION_WS[ctx.session_id] = alive
        ctx.ws_closed = not alive
        # 节点侧 turn 事件汇(可选,默认未注册 → 仅一次 dict.get,零影响):
        # a2a 入站 worker 无活 socket,上面所有帧本会被静默丢弃;注册后
        # 把同一帧交给 sink(写进度 JSONL → SSE 转发给调用方)。解析/
        # sink 异常一律吞掉——旁路绝不影响主 turn(best-effort)。
        sink = eng.get_sink(ctx.session_id)
        if sink is not None:
            try:
                frame = eng.json.loads(data)
                event = frame.pop("event", "")
                if event:
                    sink.offer(event, frame)
            except Exception:  # noqa: BLE001
                pass

    return safe_send


async def keepalive(ctx: StreamTurnCtx) -> None:
    """legacy engine.py:254-268 的 keepalive 协程(15s 保活帧)。"""
    try:
        while True:
            await asyncio.sleep(15)
            await ctx.safe_send(ctx.emit("keepalive", {}))
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


def make_steer_absorbed(ctx: StreamTurnCtx):
    """legacy engine.py:289-345 的 _steer_absorbed 闭包。

    _steer_user_arrived nonlocal → ctx 属性(闭包写,循环读)。
    """
    from .... import server_shared as shared

    async def _steer_absorbed(entries: list[dict]) -> None:
        shared.steer_mark_inflight(ctx.session_id, entries)
        for _e in entries:
            if eng._is_user_steer(_e):
                ctx._steer_user_arrived = True
        for _e in entries:
            eng._log.info(
                "steer_absorbed session=%s steer=%s turn=%s",
                ctx.session_id,
                _e.get("steer_id"),
                ctx.turn_id,
            )
            # digest 吸收进 channel 会话 = 呈现盲区(渠道无桌面注入带,
            # 事后「查无此物」——2026-11-06 digest 排查实录):
            # 点名 warn,提示该 digest 仅存在于 turn 上下文。
            _wmsg = (_e.get("extra_kwargs") or {}).get("ginno_worker_msg")
            if isinstance(_wmsg, dict) and _wmsg.get("kind") == "digest":
                _srow = eng._SESSIONS.get(ctx.session_id) or {}
                if str(_srow.get("type") or "").startswith("channel"):
                    eng._log.warning(
                        "digest_absorbed_into_channel_session session=%s "
                        "steer=%s — visible only in turn context, no "
                        "standalone message will exist",
                        ctx.session_id, _e.get("steer_id"),
                    )
            await ctx._coal.flush()  # 保序:steer 注入带不插队到缓冲文本前
            await ctx.safe_send(
                ctx.emit(
                    "steer.absorbed",
                    {
                        "steer_id": _e.get("steer_id"),
                        "injected_at": _e.get("injected_at"),
                        # 原文随事件回传:渠道侧注入的 steer(channels.steer_channel_turn)
                        # 在桌面端没有本地队列条目,带 text 才能实时渲染注入带。
                        "text": _e.get("text") or "",
                        # 后台任务注入(tasks/inject):origin + ginno_task_event
                        # 让前端把带折进 TaskCard 而非裸文本带(评审决策 4)。
                        "origin": _e.get("origin") or "",
                        "task": (
                            (_e.get("extra_kwargs") or {}).get("ginno_task_event")
                            if isinstance(_e.get("extra_kwargs"), dict)
                            else None
                        ),
                    },
                )
            )

    return _steer_absorbed


async def chunked_stream(ctx: StreamTurnCtx):
    """legacy engine.py:508-536 的 chunked_stream 闭包(stall 看门狗+stop)。

    CHUNK_TIMEOUT_S 经 ``eng.`` 属性读 —— monkeypatch 落在 engine 命名空间
    (_PatchMirrorModule),staged 必须读到 patch 后的值才与 legacy 等价。
    """
    it = ctx.stream.__aiter__()
    while True:
        nxt = asyncio.ensure_future(it.__anext__())
        nxt.add_done_callback(
            lambda t: None if t.cancelled() else t.exception()
        )  # mark result retrieved, silence "never retrieved" warnings
        done, _ = await asyncio.wait(
            {nxt, ctx.stop_waiter},
            timeout=eng.CHUNK_TIMEOUT_S,
            # FIRST_COMPLETED, not the ALL_COMPLETED default: the
            # stop waiter stays pending until a stop lands, and with
            # ALL_COMPLETED every chunk would block to the timeout.
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ctx.stop_waiter in done:
            nxt.cancel()
            # Same abandonment protocol as the stall watchdog below:
            # block the detached run's late checkpoint writes, then
            # unwind through the dedicated TurnStopped handler (state
            # heal + turn.stopped event). Cooperative event, NOT
            # task.cancel — retry layers swallow CancelledError.
            eng.ABANDONED_TURNS.add(ctx.turn_id)
            raise eng.TurnStopped()
        if not done:
            nxt.cancel()
            # Block any late checkpoint writes from this detached run
            # (see ABANDONED_TURNS) so it can't roll back a retry.
            eng.ABANDONED_TURNS.add(ctx.turn_id)
            raise RuntimeError(
                f"model/stream stall: no chunk for {eng.CHUNK_TIMEOUT_S:.0f}s"
            )
        try:
            yield nxt.result()
        except StopAsyncIteration:
            return


async def handle_messages_text(ctx: StreamTurnCtx, chunk) -> None:
    """legacy engine.py:549-651 的 messages 模式文本/thinking/叙述/attach 段。

    调用方(run.py)已做 chunk.type gate(``("ai","AIMessageChunk")``)。
    """
    content = getattr(chunk, "content", "")
    if isinstance(content, list):
        for b in content:
            btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if btype == "thinking":
                txt = b.get("thinking") or b.get("text") or ""
                if txt:
                    # 走合帧器而非裸发(2026-11-15):裸发会插队到
                    # 缓冲中的 token.delta 前面,thinking 渲染错位。
                    await ctx._coal.feed("thinking.delta", txt)
            elif btype == "text":
                txt = b.get("text") or ""
                # Z.ai/GLM gateway narration ("**🌐 Z.ai Built-in
                # Tool: …** / **Output:** …_result_summary: […json…]")
                # is machine metadata, not prose — never stream it
                # into the bubble (nor the memory buffer). Blocks
                # stream in fragments, so a block index confirmed
                # as narration stays muted for its later fragments.
                bid = b.get("index") if isinstance(b, dict) else None
                if bid is not None and bid in ctx._ZAI_NARRATION_IDX:
                    if txt:
                        ctx._ZAI_MUTED.setdefault(bid, []).append(txt)
                    continue
                if eng.server_tools.is_server_tool_narration(txt):
                    if bid is not None:
                        ctx._ZAI_NARRATION_IDX.add(bid)
                    if txt:
                        ctx._ZAI_MUTED.setdefault(bid, []).append(txt)
                    continue
                if eng.server_tools.narration_tainted(txt):
                    # A fragment that MIXES prose and narration (the
                    # gateway consolidates a step into one block):
                    # forward the prose prefix only, then mute the
                    # block — the rest is JSON dumps, and the tail
                    # prose comes back on history replay.
                    ctx._ZAI_MUTED.setdefault(bid, []).append(txt)
                    txt = eng.server_tools.split_narration(txt)[0]
                    if bid is not None:
                        ctx._ZAI_NARRATION_IDX.add(bid)
                if txt:
                    txt, _refs = ctx._attach_filter.feed(txt)
                    await _emit_attach_tags(ctx, _refs)
                if txt:
                    ctx.turn_text.append(txt)
                    ctx.seg_text.append(txt)
                    await ctx._coal.feed("token.delta", txt)
            elif btype == "server_tool_use":
                # Z.ai/GLM gateway server-side search request: open
                # the tool bubble the moment the model "calls" it
                # (the real execution happens inside the model);
                # tool.end with the formatted hits lands when the
                # committing message arrives in `updates` mode.
                tid = b.get("id") if isinstance(b, dict) else None
                if tid and tid not in ctx._glm_started:
                    ctx._glm_started.add(tid)
                    # 保序:缓冲里的文本尾片必须先于 tool.start 落地
                    # (messages 模式发射点,updates 分支同款约束)。
                    await ctx._coal.flush()
                    await ctx.safe_send(
                        ctx.emit(
                            "tool.start",
                            {
                                "name": eng.server_tools.DISPLAY_TOOL_NAME,
                                "id": tid,
                            },
                        )
                    )
            elif btype in eng._SERVER_SEARCH_BLOCK_TYPES:
                # Anthropic-protocol server-side web_search: the
                # gateway ran the search, so the hits ride back
                # inside the assistant message. Register them as
                # turn sources or every [sN] the model cites
                # resolves as unverified. Accumulated then drained
                # on the non-streaming final message, which
                # carries the complete block (streamed chunks
                # repeat partial result payloads).
                ctx._SERVER_SEARCH_BUFFER.append(
                    eng._server_search_hits(b, eng._SERVER_SEARCH_BLOCK_TYPES)
                )
    elif isinstance(content, str) and content:
        # Same narration gate for a plain-string AI chunk (models
        # that don't stream blocks); recovered cards are a replay
        # concern — live only needs the prose.
        if eng.server_tools.narration_tainted(content):
            content = eng.server_tools.split_narration(content)[0]
        if content:
            content, _refs = ctx._attach_filter.feed(content)
            await _emit_attach_tags(ctx, _refs)
        if content:
            ctx.turn_text.append(content)
            ctx.seg_text.append(content)
            await ctx._coal.feed("token.delta", content)
    rk = (getattr(chunk, "additional_kwargs", None) or {}).get("reasoning_content")
    if rk:
        await ctx._coal.feed("thinking.delta", rk)


async def handle_custom(ctx: StreamTurnCtx, payload) -> None:
    """legacy engine.py:728-759 的 custom 模式(模型退避 notice;其余吞掉)。"""
    if isinstance(payload, dict) and payload.get("ginno_model_error"):
        me = payload["ginno_model_error"]
        # 2026-11-08 429 显示优化:清洗后的错误文本(去 APIError
        # 前缀与英文括注尾巴,重置时间去重)+ 结构化 reset_at。
        _me_err = eng.clean_model_error_text(me.get("error", ""))
        _me_reset = eng.model_error_reset_at(_me_err)
        _me_params: dict[str, Any] = {
            "error": _me_err,
            "seconds": f"{me.get('wait_s', 0):.0f}",
            "attempt": int(me.get("attempt") or 0),
            "max": int(me.get("max") or 0),
        }
        if _me_reset:
            _me_params["reset_at"] = _me_reset
        await ctx._coal.flush()  # 保序:notice 不插队到缓冲文本前
        await ctx.safe_send(
            ctx.emit(
                "notice",
                {
                    "message": (
                        f"Model API error — retrying in "
                        f"{me.get('wait_s', 0):.0f}s "
                        f"({me.get('attempt')}/{me.get('max')}): "
                        f"{_me_err}"
                    ),
                    "i18n_key": "stream.model_error_backoff",
                    "params": _me_params,
                },
            )
        )
    # T3 LLM 帧的 engine 翻译分支已移除(2026-11-18):
    # bisect1/2 实锤它在 CI(ubuntu) 上导致 runtime-tests 63% 后
    # 卡死 16 分钟被 runner 关停(main 同区间 0.36s;只撤本分支
    # CI 9m44s 通过)。且用户已要求前端不渲染 llm 行——本分支
    # 唯一作用(把 custom 帧翻译成 llm.request/response 发前端)
    # 已无消费方。graph 埋点保留(bisect2 证明无害),事件仍在
    # custom 总线上,未来要恢复前端展示需先查清 Linux 卡死根因
    # (本地 macOS 全绿、writer/flush/队列三假设均已实证否掉,
    # 疑与 ubuntu runner 环境相关)再重新引入。


async def _emit_attach_tags(ctx: StreamTurnCtx, refs: list[dict]) -> None:
    """legacy engine.py:427-447 的 _emit_attach_tags 闭包。

    streamio/toolloop 两阶段共用(legacy 是单闭包,文本流与 updates 的
    attach_ref 工具分支都调它)。
    """
    for r in refs:
        kind = r.get("kind") or "file"
        ref_id = r.get("ref_id") or ""
        name = r.get("name") or ""
        if kind == "file":
            # Same normalization the real tool call gets: a relative
            # path resolves against the session workspace.
            ref_id = eng._normalize_file_ref(ctx.slug, ctx.session_id, ref_id)
        key = (kind, name, ref_id)
        if key in ctx._attach_sent:
            continue
        ctx._attach_sent.add(key)
        await ctx._coal.flush()  # 保序:ref 芯片落在正文解析出它的位置
        await ctx.safe_send(
            ctx.emit("ref.emit", {"kind": kind, "name": name, "ref_id": ref_id})
        )
        if kind in ("file", "doc", "workflow", "link"):
            art = eng.art_store.add_artifact(
                ctx.slug, kind, name, ref_id, ctx.session_id
            )
            if kind == "file":
                eng._register_artifact_file(ctx.slug, ctx.session_id, art, ref_id)
