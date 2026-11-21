"""Session domain — /compact /clear /fork /subagent /subagent-split.

History rewriting (compact/clear), session copying (fork, whose core is
shared with POST /api/sessions/{id}/fork) and subagent spawning. Async
handlers are busy-gated: they touch checkpoint state that must never race
a live turn.
"""

from __future__ import annotations

from langchain_core.messages import RemoveMessage

from ..lang import t
from ._base import BuiltinCommand, _log


def _compact_handler(project_slug: str | None = None, session=None, args=None) -> str:
    # Never reached through the resolver (async_handler routes it); kept so
    # the registry entry is well-formed and direct sync calls get a hint.
    return t(
        "Run /compact through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /compact",
    )

async def _compact_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/compact — force the E3 history compaction NOW (manual trigger).

    Same machinery as the automatic turn-entry compaction (microcompact the
    stale tool outputs first, then LLM-summarize the old prefix), but with
    ``force=True``: no token threshold, works even when auto compaction is
    off. Long sessions run at 100k+ tokens are exactly where provider streams
    get fragile (2026-09-29 incident), so users need an on-demand lever.
    """
    if not session:
        return t("(/compact must be used inside a session)", "（/compact 必须在会话内使用）")
    # 远程会话 fail-loud（2026-11-18 远程安全网 C1 小修④）：远端会话的真实
    # 历史在对端节点的 checkpoint 里，本地只是 _persist_exchange 写的问答
    # 镜像——在这里压缩只会破坏镜像、且永远碰不到对端上下文。
    if str(session.get("remote_peer") or "").strip():
        return t(
            "Compaction runs on the peer node for remote sessions; the local "
            "side only mirrors the history — open a session on that node to "
            "run /compact",
            "远程会话的压缩发生在对端节点，本地仅镜像历史——请到对端节点的会话里执行 /compact",
        )
    sid = session.get("session_id") or ""
    graph = session.get("graph")
    if graph is None or not sid:
        return t("Session is not ready yet; try again shortly", "会话尚未就绪；请稍后再试")
    slug = session.get("project_slug") or project_slug or "default"
    config = {"configurable": {"thread_id": sid, "project_slug": slug}}

    from ..compaction import maybe_compact_history
    from ..microcompact import maybe_microcompact_history
    from ..tokens import estimate_messages_tokens

    try:
        snap = await graph.aget_state(config)
    except Exception:
        snap = None
    msgs = list((getattr(snap, "values", None) or {}).get("messages") or [])
    if not msgs:
        return t(
            "This session has no history messages to compact yet",
            "本会话还没有可压缩的历史消息",
        )
    if getattr(snap, "next", None):
        # `next` 非空有两种含义：本进程里有活的 turn（流式进行中，或停在
        # 权限/提问 interrupt——两者都在内存注册表里），此时拒绝压缩；或是
        # 进程重启/崩溃把 checkpoint 留在半空中（内存注册表已随旧进程消失，
        # 没有任何东西会再跑），此时应当修复（heal 清掉 next、补齐悬空
        # tool_calls）后照常压缩，而不是永远拒绝（2026-10-12：重启杀掉进行
        # 中 turn 后 /compact 一直报「停在确认步骤」）。
        # turn_gate_busy（2026-10-31）：三查同义 + 泄漏自愈（armed 超
        # RUNNING_TURN_STALE_S 且无活 task 的残留条目不再永久卡死命令）。
        from .. import server_shared as _shared

        if _shared.turn_gate_busy(sid):
            return t(
                "The current turn is running or paused at a confirmation step (permission / question); finish or stop it before running /compact",
                "当前回合仍在进行或停在确认步骤（权限 / 提问）上；请先完成或停止再运行 /compact",
            )
        try:
            from ..api.stream import _heal_interrupted_turn
            from ..server_shared import _turn_lock

            # 与 _stop_parked_turn 相同的串行化：防止 heal 期间另一个 tab 的
            # 新 invoke 与它交错写 checkpoint。heal 自身 best-effort。
            async with _turn_lock(sid):
                await _heal_interrupted_turn(graph, config)
        except Exception:
            # 绝不能在这里 `from ..server_shared import _log`：函数体内任何对
            # `_log` 的赋值/import 都会让它变成整个函数的局部名，而该行只在
            # heal 失败时执行——heal 成功后下面既有的 `_log.exception(...)`
            # 就会 UnboundLocalError（2026-10-12 实测炸过）。用模块级 `_log`。
            _log.exception("compact_cmd_zombie_heal_failed session=%s", sid)
    before_tokens = estimate_messages_tokens(msgs)

    notes: list[str] = []
    # Rung below E3: clear stale tool outputs first (pure state rewrite, no
    # LLM). Frees the most bytes per fidelity lost, same order as turn entry.
    try:
        micro = await maybe_microcompact_history(session, config)
        if micro:
            notes.append(
                t(
                    f"cleared {micro.get('cleared_tool_outputs', 0)} stale tool output(s)"
                    f" (~{micro.get('chars_freed', 0)} chars freed)",
                    f"清理了 {micro.get('cleared_tool_outputs', 0)} 条过期工具输出"
                    f"（约释放 {micro.get('chars_freed', 0)} 字符）",
                )
            )
    except Exception:
        _log.exception("compact_cmd_microcompact_failed session=%s", sid)

    try:
        # 「压缩中…」状态事件（与 turn 入口的自动压缩同一条 UI 契约）：
        # 只在真正决定压缩（过完所有早退检查）后、总结 LLM 调用前发出。
        from ..server_shared import _push_global_event, _push_session_event

        async def _announce_compacting(predicted_s=None) -> None:
            # 进度条（2026-11-12）：stage + pct 事件；2026-11-14 增 predicted_s
            # （历史压缩时长预估），前端据此走时间轴进度 + ETA，无预估时回退缓动。
            await _push_session_event(
                sid, "context.compacting",
                {"stage": "summarizing", "pct": 8, "predicted_s": predicted_s},
            )

        async def _announce_summary_done() -> None:
            await _push_session_event(
                sid, "context.compacting", {"stage": "writing", "pct": 92}
            )

        async def _announce_heartbeat(pct: int) -> None:
            # P3（2026-11-16）：与 turn 入口自动压缩同一条进度契约。
            await _push_session_event(
                sid, "context.compacting", {"stage": "summarizing", "pct": pct}
            )

        async def _announce_done() -> None:
            # 终态必达（2026-11-12 卡死修复）：compact 是后台任务，结束后
            # 若无后续事件，前端 compacting 镜像永不清 → Composer 永久
            # readOnly（@ 打不进输入框）。done 事件让前端即时解锁。
            await _push_session_event(
                sid, "context.compacting", {"stage": "done", "pct": 100}
            )

        try:
            stats = await maybe_compact_history(
                session, config, force=True,
                on_summary_start=_announce_compacting,
                on_summary_done=_announce_summary_done,
                on_heartbeat=_announce_heartbeat,
            )
        except Exception as e:
            _log.exception("compact_cmd_failed session=%s", sid)
            # 不在此处发 done：finally 必达，except 内再发就是双发（2026-11
            # 审计 P3——前端按「恰好一条 done」解锁，重复事件徒增噪音）。
            return t(
                f"Compaction failed: {type(e).__name__}: {e}",
                f"压缩失败：{type(e).__name__}: {e}",
            )
        finally:
            await _announce_done()
    except Exception as e:
        # 外层（heal/前置段）兜底——原 try 的收尾；stats 自身的失败已在
        # 内层捕获并连带发出 compacting 终态。
        _log.exception("compact_cmd_failed session=%s", sid)
        return t(
            f"Compaction failed: {type(e).__name__}: {e}",
            f"压缩失败：{type(e).__name__}: {e}",
        )

    if not stats:
        # No user-turn boundary to split at (history shorter than
        # compact_keep_turns) or the summarizer returned nothing.
        tail = "; ".join(notes)
        msg = t(
            "History not long enough: full turns haven't exceeded the retention threshold (compact_keep_turns); no old messages to summarize yet",
            "历史还不够长：完整回合数未超过保留阈值（compact_keep_turns）；暂无可摘要的旧消息",
        )
        return f"{msg} ({tail})" if tail else msg

    try:
        snap2 = await graph.aget_state(config)
        after_tokens = estimate_messages_tokens(
            list((getattr(snap2, "values", None) or {}).get("messages") or [])
        )
    except Exception:
        after_tokens = None

    lines = [
        t("✅ Context compacted", "✅ 上下文已压缩"),
        t(
            f"- LLM-summarized {stats['compacted_messages']} old message(s)"
            f" (summary of {stats['summary_chars']} chars), kept the most recent {stats['kept_messages']}",
            f"- LLM 摘要了 {stats['compacted_messages']} 条旧消息"
            f"（摘要 {stats['summary_chars']} 字符），保留最近 {stats['kept_messages']} 条",
        ),
    ]
    if after_tokens is not None:
        lines.append(
            t(
                f"- History tokens ~{before_tokens} → ~{after_tokens}",
                f"- 历史 tokens 约 {before_tokens} → 约 {after_tokens}",
            )
        )
    lines.extend(f"- {n}" for n in notes)
    return "\n".join(lines)

def _clear_handler(project_slug: str | None = None, session=None, args=None) -> str:
    # Never reached through the resolver (async_handler routes it); kept so
    # the registry entry is well-formed and direct sync calls get a hint.
    return t(
        "Run /clear through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /clear",
    )

async def _clear_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/clear — wipe this session's conversation history, start fresh.

    Same session_id keeps its identity: files dir, mounts, goal, settings all
    survive; only the checkpoint's ``messages`` channel is emptied (the delete-
    rewrite pattern of microcompact.py, minus the re-add). Busy-gated like
    /compact — a history rewrite must never race a live turn. After the
    rewrite, ``history.cleared`` is broadcast so every open UI drops its local
    transcript (the /history endpoint now rebuilds from an empty checkpoint);
    the receipt notice that follows lands as the first bubble of the fresh chat.
    """
    if not session:
        return t("(/clear must be used inside a session)", "（/clear 必须在会话内使用）")
    sid = session.get("session_id") or ""
    graph = session.get("graph")
    if graph is None or not sid:
        return t("Session is not ready yet; try again shortly", "会话尚未就绪；请稍后再试")
    slug = session.get("project_slug") or project_slug or "default"
    config = {"configurable": {"thread_id": sid, "project_slug": slug}}

    from ..tokens import estimate_messages_tokens

    try:
        snap = await graph.aget_state(config)
    except Exception:
        snap = None
    msgs = list((getattr(snap, "values", None) or {}).get("messages") or [])
    if not msgs:
        return t("This session's context is already empty", "本会话上下文已经是空的")
    if getattr(snap, "next", None):
        # `next` 非空与 /compact 同理有两种含义（详见 /compact 处注释）：本进程
        # 里有活的 turn（停在权限/提问 interrupt，UI 有卡可确认）→ 拒绝；进程
        # 重启/崩溃留下的僵尸 checkpoint（内存注册表已随旧进程消失，UI 没有
        # 任何可确认的地方，也没有东西会再跑）→ heal 后照常清空，而不是永远
        # 拒绝（2026-10-15 用户报告：/clear 报「停在确认步骤」但无卡可确认）。
        # turn_gate_busy（2026-10-31）：三查同义 + 泄漏自愈（armed 超
        # RUNNING_TURN_STALE_S 且无活 task 的残留条目不再永久卡死命令）。
        from .. import server_shared as _shared

        if _shared.turn_gate_busy(sid):
            return t(
                "The current turn is paused at a confirmation step (permission / question); finish it before running /clear",
                "当前回合停在确认步骤（权限 / 提问）上；请先完成再运行 /clear",
            )
        try:
            from ..api.stream import _heal_interrupted_turn
            from ..server_shared import _turn_lock

            # 与 _stop_parked_turn 相同的串行化（防止 heal 与新 invoke 交错写
            # checkpoint）；heal 自身 best-effort，失败也继续清空。
            async with _turn_lock(sid):
                await _heal_interrupted_turn(graph, config)
        except Exception:
            _log.exception("clear_cmd_zombie_heal_failed session=%s", sid)
    if any(not getattr(m, "id", None) for m in msgs):
        # id-less messages can't be delete-rewritten (microcompact bails the
        # same way); nothing re-added means they'd survive as ghosts.
        return t(
            "Some history messages carry no id and cannot be removed safely; run /compact instead",
            "部分历史消息缺少 id，无法安全清除；请改用 /compact",
        )

    freed = estimate_messages_tokens(msgs)
    await graph.aupdate_state(
        config,
        {"messages": [RemoveMessage(id=m.id) for m in msgs]},
        as_node="agent",
    )
    from ..server_shared import _push_global_event, _push_session_event

    await _push_session_event(sid, "history.cleared", {})
    return (
        t(
            f"🧹 Context cleared — removed {len(msgs)} message(s) (~{freed} tokens freed).",
            f"🧹 上下文已清空 —— 移除了 {len(msgs)} 条消息（约释放 {freed} tokens）。",
        )
        + "\n"
        + t(
            "The conversation starts fresh; session files, mounts and settings are kept.",
            "对话从新开始；会话文件、挂载与设置均保留。",
        )
    )

def _fork_handler(project_slug: str | None = None, session=None, args=None) -> str:
    return t(
        "Run /fork through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /fork",
    )

async def fork_session_core(sid: str, slug: str, meta: dict, session: dict) -> dict:
    """/fork 与 POST /api/sessions/{id}/fork 共用的复制核心。

    返回 ``{"ok": bool, "error"?: str, "new_id"?: str, "copied"?: int,
    "existing"?: bool}``；调用方负责摊成自己的回复形态（IM 双语文本 /
    REST JSON）。busy-gate（带泄漏自愈，server_shared.turn_gate_busy）与
    一会话一副本幂等都在这里。
    """
    from .. import server_shared as shared

    if shared.turn_gate_busy(sid):
        return {"ok": False, "error": "busy"}

    # 一会话一副本：已有存活副本 → 幂等返回（防双胞胎）；副本被删 →
    # 稍后用新值覆盖死引用重新 fork（patch 丢 None，无需显式清键）。
    existing = meta.get("forked_to_session")
    if existing:
        from ..session_meta import _find_meta

        if _find_meta(str(existing)):
            return {"ok": True, "new_id": str(existing), "existing": True}

    from ..api.sessions import CreateSessionRequest, create_session

    channel = str(meta.get("channel") or "")
    is_channel = meta.get("type") == "channel"
    src_title = str(meta.get("title") or "").strip() or (
        {"dingtalk": "钉钉对话", "wecom": "企业微信对话"}.get(channel, "IM 对话")
        if is_channel else "会话"
    )
    # 副本标题带溯源后缀（2026-10-31 用户要求）：fork 出来的会话一眼可辨
    # 来源；侧栏超长自动截断，不担心占宽。
    fork_title = f"{src_title} (fork from {sid[:8]})"
    new_meta = await create_session(
        CreateSessionRequest(
            project_slug=slug,
            workspace="",
            agent_id=meta.get("agent_id") or session.get("agent_id") or None,
            title=fork_title,
            # 思考档位随副本(thinking-effort-design §8):源会话显式选过档位
            # 就带过去;auto 走 agent 默认链(provider/model 的 fork 语义不变,
            # 仍由 agent 绑定解析)。
            thinking_effort=str(meta.get("thinking_effort") or "") or None,
        )
    )
    if new_meta.get("ok") is False or not new_meta.get("id"):
        return {"ok": False, "error": "create_failed"}
    new_id = str(new_meta["id"])

    import time as _time

    stamp = _time.strftime("%m-%d %H:%M", _time.localtime())
    label = {"dingtalk": "钉钉", "wecom": "企业微信"}.get(channel, "IM")
    label_en = {"dingtalk": "DingTalk", "wecom": "WeCom"}.get(channel, "IM")
    brief = t(
        f"[turn context] Continued from a {label_en} conversation ({stamp}). "
        "Its current transcript follows — keep discussing the results here.",
        f"[turn context] 续自{label}对话（{stamp}）。以下为其当前记录，可就结果继续沟通。",
    ) if is_channel else t(
        f"[turn context] Continued from session {sid[:8]} ({stamp}). "
        "Its current transcript follows — keep discussing the results here.",
        f"[turn context] 续自会话 {sid[:8]}（{stamp}）。以下为其当前记录，可就结果继续沟通。",
    )
    try:
        from ..subagent_scheduler import _seed_fork_history

        copied = await _seed_fork_history(sid, new_id, slug, brief)
    except Exception:  # noqa: BLE001 — 种子失败要显式报错（副本没历史不能用）
        from ..server_shared import _log

        _log.exception("channel_fork_seed_failed src=%s child=%s", sid, new_id)
        return {"ok": False, "error": "seed_failed"}

    from ..session_meta import _session_meta_patch

    # 双向标记：新会话记来源（渠道名或源会话 id——溯源可辨）；源会话记
    # 副本落点（幂等键）。源会话 touch=False——复制不该把源会话顶到侧栏
    # 顶端。
    _session_meta_patch(
        slug, new_id,
        {"forked_from_channel": channel} if channel
        else {"forked_from_session": sid})
    _session_meta_patch(slug, sid, {"forked_to_session": new_id}, touch=False)

    from ..server_shared import _log

    _log.info(
        "channel_forked src=%s child=%s channel=%s messages=%d",
        sid, new_id, channel or "-", copied,
    )
    return {"ok": True, "new_id": new_id, "copied": copied}

async def _fork_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/fork — 把本会话复制为一个新的桌面普通会话（渠道或桌面均可）。

    范式 = ``api/schedule.py: fork_run``：``create_session`` +
    ``_seed_fork_history`` 复制 checkpoint 全量 messages（id 重铸，顶部
    一条 [turn context] 溯源行）。源会话本体不动。2026-10-31 起桌面会话
    也可 fork（侧栏扇形菜单入口）；渠道语义不变。

    语义要点（im-fork-to-desktop 设计 v0.4）：
    - ``/clear`` 边界天然尊重：clear 是对 checkpoint messages 的真删，
      而 checkpoint 正是唯一复制源——副本天然只带 clear 后的「眼前所见」。
    - 幂等（一会话一副本）：源 meta 记 ``forked_to_session``，副本存活时
      重复 /fork 返回既有副本；副本被删则重新 fork。
    - busy-gate（/clear 同口径，带泄漏自愈）：turn 在跑 / 停在确认步骤时
      拒绝，防止复制到写了一半的 checkpoint。
    - ``/fork <附带消息>`` 的「带话过去」留给 P1——附带内容暂被忽略并提示。
    """
    if not session or not (session.get("session_id") or ""):
        return t("(/fork must be used inside a session)", "（/fork 必须在会话内使用）")
    sid = session["session_id"]

    from ..session_meta import _find_meta

    found = _find_meta(sid)
    meta, slug = found if found else (None, str(project_slug or "default"))
    if not meta:
        return t(
            "/fork must be used inside a session with an index entry.",
            "/fork 需要在有索引记录的会话内使用。",
        )
    is_channel = meta.get("type") == "channel"
    channel = str(meta.get("channel") or "")
    label = {"dingtalk": "钉钉", "wecom": "企业微信"}.get(channel, "IM")

    extra = str(args or "").strip()
    extra_note = (
        t(
            " (The message attached to /fork is not carried over yet — that's a planned follow-up.)",
            "（/fork 后附带的消息暂不支持带走，属于后续版本功能。）",
        )
        if extra
        else ""
    )

    r = await fork_session_core(sid, slug, meta, session)
    if not r.get("ok"):
        err = r.get("error")
        if err == "busy":
            return t(
                "⏳ The AI is still replying (or a confirmation is pending) — "
                "send /fork again after it finishes.",
                "⏳ AI 正在回复（或停在确认步骤），结束后再发 /fork。",
            )
        if err == "seed_failed":
            return t(
                "Fork failed while copying history — please try again later.",
                "复制失败：历史搬运出错，请稍后再试。",
            )
        return t("Fork failed: could not create the desktop session.",
                 "复制失败：桌面会话创建失败。")
    new_id = str(r.get("new_id"))
    src_title = str(meta.get("title") or "").strip() or (
        f"{label}对话" if is_channel else "会话"
    )
    if r.get("existing"):
        return t(
            f'✅ Already copied — desktop session "{src_title}" '
            "(no second copy is made).",
            f"✅ 已复制过：桌面普通对话《{src_title}》，不会产生第二份。",
        )
    copied = int(r.get("copied") or 0)
    if is_channel:
        return t(
            f'✅ Copied to desktop session "{src_title}" ({copied} messages). '
            "Open Ginno on your desktop to continue — this conversation stays as is."
            + extra_note,
            f"✅ 已复制到桌面普通对话《{src_title}》（{copied} 条消息）。历史已带走，"
            "回电脑打开 Ginno 即可继续；本对话保持不变，在这里聊仍然照旧。" + extra_note,
        )
    return t(
        f'✅ Forked to new session "{src_title}" ({copied} messages); '
        "open it from the sidebar." + extra_note,
        f"✅ 已复制为新会话《{src_title}》（{copied} 条消息）；请在侧栏打开继续。"
        + extra_note,
    )

_SUBAGENT_USAGE_EN = (
    "**Subagent usage**\n"
    "- `/subagent <goal>` — create 1 subagent right away (constraints/acceptance "
    "left empty; refine later via steering in the sub-session)\n"
    "- `/subagent split <task description>` — decompose the task with the current "
    "model first, producing a plan card; spawns in bulk after you confirm\n"
    "- `/subagent-split <task description>` — same as the split form (alias)\n"
    "Splitting again before confirming overwrites the previous plan; each subagent's "
    "result is returned to this conversation automatically when it completes."
)

_SUBAGENT_USAGE_ZH = (
    "**/subagent 用法**\n"
    "- `/subagent <目标>` — 直接创建 1 个子代理（约束/验收留空；"
    "之后可在子会话中通过引导消息补充）\n"
    "- `/subagent split <任务描述>` — 先用当前模型拆解任务，生成方案卡片；"
    "确认后批量启动\n"
    "- `/subagent-split <任务描述>` — 同 split 形式（别名）\n"
    "确认前再次拆分会覆盖上一份方案；每个子代理完成后其结果会自动返回本对话。"
)

def _subagent_usage() -> str:
    return t(_SUBAGENT_USAGE_EN, _SUBAGENT_USAGE_ZH)

def _subagent_handler(project_slug: str | None = None, session=None, args=None) -> str:
    # Never reached through the resolver (async_handler routes /subagent); kept
    # so the registry entry is well-formed and direct sync calls get a hint.
    return t(
        "Run /subagent through the async entry point (WS invoke)",
        "请通过异步入口（WS invoke）运行 /subagent",
    )

def _split_form_task(args: str) -> str | None:
    """`/subagent split <task>` — the split keyword is the leading token
    (`split`, or the legacy Chinese `拆分`); the task description is
    everything after it (colon/space separators tolerated). None when the
    args are not the split form."""
    s = (args or "").lstrip()
    for kw in ("split", "拆分"):
        if s.startswith(kw):
            return s[len(kw):].lstrip(" ：:　")
    return None

def _is_split_form(args: str) -> bool:
    return _split_form_task(args) is not None

async def _subagent_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/subagent — direct form spawns ONE subagent immediately (origin=user);
    split form (拆分) runs the LLM decompose → subagent.plan card (P2 contract
    3). Busy-gated like /compact: the decompose LLM call must not race a live
    turn's supersteps, and the direct spawn path creates a session + graph."""
    if not session:
        return _subagent_usage()
    args = (args or "").strip()
    if not args:
        return _subagent_usage()
    if _is_split_form(args):
        task = _split_form_task(args) or ""
        if not task:
            return _subagent_usage()
        from ..subagent_plan import decompose_and_issue

        try:
            return await decompose_and_issue(session, task)
        except ValueError as e:
            return t(f"Split failed: {e}", f"拆分失败：{e}")
    from ..subagent_scheduler import create_subagent  # lazy: cycle

    res = await create_subagent(
        session.get("session_id") or "", args, "", "", origin="user"
    )
    if not res.get("ok"):
        return str(res.get("error") or "[error] spawn failed")
    return (
        t(
            f"✅ subagent started session_id={res['session_id']}"
            f" title={res['title']} depth={res['depth']}\n"
            "It runs independently in the background; its result is returned to this conversation when it completes.",
            f"✅ 子代理已启动 session_id={res['session_id']}"
            f" 标题={res['title']} 深度={res['depth']}\n"
            "它在后台独立运行；完成后其结果会自动返回本对话。",
        )
    )

async def _subagent_split_async_handler(
    project_slug: str | None = None, session=None, args=None
) -> str:
    """/subagent-split <任务> — the split form under an explicit alias; the
    whole tail is the task description (no 拆分 keyword needed)."""
    if not session:
        return _subagent_usage()
    task = (args or "").strip()
    if not task:
        return _subagent_usage()
    from ..subagent_plan import decompose_and_issue

    try:
        return await decompose_and_issue(session, task)
    except ValueError as e:
        return t(f"Split failed: {e}", f"拆分失败：{e}")


# 本域注册块：registry.py 装配 BUILTINS 时收集（命令清单契约快照钉住）。
COMMANDS: dict[str, BuiltinCommand] = {
    "compact": BuiltinCommand(
        name="compact",
        description=(
            "Manually compact session context (LLM-summarizes old messages to free tokens)",
            "手动压缩会话上下文（LLM 摘要旧消息，释放 tokens）",
        ),
        handler=_compact_handler,
        async_handler=_compact_async_handler,
    ),
    "clear": BuiltinCommand(
        name="clear",
        description=(
            "Clear this session's conversation history and start fresh (files, mounts and settings are kept)",
            "清空本会话的对话上下文，重新开始（会话文件、挂载与设置保留）",
        ),
        handler=_clear_handler,
        async_handler=_clear_async_handler,
    ),
    "fork": BuiltinCommand(
        name="fork",
        description=(
            "Copy this IM (DingTalk/WeCom) conversation into a fresh desktop session (history carried over, this chat untouched)",
            "把本 IM（钉钉/企微）对话复制为桌面普通对话（历史带走，本对话保持不变）",
        ),
        handler=_fork_handler,
        async_handler=_fork_async_handler,
    ),
    "subagent": BuiltinCommand(
        name="subagent",
        description=(
            "Create a subagent (/subagent <goal> direct; /subagent split <task> to decompose first)",
            "创建 subagent（/subagent <goal> 直接启动；/subagent 拆分 <任务> 先 LLM 拆分再确认）",
        ),
        handler=_subagent_handler,
        async_handler=_subagent_async_handler,
    ),
    "subagent-split": BuiltinCommand(
        name="subagent-split",
        description=(
            "Split a task into multiple subagents (/subagent-split <task>, alias of /subagent split)",
            "拆分任务为多个 subagent（/subagent-split <任务>，同 /subagent 拆分）",
        ),
        handler=_subagent_handler,
        async_handler=_subagent_split_async_handler,
    ),
}
