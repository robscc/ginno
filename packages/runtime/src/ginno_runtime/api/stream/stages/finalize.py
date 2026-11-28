"""finalize 阶段 — 段末收尾、parked 退出、final 消息落盘、异常路径（批次 5-2）。

复制自 legacy ``engine._stream_graph`` 的对应段（原行号见各函数 docstring）。

``handle_turn_error`` 返回 ``True`` 表示 steer-continue / auto-retry 分支
已递归接管本轮（legacy 在 except 块内 ``return``——编排器据此 return，
其后 finally 照常执行，与 legacy 的 Python 语义一致）。

finally 清理的栈化（批次 5-3）：legacy 的 finally 块拆成 6 个
``AsyncExitStack`` 条目（E1-E6，见 run.py 入口）。压栈序 = 资源获取序，
弹出序（LIFO）= legacy finally 的逐行清理序——登记与获取钉在同一行
邻域，新增资源不再依赖「记得去 finally 补配对」的纪律（三次泄漏事故
的结构病根治点）。本模块只保留各条目的**释放函数**：

============  ==========================  ===========================
条目          释放内容                    legacy engine.py 行号
============  ==========================  ===========================
E6 _close_coalescer            coal.close    1746-1749
E5 _cancel_keepalive           _ka.cancel    1658-1661
E4 _cancel_stop_waiter         waiter.cancel 1662-1663
E3 _release_stop_entry         TURN_STOP.pop 1666-1668
E2 _release_turn_registration  条件收尾组    1669-1760
E1 ask_budget token reset（编排器内联登记，run.py）            121-126
============  ==========================  ===========================
"""

from __future__ import annotations

from .. import engine as eng
from .ctx import StreamTurnCtx


async def post_stream_flush(ctx: StreamTurnCtx) -> None:
    """legacy engine.py:1153-1181 — attach 尾片 flush、TODO 关联、changed 帧。"""
    # Release whatever the attach-tag filter held back: a completed tag
    # becomes its chip, and an unterminated "<attach_ref ..." fragment is
    # plain prose that must still reach the bubble.
    _tail, _tail_refs = ctx._attach_filter.flush()
    from .streamio import _emit_attach_tags

    await _emit_attach_tags(ctx, _tail_refs)
    if _tail:
        ctx.turn_text.append(_tail)
        ctx.seg_text.append(_tail)
        await ctx.safe_send(ctx.emit("token.delta", {"content": _tail}))
    # Auto-associate this session with every TODO the turn created or
    # touched — the TODO panel surfaces these sessions as jump targets.
    # Gated on an actual todo_* tool call so unrelated edits don't count.
    if ctx.session_id and any(
        (nm or "").startswith("todo_") for nm, _args in ctx.tool_args_by_id.values()
    ):
        # 变量名用 todo（不用 t）：同函数上游的步数预警分支用 t() 作
        # i18n 翻译函数，遮蔽会在触发预警时 UnboundLocalError（issue #5）。
        for todo in eng.todo_store.touched_since(ctx._pre_turn_todos):
            if todo.get("id") and ctx.session_id not in (todo.get("session_ids") or []):
                eng.todo_store.link_session(todo["id"], ctx.session_id)
    # refresh the right-panel TODO list after every turn (the agent may
    # have mutated it via the todo_* tools); the checkbox path is optimistic
    # and doesn't need this.
    await ctx.safe_send(ctx.emit("todos.changed", {}))
    await ctx.safe_send(ctx.emit("workflows.changed", {}))
    await ctx.safe_send(ctx.emit("artifacts.changed", {}))
    # skills too: a turn may have installed/uninstalled skills (the
    # install_skills tool — or even bash), and the slash menu / next
    # turn's skills index must reflect it without a manual refresh.
    await ctx.safe_send(ctx.emit("skills.changed", {}))


async def finalize_completed(ctx: StreamTurnCtx) -> None:
    """legacy engine.py:1182-1433 — 正常完成分支(not saw_interrupt)。"""
    # Prose stranded behind muted narration (see _ZAI_MUTED) is only
    # for the memory pool — the live bubble already showed everything
    # before the first dump.
    for _frags in ctx._ZAI_MUTED.values():
        _prose = eng.server_tools.split_narration("".join(_frags))[0]
        if _prose.strip():
            ctx.turn_text.append(_prose)
    _final_text = "".join(ctx.turn_text)
    # Citation framework: parse/validate the trailing block against the
    # turn's registered sources, record the usage ledger. Runs before
    # memory capture so the block can be stripped from the pool text.
    _verified = 0
    try:
        _verified = await eng._process_turn_citations(
            ctx.session_id, ctx.turn_id, _final_text
        )
    except Exception:
        eng._log.exception(
            "citations_failed session=%s turn=%s", ctx.session_id, ctx.turn_id
        )
    # Capture sanitized assistant text for memory summarization (P2).
    # Also reused as the desktop notification body below (the web shell
    # shows a turn-done notification when the user looked away).
    # Gate 1: `capture` config (revived) + verified-citation quality
    # signal travels with the entry for evidence-weighted distilling.
    from ....knowledge.citations import strip_citation_block

    _clean_text = strip_citation_block(_final_text)
    if ctx.turn_text:
        from ....knowledge.config import load_knowledge_config
        from ....memory import append_to_pool

        _mem_cfg = load_knowledge_config()
        if _mem_cfg.capture:
            append_to_pool(
                ctx.session_id, ctx.agent_id, _clean_text, cited=_verified > 0
            )
            eng._maybe_auto_distill(_mem_cfg)
    # instincts P0：turn 收尾触发模板规则分析（确定性、零 LLM、
    # 异常全吞；docs/instincts-design.md §6——设计口径是 detached
    # 不阻塞 turn：分析要读全量 observations + 扫条目目录，CI 慢盘
    # 上同步执行会拖长收尾、放大 e2e 落盘竞态（PR #74 CI 红根因），
    # 故 daemon 线程化）。
    def _instincts_kick() -> None:
        try:
            from ....instincts import maybe_analyze as _instinct_analyze
            from ....instincts.observer import (
                capture_correction_signal as _ics,
                maybe_run_observer as _mro,
            )

            # L2 预筛：用户输入含纠偏信号 → 观察 user_correction。
            _utext = str(
                ((ctx.config or {}).get("configurable") or {}).get("user_text") or ""
            )
            _corr = _ics(_utext, ctx.session_id)
            # P1 归因：本 turn 注入条目 applied/uncorrected/corrected
            # 闭环（select 同口径重放，零额外状态）。
            try:
                from ....instincts.capture import note_injection_feedback

                note_injection_feedback(
                    _utext,
                    project_slug=str(
                        ((ctx.config or {}).get("configurable") or {}).get(
                            "project_slug"
                        )
                        or ""
                    ),
                    had_correction=bool(_corr),
                )
            except Exception:
                pass
            _instinct_analyze(session_id=ctx.session_id)
            # observer 位点分析（含节流；LLM 判定仅在配置了
            # observer_model 且有 user_correction 观察时发生）。
            _mro(ctx.session_id)
        except Exception:
            pass

    import threading as _threading

    _threading.Thread(target=_instincts_kick, daemon=True).start()
    # LLM subject title (title_gen): armed by _touch_session_title on
    # the first invoke, fired by the first turn that completes without
    # an interrupt. Background + best-effort — failure keeps the
    # truncated placeholder and retries on the next completed turn.
    if _clean_text.strip():
        from ....title_gen import spawn_title_gen

        _found = eng._find_meta(ctx.session_id)
        if _found and _found[0].get("title_llm_pending"):
            eng._session_meta_patch(
                ctx.slug, ctx.session_id, {"title_assistant": _clean_text[:2000]}
            )
            spawn_title_gen(ctx.session_id, ctx.turn_id)
    eng._log.info(
        "turn_done session=%s turn=%s status=completed text_len=%d",
        ctx.session_id, ctx.turn_id, len("".join(ctx.turn_text)),
    )
    # Session unread inbox (session_unread.py): completed turns make
    # the session unread unless the user is watching — the frontend
    # clears it via POST /api/sessions/{id}/seen. Only this explicit
    # terminal branch (not the finally leg: retry/interrupt parking
    # traverse it too) and not paused_at_interrupt / TurnStopped.
    eng.note_turn_finished(ctx.slug, ctx.session_id)
    # Goal lifecycle finish (2026-11-16): a COMPLETE goal archives
    # itself at the turn's explicit terminal branch — the achievement
    # chip stays visible through the completing turn, then the record
    # moves to goals-history.jsonl and the chip clears. Retry/interrupt
    # parking never reach this branch, so an unfinished goal is never
    # archived by a transient turn failure.
    try:
        from ...sessions import _GOAL_DRIVERS, _archive_complete_goal
        from ....goals import store as _goal_store

        _g = _goal_store.get_goal(ctx.slug, ctx.session_id)
        if (
            _g
            and _g.get("status") == _goal_store.STATUS_COMPLETE
            and ctx.session_id not in _GOAL_DRIVERS
        ):
            # User-turn path: no driver owns this session → archive
            # here. Goal-turn path: turn_done fires BEFORE the driver's
            # account_turn, so the driver archives after accounting
            # (the terminating turn's usage must land in history).
            await _archive_complete_goal(ctx.slug, ctx.session_id)
    except Exception:  # noqa: BLE001 — 钩子绝不带崩 turn
        eng._log.exception("goal_archive_hook_failed session=%s", ctx.session_id)
    # AgentTeam 成员 turn 终态钩子（backlog P0-7/P1-2/P1-3）：回帖 +
    # 状态回流 + 放行排队的**唯一触发源**。非团队成员会话内部 no-op。
    try:
        from ....teams.lifecycle import spawn_settled as _team_settled

        _team_settled(ctx.session_id, ctx.turn_id)
    except Exception:  # noqa: BLE001 — 钩子绝不带崩 turn
        eng._log.exception(
            "team_settle_hook_spawn_failed session=%s", ctx.session_id
        )
    # Empty text (tool-only turn) → the UI falls back to a generic body.
    # Mods turn.complete (claude-code-mods-design.md §5.3/§15.7):
    # detached dispatch BEFORE message.end; observe-only in P0.
    try:
        from ....mods.events import spawn_turn_complete

        spawn_turn_complete(
            ctx.session_id,
            ctx.ui_turn_id,
            answer=_clean_text,
            duration_ms=int((eng.time.monotonic() - ctx._mods_t0) * 1000),
            is_aborted=False,
            reason="answer",
        )
    except Exception:
        eng._log.exception(
            "mods_turn_complete_failed session=%s", ctx.session_id
        )
    # 渠道会话的无 tap 收口补推（2026-10-30 keep_edc84fe53d 事故）：
    # 调度器开的头 turn（子代理回传 / worker digest·result / 任务
    # 完成注入）没有 ChannelTap 挂流，回复只落会话，IM 侧订阅的消息
    # 永远收不到。channel 类型会话且流上无 tap 时经适配器主动推送
    # （有 tap 的正常渠道轮由 tap 翻译，函数内部自行去重）。
    # spawn_bg：企微/钉钉 HTTP 不能挡 message.end 帧。
    if _clean_text.strip():
        from ....channels.base import relay_channel_reply

        eng.spawn_bg(relay_channel_reply(ctx.session_id, _clean_text))
    # turn-meta（turn 右下角耗时+token 显示）：
    # ① message.end payload 带权威值（前端 live 值定格）；
    # ② 落 session 的 turns.jsonl（history 投影按最后 AI 消息 id 对齐，
    #    reload 后仍在）。best-effort——meta 永不阻断 turn 收尾。
    try:
        _tm = {
            "turn_id": ctx.turn_id,
            "message_id": ctx._last_ai_id,
            "elapsed_s": round(eng.time.monotonic() - ctx._mods_t0, 1),
            "input_tokens": ctx._turn_usage["input_tokens"],
            "output_tokens": ctx._turn_usage["output_tokens"],
            "ts": int(eng.time.time()),
        }
        _tf = eng.paths.session_files_dir(ctx.slug, ctx.session_id) / "turns.jsonl"
        _tf.parent.mkdir(parents=True, exist_ok=True)
        with _tf.open("a", encoding="utf-8") as _f:
            _f.write(eng.json.dumps(_tm, ensure_ascii=False) + "\n")
    except Exception:
        eng._log.debug(
            "turn_meta_write_failed session=%s", ctx.session_id, exc_info=True
        )
    await ctx._coal.flush()
    request = ctx.reasoning_summary.current
    if request is not None and request.receiving:
        request.take_delta(receiving=False)
    summaries = ctx.reasoning_summary.snapshots()
    await ctx.safe_send(
        ctx.emit(
            "message.end",
            {
                "text": _clean_text.strip()[:200],
                "meta": _tm,
                **({"reasoning_summaries": summaries} if summaries else {}),
                # turn 发起者标记（interactive/schedule/subagent）：
                # 前端据此跳过非交互 turn 的 OS 通知（完成通知由
                # schedule-run/workflow-run 通道统一发，防双弹）。
                "origin": (ctx.config.get("configurable") or {}).get(
                    "ginno_turn_origin", "interactive"
                ),
            },
        )
    )
    # ── linked 完成推送（2026-11-06 用户实录：c79b49cd 手动挂载
    # 16aef0a3，后者 turn 完成后前者毫无感知——「订阅」的核心价值
    # 缺失）。向所有 incoming 订阅方（watch/assign/手动挂载）广播
    # linked.done；schedule/subagent origin 已有专属通知通道，跳过
    # 防双弹。订阅方前端弹 OS 通知（📡 前缀）+ 点击直达本会话。
    _origin = (ctx.config.get("configurable") or {}).get(
        "ginno_turn_origin", "interactive"
    )
    if _origin in ("interactive", "deliver"):
        try:
            from ....session_links import linked_report

            _inc = (linked_report(ctx.session_id) or {}).get("incoming") or []
            if _inc:
                _t = _clean_text.strip()[:160]
                # worker 语义修正（2026-11-10）：turn 完成 ≠ 任务完成
                # ——worker 会话处理完一段进入 waiting/继续 working 时，
                # 订阅方不该收到「已完成」。投影 live_status 给前端
                # 选文案；投影失败不带键（前端按普通完成显示）。
                _wst: str | None = None
                try:
                    from ....worker_bus.registry import project_status

                    _fm = eng._find_meta(ctx.session_id)
                    if _fm:
                        _wst = project_status(ctx.session_id, _fm[0])
                except Exception:
                    _wst = None
                for _w in _inc:
                    if isinstance(_w, dict) and _w.get("id"):
                        await eng._push_session_event(
                            str(_w["id"]),
                            "linked.done",
                            {
                                "session_id": ctx.session_id,
                                "text": _t,
                                "origin": _origin,
                                "worker_status": _wst,
                            },
                            ctx.turn_id,
                        )
        except Exception:
            pass  # 推送失败不影响本会话收口



# --------------------------------------------------------------------------- #
# 栈条目释放函数（E2-E6）—— 登记点见 run.py/_setup_identity/_setup_io。
# 顺序契约：弹出序（LIFO）= legacy finally 逐行清理序；AsyncExitStack 保证
# 任一条目抛异常不阻断其余条目——比 legacy finally 的段间依赖更健壮。
# --------------------------------------------------------------------------- #


async def _close_coalescer(ctx: StreamTurnCtx) -> None:
    """【栈条目 E6】legacy engine.py:1746-1749 — 合帧器收尾（finally 首位）。

    flush 残留 + 取消定时器（幂等；异常路径也清）。最后压栈 → 最先弹出，
    与 legacy finally 的第一句对位。
    """
    try:
        await ctx._coal.close()
    except Exception:  # noqa: BLE001
        pass


def _cancel_keepalive(ctx: StreamTurnCtx) -> None:
    """【栈条目 E5】legacy engine.py:1658-1661 — keepalive 任务取消。"""
    try:
        ctx._ka.cancel()
    except Exception:  # noqa: BLE001
        pass


def _cancel_stop_waiter(ctx: StreamTurnCtx) -> None:
    """【栈条目 E4】legacy engine.py:1662-1663 — stop 等待任务取消。"""
    if ctx.stop_waiter is not None and not ctx.stop_waiter.done():
        ctx.stop_waiter.cancel()


def _release_stop_entry(ctx: StreamTurnCtx) -> None:
    """【栈条目 E3】legacy engine.py:1666-1668 — _TURN_STOP 弹出。

    A parked-at-interrupt turn no longer needs the stop event either —
    the WS `stop` handler heals parked turns directly (no live stream).
    【注册表写入点②】_TURN_STOP pop(legacy finally 第一处注册表操作)。
    """
    eng._TURN_STOP.pop(ctx.session_id, None)


async def _release_turn_registration(ctx: StreamTurnCtx) -> None:
    """【栈条目 E2】legacy engine.py:1669-1760 — 条件收尾组（整段保序平移）。

    Ended or errored (not paused at an interrupt): unregister so a client
    `turn_state` query reports "not running". A turn parked at a
    permission/version-propose/ask_user interrupt stays registered — its
    resume hasn't happened yet.
    【注册表写入点④】_RUNNING_TURNS/_RUNNING_TURNS_AT pop + _PENDING_* 清。

    栈化注意：major-B 的 `_user_stopped` 在 legacy 是 TURN_STOP.pop **之前**
    捕获；栈弹出序 E3 → E2，本函数执行时 dict 条目已移除——但 ctx.stop_evt
    仍持同一 Event 对象，is_set() 读值不变（pop 只动 dict 不动事件），语义
    等价。
    """
    from .... import server_shared as shared

    # major-B: a set event here means the USER stopped this turn (the
    # TurnStopped unwind, or a stop landing during retry backoff). The
    # reconcile below must not spawn result-redelivery wakes ahead of the
    # stopped finalize on such a turn.
    _user_stopped = ctx.stop_evt.is_set()
    if not ctx.saw_interrupt:
        eng._RUNNING_TURNS.pop(ctx.session_id, None)
        eng._RUNNING_TURNS_AT.pop(ctx.session_id, None)
        # AgentTeam：error / 用户 stop 收尾也走这里（显式 completed 分支
        # 之外的那条腿）。同样触发终态钩子——否则成员出错时主会话永远
        # 等不到回帖。与 completed 分支的重复触发由 turn_id 去重兜住。
        try:
            from ....teams.lifecycle import spawn_settled as _team_settled

            _team_settled(ctx.session_id, ctx.turn_id)
        except Exception:  # noqa: BLE001
            eng._log.exception(
                "team_settle_hook_spawn_failed session=%s", ctx.session_id
            )
        # 遗言作废（lastwords-design P2）：turn 正常/出错收尾即删——绝不
        # 把已完成的 turn 留给下次重启误恢复（parked interrupt 不在此列，
        # 那是 turn 还活着等 resume，遗言保留是对的）。
        try:
            from ....lastwords import note_turn_end

            note_turn_end(ctx.session_id, ctx.turn_id)
        except Exception:  # noqa: BLE001
            pass
        # 图片 turn 基线退役（image-emit-universal）：终态清除——下一 turn
        # 重新快照。parked turn 走 else 分支保留，resume 段的 agent lift
        # 还要 diff 停车前工具产出的图。
        shared._TURN_IMAGE_BASELINE.pop(ctx.session_id, None)
        eng._PENDING_RESUME.discard(ctx.session_id)
        eng._PENDING_KIND.pop(ctx.session_id, None)
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
                from ....subagent_scheduler import on_turn_stopped

                await on_turn_stopped(ctx.session_id)
            except Exception:
                eng._log.exception(
                    "subagent_stop_finalize_failed session=%s", ctx.session_id
                )
        try:
            from ....subagent_scheduler import reconcile_stash_injections

            reconcile_stash_injections(ctx.session_id)
        except Exception:
            eng._log.exception(
                "subagent_reconcile_failed session=%s", ctx.session_id
            )
        # 渠道来源的未吸收 steer 不清场（steer_keep_channel）：turn 已
        # 收尾、注入无下家，run_channel_turn 尾部排水后由 adapter 补投
        # 为后续 turn——「已追加」的回执不能是空头支票。
        shared.steer_keep_channel(ctx.session_id)
        # The uncommitted-drain record is either retired by the commit or
        # committed by the heal (both ran above) — never left behind.
        shared.steer_clear_inflight(ctx.session_id)
    else:
        # Parked: the interrupt fires after the agent commit, so a pending
        # drain should not exist — but if one ever did, the resumed segment
        # re-runs the node that drains, so hand it back rather than dropping
        # the user's message on the floor.
        shared.steer_restash_inflight(ctx.session_id)
        # AgentTeam：turn 停在 interrupt → 让 blocked 实时冒泡到成员面板 /
        # 团队行 / Dock（backlog P1-2）。**不回帖**——此刻产出是半截话，
        # 回它等于骗主会话（P0-7 前半）。一处覆盖三种 interrupt。
        try:
            from ....teams.lifecycle import spawn_parked as _team_parked

            _team_parked(ctx.session_id)
        except Exception:  # noqa: BLE001
            eng._log.exception(
                "team_park_hook_spawn_failed session=%s", ctx.session_id
            )
    if ctx.ws_closed:
        eng._log.info(
            "turn_client_gone session=%s turn=%s (no live client socket at the "
            "last send; turn completed server-side)",
            ctx.session_id,
            ctx.turn_id,
        )
