"""closeout — finalize 阶段的异常收口半边（批次 5-2）。

复制自 legacy ``engine._stream_graph`` 的两个 except 分支（原行号见各
函数 docstring）。与 ``finalize.py``（正常收尾/finally 清理）共同构成
finalize 阶段；拆开只为满足「每文件 ≤600 行」的模块预算。

``handle_turn_error`` 返回 ``True`` 表示 steer-continue / auto-retry 分支
已递归接管本轮（legacy 在 except 块内 ``return``——编排器据此 return，
其后 finally 照常执行，与 legacy 的 Python 语义一致）。
"""

from __future__ import annotations

from .. import engine as eng
from .ctx import StreamTurnCtx


async def handle_turn_stopped(ctx: StreamTurnCtx) -> None:
    """legacy engine.py:1441-1467 — except TurnStopped(用户 stop)。"""
    # User pressed stop: heal the persisted state (dangling tool_calls /
    # uncommitted partial text), tell the clients, exit quietly. No error
    # card, no last_error, no completion side effects (memory/title/
    # citations live under the normal-completion branch below).
    await eng._heal_interrupted_turn(
        ctx.graph, ctx.config, "".join(ctx.seg_text)
    )
    eng._log.info(
        "turn_stopped session=%s turn=%s seg_len=%d",
        ctx.session_id, ctx.turn_id, len("".join(ctx.seg_text)),
    )
    await ctx.safe_send(ctx.emit("turn.stopped", {}))
    # Mods turn.complete for the aborted close-out (§5.3: all three
    # engine close-outs dispatch; detached, observe-only in P0).
    try:
        from ....mods.events import spawn_turn_complete

        spawn_turn_complete(
            ctx.session_id,
            ctx.ui_turn_id,
            answer="".join(ctx.seg_text),
            duration_ms=int((eng.time.monotonic() - ctx._mods_t0) * 1000),
            is_aborted=True,
            reason="aborted",
        )
    except Exception:
        eng._log.exception(
            "mods_turn_complete_failed session=%s", ctx.session_id
        )


async def handle_turn_error(ctx: StreamTurnCtx, e: Exception) -> bool:
    """legacy engine.py:1468-1744 — except Exception。

    返回 True = steer-continue / auto-retry 已递归接管（legacy 的
    ``return``），编排器据此返回；finally 仍照常执行。
    """
    import asyncio

    # A turn the USER steered mid-flight outran the budget it was sized
    # for: resume from the checkpoint with a fresh budget instead of
    # failing, or the user's own request dies at the wall. Bounded by
    # STEER_CONT_MAX. Only for user steers — the system's recursion-warn
    # steer deliberately gets no extension (it exists to stop runaway
    # loops, and more budget would just let one run longer).
    _steer_cont = int(ctx._cfg_conf.get("_steer_cont") or 0)
    if ctx._steer_user_arrived and _steer_cont < eng.STEER_CONT_MAX:
        from langgraph.errors import GraphRecursionError

        if isinstance(e, GraphRecursionError):
            try:
                _snap = await ctx.graph.aget_state(ctx.config)
                _has_ckpt = bool(getattr(_snap, "values", None) or {})
            except Exception:
                _has_ckpt = False
            if _has_ckpt:
                eng._log.warning(
                    "turn_steer_continue session=%s turn=%s cont=%d/%d",
                    ctx.session_id, ctx.turn_id, _steer_cont + 1, eng.STEER_CONT_MAX,
                )
                await ctx.safe_send(
                    ctx.emit(
                        "notice",
                        {
                            "message": (
                                "You steered this turn past its step budget; "
                                "continuing with a fresh budget"
                                f" ({_steer_cont + 1}/{eng.STEER_CONT_MAX})…"
                            ),
                            "i18n_key": "stream.steer_continue",
                            "params": {
                                "attempt": _steer_cont + 1,
                                "max": eng.STEER_CONT_MAX,
                            },
                        },
                    )
                )
                from .run import stream_graph_staged

                await stream_graph_staged(
                    ctx.graph,
                    {
                        **ctx.config,
                        "configurable": {
                            **ctx._cfg_conf,
                            # Derived exec id: the abandoned original's
                            # checkpoint writes would be refused.
                            "turn_id": (
                                f"{ctx.ui_turn_id}--s{_steer_cont + 1}-"
                                f"{eng.uuid.uuid4().hex[:6]}"
                            ),
                            "_ui_turn_id": ctx.ui_turn_id,
                            "_steer_cont": _steer_cont + 1,
                        },
                    },
                    input_state=None,
                    command=None,
                )
                return True
    # Transient provider/network failure (SSL drop, connection error,
    # 429/5xx, stall watchdog): auto-retry from the latest checkpoint with
    # a backoff instead of failing the turn — see AUTO_RETRY_MAX notes.
    # The retry runs under a DERIVED exec turn_id (the abandoned original
    # would have its checkpoint writes refused) while every client-facing
    # surface keeps ui_turn_id. The caller still holds _turn_lock, so the
    # recursive call must NOT re-acquire it (it doesn't — the lock lives in
    # the job wrappers). A user stop during the backoff aborts the retry.
    if not ctx.stop_evt.is_set() and eng._is_transient_model_error(e):
        _attempt = int(ctx._cfg_conf.get("_auto_retry_attempt") or 0)
        if _attempt < eng.AUTO_RETRY_MAX:
            _backoff = eng.AUTO_RETRY_BACKOFF_S[
                min(_attempt, len(eng.AUTO_RETRY_BACKOFF_S) - 1)
            ]
            eng._log.warning(
                "turn_auto_retry session=%s turn=%s attempt=%d/%d backoff=%.0fs err=%s: %s",
                ctx.session_id, ctx.turn_id, _attempt + 1, eng.AUTO_RETRY_MAX,
                _backoff, type(e).__name__, str(e)[:200],
            )
            await ctx.safe_send(
                ctx.emit(
                    "notice",
                    {
                        # Event contract (i18n-design.md §3): English
                        # fallback text + i18n_key + params for the UI.
                        # error 参数带 provider 原始信息（e48be8a3 决策：
                        # 接口错误永远亮给前端，用户自己决定中断与否）；
                        # 展示文本经 clean_model_error_text 清洗（去
                        # APIError 前缀/英文括注，2026-11-08 429 优化）。
                        "message": (
                            f"Model connection error, auto-retrying in "
                            f"{_backoff:.0f}s ({_attempt + 1}/{eng.AUTO_RETRY_MAX}): "
                            f"{eng.clean_model_error_text(e)}"
                        ),
                        "i18n_key": "stream.auto_retry",
                        "params": {
                            "error": eng.clean_model_error_text(e),
                            "seconds": f"{_backoff:.0f}",
                            "attempt": _attempt + 1,
                            "max": eng.AUTO_RETRY_MAX,
                        },
                    },
                )
            )
            await asyncio.sleep(_backoff)
            if not ctx.stop_evt.is_set():
                _retry_conf = {
                    **ctx.config,
                    "configurable": {
                        **ctx._cfg_conf,
                        # Random suffix: a later retry chain of the same
                        # ui turn must never collide with an earlier
                        # chain's abandoned exec id (exact-match gate in
                        # FileCheckpointer.aput/aput_writes).
                        "turn_id": (
                            f"{ctx.ui_turn_id}--a{_attempt + 1}-"
                            f"{eng.uuid.uuid4().hex[:6]}"
                        ),
                        "_ui_turn_id": ctx.ui_turn_id,
                        "_auto_retry_attempt": _attempt + 1,
                    },
                }
                # Resume from the latest checkpoint when one exists (tool
                # work preserved). A failure on the FIRST model call of a
                # fresh session commits nothing — resuming with None would
                # die on EmptyInputError — so re-run the original input
                # instead. Neither available: fall through to the error.
                try:
                    _snap = await ctx.graph.aget_state(ctx.config)
                    _has_ckpt = bool(getattr(_snap, "values", None) or {})
                except Exception:
                    _has_ckpt = False
                if _has_ckpt or ctx.input_state is not None:
                    from .run import stream_graph_staged

                    await stream_graph_staged(
                        ctx.graph,
                        _retry_conf,
                        input_state=None if _has_ckpt else ctx.input_state,
                        command=None,
                    )
                    return True
    eng._log.exception(
        "turn_error session=%s turn=%s", ctx.session_id, ctx.turn_id
    )
    # Event contract (i18n-design.md §3): the raw exception dump (+ remedy
    # hint for known failures) stays the English fallback; the key lets the
    # UI render a localized version.
    _err_fields = eng.turn_error_fields(e)
    # Persist the failure on the session meta so the error card (with its
    # retry action) survives webview reloads and route/session switches —
    # the history endpoint re-surfaces it as the last message. The retry
    # button round-trips ui_turn_id; the handler derives a fresh exec id
    # when this turn was abandoned.
    eng._session_meta_patch(
        ctx.slug,
        ctx.session_id,
        {
            "last_error": {
                "turn_id": ctx.ui_turn_id,
                "message": _err_fields["message"],
                # key+params 随行持久化:history 回放重放错误卡时按事件
                # 契约落地翻译,而不是退化为英文 message(i18n-design §3)。
                "i18n_key": _err_fields.get("i18n_key"),
                "params": _err_fields.get("params"),
                "at": eng.time.time(),
            },
            # Session unread inbox: error turns count as a finished turn
            # (same patch — one index write). The auto-retry branch above
            # `return`s before this point, so retried turns settle once.
            "last_finished": eng.time.time(),
        },
    )
    await ctx.safe_send(
        ctx.emit(
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
        from ....mods.events import spawn_turn_complete

        spawn_turn_complete(
            ctx.session_id,
            ctx.ui_turn_id,
            answer="".join(ctx.seg_text),
            duration_ms=int((eng.time.monotonic() - ctx._mods_t0) * 1000),
            is_aborted=ctx.stop_evt.is_set(),
            reason="error",
        )
    except Exception:
        eng._log.exception(
            "mods_turn_complete_failed session=%s", ctx.session_id
        )
    return False


