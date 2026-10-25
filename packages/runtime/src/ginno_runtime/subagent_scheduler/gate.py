"""Completion gate (contract 6, design §5.6) + finalize + summary + LLM title:
after any turn of a session settles, decide done / failed / stopped / waiting,
write the terminal meta once, compress the report, and inject the result into
the parent."""

from __future__ import annotations

import asyncio
import sys

_NS = sys.modules[__package__]  # package namespace: all former module globals


async def on_turn_settled(
    session_id: str, stop_evt: asyncio.Event | None = None, turn_id: str = ""
) -> None:
    """The gate: called after ANY turn of a session settles (scheduler runner,
    WS invoke job, resume job, checkpoint-retry job). No-op unless the session
    is a non-terminal subagent with nothing live.

    stop_evt is the session's cooperative-stop Event object captured before the
    turn's cleanup popped it — a set event means the USER stopped the turn, and
    a user-killed subagent is "stopped" and injects nothing (design §5.7).
    """
    if not session_id or session_id in _NS._PENDING_RESUME:
        return
    task = _NS._TURN_TASKS.get(session_id)
    if task is not None and not task.done() and task is not _NS.asyncio.current_task():
        return  # a new segment is already running; it settles for itself
    if session_id in _NS._RUNNING_TURNS:
        return
    # Result injections that missed their absorption window (the turn's last
    # agent superstep had already drained when they arrived) would be dropped
    # by the segment's steer_clear — redeliver them before anything else. The
    # redelivery itself may open a wake turn; the gate below then no-ops via
    # the _RUNNING_TURNS guard on the next settle.
    try:
        _NS.reconcile_stash_injections(session_id)
    except Exception:
        _NS._log.exception("subagent_reconcile_failed session=%s", session_id)
    s = _NS._SESSIONS.get(session_id)
    # Cheap short-circuit for main conversations (no disk read). NOTE: the
    # in-memory entry carries ``type`` but not the ``subagent`` payload — the
    # disk meta below is the authority for the subagent fields.
    if s is not None and s.get("type") != "subagent":
        return
    found = _NS._find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if not _NS._is_subagent(meta) or _NS._subagent_status(meta) in _NS.TERMINAL_STATUSES:
        return
    # Settled: drop the stop event so a stale set can never kill the wake turn.
    _NS._TURN_STOP.pop(session_id, None)

    if stop_evt is not None and stop_evt.is_set():
        await _finalize_subagent(slug, meta, "stopped")
        return
    err = None
    if turn_id:
        from ..api.sessions import _turn_last_error  # lazy: cycle

        err = _turn_last_error(session_id, turn_id)
    if err is not None:
        # A recursion-limited subagent gets ONE wrap-up continuation instead of
        # an empty-handed failure: the child already did the work, it just never
        # got to write the final report. The continuation instructs it to answer
        # from gathered material with no tool calls; its own settle then
        # finalizes done. Guarded once per subagent via the meta flag (a
        # disobedient wrap-up that hits the limit again fails for real).
        _err_text = str(err.get("message") if isinstance(err, dict) else err or "")
        _sub = dict(meta.get("subagent") or {})
        if "GraphRecursionError" in _err_text and not _sub.get("recursion_wrapped"):
            _sub["recursion_wrapped"] = True
            _NS._session_meta_patch(slug, session_id, {"subagent": _sub})
            _NS._log.info(
                "subagent_recursion_wrap session=%s parent=%s", session_id, meta.get("parent_session_id")
            )
            _NS._TURN_STOP.pop(session_id, None)
            _NS.spawn_bg(
                _NS._run_managed_turn(
                    session_id,
                    _NS.t(_NS._RECURSION_WRAP_TEXT_EN, _NS._RECURSION_WRAP_TEXT),
                    {"ginno_system_note": "recursion-wrap"},
                    _NS.asyncio.Event(),
                )
            )
            return
        # AUTO_RETRY exhausted upstream; the error is NOT mixed into any summary
        # (design §5.7) — it rides the failure report only.
        await _finalize_subagent(slug, meta, "failed", error=err)
        return
    if _NS._live_descendants(slug, session_id):
        await _mark_waiting(slug, meta)
        return
    await _finalize_subagent(slug, meta, "done")


async def on_turn_stopped(session_id: str) -> None:
    """Explicit user-stop finalize (parked-turn stop path in stream.py)."""
    found = _NS._find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if not _NS._is_subagent(meta) or _NS._subagent_status(meta) in _NS.TERMINAL_STATUSES:
        return
    _NS._TURN_STOP.pop(session_id, None)
    await _finalize_subagent(slug, meta, "stopped")


async def on_stop_if_waiting(session_id: str) -> None:
    """User pressed stop on a WAITING subagent (no live turn of its own): mark
    it stopped; the descendants were cascade-stopped by stop_descendants.

    minor-C: docstring-promised preconditions are now ENFORCED, under the turn
    lock. The window between a turn's normal completion and its settle hook
    (_TURN_TASKS/_RUNNING_TURNS already popped) used to let a stop that landed
    there finalize the just-succeeded turn as "stopped" instead of "done" —
    swallowing its upward injection. Holding the lock serializes against every
    scheduler-managed turn AND its settle; the live-turn guard + the strict
    ``waiting`` status re-read also refuse the WS-path settle (spawned outside
    the lock), where the meta still reads "running"."""
    async with _NS._turn_lock_of(session_id):
        task = _NS._TURN_TASKS.get(session_id)
        if session_id in _NS._RUNNING_TURNS or (task is not None and not task.done()):
            return  # a live turn exists: its own machinery settles it stopped
        found = _NS._find_meta(session_id)
        if not found:
            return
        meta, slug = found
        if not _NS._is_subagent(meta) or _NS._subagent_status(meta) != "waiting":
            return  # running (settle pending) or already terminal: not ours
        await _finalize_subagent(slug, meta, "stopped")


async def revive_failed_for_retry(session_id: str) -> None:
    """User hit retry on a FAILED subagent's error card (minor-6): the failed
    attempt already finalized the meta terminal ("failed"), and the completion
    gate skips terminal metas — without reviving it first, a successful retry
    would settle into a gate no-op and its result would never reach the parent.
    Flip "failed" → "running" so the retry's settle re-runs the gate."""
    found = _NS._find_meta(session_id)
    if not found:
        return
    meta, slug = found
    if not _NS._is_subagent(meta) or _NS._subagent_status(meta) != "failed":
        return
    sa = dict(meta.get("subagent") or {})
    sa["status"] = "running"
    _NS._session_meta_patch(slug, session_id, {"subagent": sa})
    _NS._touch_in_memory(session_id, {"subagent": sa})
    await _NS._push_both(
        meta.get("parent_session_id"),
        session_id,
        "subagent.status",
        {
            "session_id": session_id,
            "parent_session_id": meta.get("parent_session_id"),
            "status": "running",
        },
    )
    _NS._log.info("subagent_retry_revived session=%s", session_id)


async def _mark_waiting(slug: str, meta: dict) -> None:
    sa = dict(meta.get("subagent") or {})
    if sa.get("status") == "waiting":
        return
    sa["status"] = "waiting"
    _NS._session_meta_patch(slug, meta["id"], {"subagent": sa})
    _NS._touch_in_memory(meta["id"], {"subagent": sa})
    await _NS._push_both(
        meta.get("parent_session_id"),
        meta["id"],
        "subagent.status",
        {
            "session_id": meta["id"],
            "parent_session_id": meta.get("parent_session_id"),
            "status": "waiting",
        },
    )
    _NS._log.info("subagent_waiting session=%s", meta["id"])


# Session ids with a finalize IN FLIGHT. The terminal check ("is the meta
# already terminal?") happens BEFORE the summary work, and a ``done`` finalize
# can suspend for seconds on the compression LLM call — long enough for a
# second gate invocation on the same session (e.g. a user invoke into the
# session whose wake turn just freed both busy registries) to pass the same
# check and finalize again: double compression, double broadcast, and the SAME
# result injected into the ancestor twice. First-wins; the loser returns (its
# status intent is already covered — see the note at the latch).
_FINALIZING: set[str] = set()


async def _finalize_subagent(
    slug: str, meta: dict, status: str, error: str | None = None
) -> None:
    """Write the terminal status (+ summary when done), broadcast, and inject
    into the parent. Summary + upward injection happen ONCE, here (§5.6)."""
    child_id = meta["id"]
    if child_id in _FINALIZING:
        _NS._log.info(
            "subagent_finalize_skipped_inflight session=%s attempted=%s",
            child_id, status,
        )
        return
    _FINALIZING.add(child_id)
    try:
        await _finalize_subagent_locked(slug, meta, status, error)
    finally:
        _FINALIZING.discard(child_id)


async def _finalize_subagent_locked(
    slug: str, meta: dict, status: str, error: str | None = None
) -> None:
    child_id = meta["id"]
    sa = dict(meta.get("subagent") or {})
    parent_id = meta.get("parent_session_id")
    report = ""
    summary = ""
    if status == "done":
        report = await _NS._final_report_text(slug, child_id)
        summary = await _NS._maybe_compress_summary(report, meta)
    sa["status"] = status
    sa["result_summary"] = summary
    _NS._session_meta_patch(slug, child_id, {"subagent": sa})
    _NS._touch_in_memory(child_id, {"subagent": sa})
    payload: dict = {
        "session_id": child_id,
        "parent_session_id": parent_id,
        "status": status,
    }
    if status == "done":
        payload["result_summary"] = summary
    if status == "failed" and error:
        payload["error"] = error
    await _NS._push_both(parent_id, child_id, "subagent.status", payload)
    _NS._log.info(
        "subagent_finalized session=%s status=%s parent=%s summary_len=%d",
        child_id, status, parent_id, len(summary),
    )
    # LLM title (P2 contract 6): after the FIRST completed turn, one auxiliary
    # call with the session's own model replaces the goal[:40] placeholder with
    # a short verb-phrase title (≤16 chars), refreshed through the existing
    # ``session_title`` event. Fire-and-forget, independent of the upward
    # injection (a subagent whose parent is already terminal still gets its
    # title); any failure keeps the P1 goal[:40] placeholder.
    if status == "done" and not sa.get("titled"):
        _NS.spawn_bg(_gen_subagent_title(child_id, report))

    if status == "stopped":
        # User kill: no result injection (design §5.7). But a WAITING ancestor
        # whose whole subtree is now dead must be woken to wrap up (§5.6).
        await _NS._reevaluate_waiting_parent(parent_id)
        return
    if not parent_id:
        return
    # A terminal parent must never be injected (design §5.6: 被级联停止的子代
    # 不向已停止的父注入结果). Without this gate a descendant finishing after
    # its parent was cascade-stopped would wake the dead session and burn a
    # turn on it. Main conversations carry no subagent status and always pass.
    pfound = _NS._find_meta(parent_id)
    if pfound and _NS._is_subagent(pfound[0]) and _NS._subagent_status(pfound[0]) in _NS.TERMINAL_STATUSES:
        _NS._log.info(
            "subagent_inject_skipped_terminal_parent parent=%s child=%s status=%s",
            parent_id, child_id, _NS._subagent_status(pfound[0]),
        )
        return
    goal = str(sa.get("goal") or "")
    if status == "failed":
        text = _NS.format_subagent_failure(
            child_id, goal, error or _NS.t("unknown error", "未知错误")
        )
    else:
        text = _NS.format_subagent_result(
            child_id, goal, summary, str(sa.get("acceptance") or "")
        )
    await _NS._inject_result(parent_id, text, child_id)


# --------------------------------------------------------------------------- #
# summary (§5.5 step 1)
# --------------------------------------------------------------------------- #

async def _final_report_text(slug: str, session_id: str) -> str:
    """The final report = the last assistant text in the child's checkpoint."""
    from langchain_core.messages import AIMessage

    from ..checkpointer import FileCheckpointer
    from ..graph import text_of_content

    try:
        tup = await FileCheckpointer(slug).aget_tuple(
            {"configurable": {"thread_id": session_id}}
        )
    except Exception:
        _NS._log.exception("subagent_report_read_failed session=%s", session_id)
        return ""
    messages = ((tup.checkpoint.get("channel_values") or {}).get("messages") or []) if tup else []
    for m in reversed(messages):
        if isinstance(m, AIMessage):
            text = text_of_content(getattr(m, "content", "")).strip()
            if text:
                return text
    return ""


async def _maybe_compress_summary(report: str, meta: dict) -> str:
    report = (report or "").strip()
    if not report:
        return _NS.t("(subagent produced no text output)", "(subagent 未产生文本输出)")
    if len(report) <= _NS.SUMMARY_MAX_CHARS:
        return report
    try:
        if _NS.os.environ.get("GINNO_FAKE_LLM"):
            raise RuntimeError("GINNO_FAKE_LLM")  # deterministic-demo seam
        from langchain_core.messages import HumanMessage, SystemMessage

        from ..models import build_model

        model = build_model(meta.get("provider"), meta.get("model"))
        resp = await model.ainvoke(
            [
                SystemMessage(
                    content=_NS.t(
                        "Compress the subagent final report below into a "
                        "summary of at most 500 characters: keep the "
                        "conclusions, key artifact paths, and acceptance-check "
                        "results; add nothing beyond the report, and no "
                        "opening preamble.",
                        "把下面这份 subagent 最终报告压缩为不超过 500 字的摘要："
                        "保留结论、关键产出物路径、验收对照结果；"
                        "不要添加报告之外的信息，不要写开场白。",
                    )
                ),
                HumanMessage(content=report),
            ]
        )
        from ..graph import text_of_content

        # Usage telemetry (docs/dream-mode-design.md §8.1 #4).
        from .. import usage as _usage_mod

        _usage_mod.record_message(
            resp,
            source="subagent_gate",
            provider=str(meta.get("provider") or ""),
            model=str(meta.get("model") or ""),
            session_id=meta.get("id"),
        )
        text = text_of_content(getattr(resp, "content", "")).strip()
        if text:
            return text
    except Exception:
        # Compression is best-effort; the truncated report is still truthful.
        _NS._log.info(
            "subagent_summary_compress_skipped session=%s", meta.get("id")
        )
    return report[: _NS.SUMMARY_MAX_CHARS] + _NS.t(
        "…(report too long, truncated)", "…（原文过长，已截断）"
    )


# --------------------------------------------------------------------------- #
# LLM title (P2 contract 6, design §5.9)
# --------------------------------------------------------------------------- #

_SUBAGENT_TITLE_SYSTEM_EN = (
    "You are a session titler for a delegated subagent. Read its goal and the "
    "excerpt of its final report, then produce ONE very short verb-phrase "
    "title (at most 16 characters) naming WHAT it set out to do — e.g. "
    "\"research OAuth library options\", \"write integration test "
    "skeleton\". Not a sentence, no quotes, no trailing punctuation, no "
    "preamble. Answer in the goal's language."
)

_SUBAGENT_TITLE_SYSTEM = (
    "You are a session titler for a delegated subagent. Read its goal and the "
    "excerpt of its final report, then produce ONE very short verb-phrase "
    "title (at most 16 characters) naming WHAT it set out to do — e.g. "
    "「调研 OAuth 库选型」「编写集成测试骨架」. Not a sentence, no quotes, "
    "no trailing punctuation, no preamble. Answer in the goal's language."
)


async def _gen_subagent_title(child_id: str, report: str) -> None:
    """Generate the short title after the subagent's first completed turn.

    Degradation contract: GINNO_FAKE_LLM skips (deterministic demo), a model
    build/call failure or an unusable answer keeps the goal[:40] placeholder
    (the P1 behavior). Applied at most once — the ``subagent.titled`` flag on
    the meta is the claim, re-read with no await before the write (the same
    check-at-write discipline title_gen uses against a manual rename).
    """
    import os

    if os.environ.get("GINNO_FAKE_LLM"):
        return
    try:
        from langchain_core.messages import HumanMessage, SystemMessage

        from ..models import build_model
        from ..title_gen import _content_text, _sanitize_title

        found = _NS._find_meta(child_id)
        if not found or ((found[0].get("subagent") or {}).get("titled")):
            return
        meta, slug2 = found
        sa = meta.get("subagent") or {}
        goal = str(sa.get("goal") or "").strip()
        if not goal:
            return
        model = build_model(meta.get("provider"), meta.get("model"))
        resp = await model.ainvoke(
            [
                SystemMessage(
                    content=_NS.t(_SUBAGENT_TITLE_SYSTEM_EN, _SUBAGENT_TITLE_SYSTEM)
                ),
                HumanMessage(
                    content=_NS.t(
                        f"Goal: {goal}\nFinal report excerpt: "
                        f"{(report or '').strip()[:800]}",
                        f"目标：{goal}\n"
                        f"最终报告节选：{(report or '').strip()[:800]}",
                    )
                ),
            ]
        )
        title = _sanitize_title(_content_text(resp))[: _NS.SUBAGENT_TITLE_MAX_CHARS].strip()
        # Usage telemetry (docs/dream-mode-design.md §8.1 #4).
        from .. import usage as _usage_mod

        _usage_mod.record_message(
            resp,
            source="subagent_gate",
            provider=str(meta.get("provider") or ""),
            model=str(meta.get("model") or ""),
            session_id=child_id,
        )
        if not title:
            _NS._log.info("subagent_title_skipped session=%s reason=empty", child_id)
            return
        # Claim at write: only the first generator wins.
        found = _NS._find_meta(child_id)
        if not found or ((found[0].get("subagent") or {}).get("titled")):
            return
        sa = dict(found[0].get("subagent") or {})
        sa["titled"] = True
        updated = _NS._session_meta_patch(slug2, child_id, {"title": title, "subagent": sa})
        if updated is None:
            return  # deleted mid-flight
        _NS._touch_in_memory(child_id, {"subagent": sa, "title": title})
        await _NS._push_session_event(child_id, "session_title", {"title": title})
        _NS._log.info(
            "subagent_titled session=%s title=%r", child_id, title,
        )
    except Exception:
        # Best-effort: the goal[:40] placeholder stays truthful.
        _NS._log.info("subagent_title_failed session=%s", child_id)
