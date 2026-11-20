"""sessions 终态域：外部委托（external-agents / delegations stop）与
会话删除 / 批量归档删除 / 停止。

stop_session 恒答 202：running → 协作停止（先暂停活跃 goal）、parked →
heal+广播、waiting subagent → 级联终态、idle → 幂等 no-op。
"""

from __future__ import annotations

import asyncio

from fastapi import HTTPException
from pydantic import BaseModel

from ... import paths
from ... import server_shared as shared
from ...goals import store as goal_store
from ...server_shared import (
    _PENDING_KIND,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSIONS,
    _TURN_STOP,
    _TURN_TASKS,
    _log,
    spawn_bg,
)
from ...session_meta import (
    _find_meta,
    _session_meta_patch,
    _session_meta_remove,
    _session_slug,
)
from ...subagent_scheduler import _is_subagent, _subagent_status, stop_subagent_tree
from ._base import _ensure_session, router
from .goal import _emit_goal_event, _stop_goal_driver


@router.get("/api/external-agents")
async def list_external_agents() -> list[dict]:
    """Backend detection for Settings → External Agents（安装状态 + 路径）。"""
    from ...tools.external_agent import _BACKENDS

    out: list[dict] = []
    for _name, _be in _BACKENDS.items():
        try:
            _path = _be.available()
        except Exception:  # noqa: BLE001 — 检测绝不 500
            _path = None
        # 已安装但未完成首次初始化 → 前端给引导提示（pi 需要终端跑一次 =
        # needsInit；dsh 需要配 DEEPSEEK_API_KEY = needsAuth）。
        _hint = ""
        if _path:
            try:
                _hint = (
                    "" if _be.configured()
                    else getattr(_be, "setup_hint_code", "needsInit")
                )
            except Exception:  # noqa: BLE001
                _hint = ""
        out.append({"name": _name, "installed": bool(_path), "path": _path or "",
                    "setupHint": _hint})
    return out


@router.post("/api/delegations/{delegation_id}/stop")
async def stop_delegation(delegation_id: str) -> dict:
    """取消运行中的外部委托：直杀子进程，bg 随后自行回填 error 终态并注入。"""
    from ...tools.external_agent import (
        _ACTIVE_DELEGATIONS,
        _ACTIVE_LOCK,
        _kill_tree,
    )

    with _ACTIVE_LOCK:
        entry = _ACTIVE_DELEGATIONS.get(delegation_id)
    if not entry:
        return {"ok": True, "killed": False, "reason": "not running"}
    _kill_tree(entry["proc"])
    return {"ok": True, "killed": True}


async def _heal_run_history_refs() -> None:
    """Delete 收尾：立即清掉 runs 日志里指向已删会话的引用。

    Run-history 引用不再阻止删除（见 delete_session 的 guard 注释）后，原本
    每日 hygiene 兜底的 heal_dead_session_refs 改为删除后立即跑一次，让运行
    历史的「查看会话」当场显示「已删除」而不是等到第二天。best-effort：
    失败只记日志，日线志的兜底仍在。
    """
    try:
        from ... import schedule_store
        from ...diagnostics import _valid_session_ids

        schedule_store.heal_dead_session_refs(_valid_session_ids())
    except Exception:  # noqa: BLE001
        _log.exception("run_refs_heal_after_delete_failed")


@router.delete("/api/sessions/{session_id}")
async def delete_session(
    session_id: str, cascade: bool = False, force: bool = False
) -> dict:
    """Delete a session: its index entry, on-disk checkpoint history, and any
    in-memory graph cache. Returns ok=True even if the id was already gone.

    ``cascade`` (=1, sent by the web shell for a parent with descendants,
    subagent-design.md §5.8) is DECLARED for contract visibility: subagent
    descendants are always cascade-deleted/stopped regardless, so the flag
    only documents the client's intent (and the confirm-dialog count).

    The session's files directory (`sessions/<session_id>/`) is intentionally
    PRESERVED — only the conversation (checkpoint + index row) is removed. The
    files stay browsable/cleanable via Settings → 会话文件. Note the checkpoint
    is the *file* `sessions/<session_id>.json`; the preserved dir is
    `sessions/<session_id>/` — never glob `<session_id>*` here.
    """
    # P2-1 reference guard: deleting a session a TEAM points at silently
    # breaks team chat — refuse unless the caller forces it (bulk delete
    # reports the skipped ids instead). Run-history rows deliberately don't
    # block: they're append-only logs that self-heal via
    # heal_dead_session_refs (see _heal_run_history_refs below), not live
    # pointers — counting them made every scheduler-run session undeletable.
    if not force:
        try:
            from ...maintenance import session_reference_map

            refs = session_reference_map(include_run_history=False).get(session_id) or []
        except Exception:  # noqa: BLE001 — a broken store must not block deletion
            refs = []
        if refs:
            raise HTTPException(
                status_code=409,
                detail={
                    "ok": False,
                    "error": "session is referenced by other records",
                    "references": refs[:8],
                },
            )
    s = _SESSIONS.pop(session_id, None)
    slug = s["project_slug"] if s else None
    # 代码面板实时 diff 的 session 基线随会话删除清理（design §6.1，内存 map，
    # 不清理就是唯一的常驻增长点）。
    try:
        from ...files.baselines import clear_session

        clear_session(session_id)
    except Exception:
        _log.exception("baselines_clear_failed session=%s", session_id)
    # 渠道会话（wecom/dingtalk）删除前先取 channel 标记：meta 马上要没了，
    # 之后要靠它找到对应适配器解绑映射并通知 IM 侧。
    chan = None
    found = _find_meta(session_id)
    if found and found[0].get("type") == "channel":
        chan = str(found[0].get("channel") or "") or None
    # Settle a live subagent BEFORE its meta disappears (major-5): once the
    # index row is gone the completion gate's _find_meta finds nothing and no
    # one would re-evaluate a WAITING ancestor left behind by the removal.
    try:
        from ...subagent_scheduler import finalize_for_delete

        await finalize_for_delete(session_id)
    except Exception:
        _log.exception("subagent_delete_finalize_failed session=%s", session_id)
    removed = False
    # find the slug from the on-disk index if not known from memory
    for slug_dir in paths.home().glob("projects/*/sessions/_index.json"):
        cand = slug_dir.parent.parent.name
        if _session_meta_remove(cand, session_id):
            removed = True
            slug = slug or cand
    # Subagent cascade delete (subagent-design.md §5.8): cooperative-stop the
    # session + all descendants, WAIT for the turn tasks to unwind, then remove
    # each descendant's meta + checkpoint — never the reverse, or a live turn
    # would write its checkpoint back after the unlink.
    if slug:
        try:
            from ...subagent_scheduler import delete_subagent_tree

            cascade = await delete_subagent_tree(slug, session_id)
            if cascade:
                removed = True
        except Exception:
            _log.exception("subagent_cascade_delete_failed session=%s", session_id)
        shared.subagent_unlink_any(session_id)
    # drop the checkpoint file (the full conversation history; log + legacy)
    files_dir = None
    if slug:
        from ...checkpointer import remove_checkpoint_files

        if remove_checkpoint_files(slug, session_id):
            removed = True
        # preserved files dir (may not exist for legacy sessions)
        fd = paths.session_files_dir(slug, session_id)
        if fd.is_dir():
            files_dir = str(fd)
        # cascade: drop the session's goal (goal-design.md §4.1) and stop any
        # continuation driver still looping for it.
        try:
            goal_store.clear_goal(slug, session_id)
        except Exception:
            _log.exception("goal_cascade_delete_failed session=%s", session_id)
        _stop_goal_driver(session_id)
    # Mods/classic SessionEnd (claude-code-mods-design.md §5.3): observe-only;
    # the mods dispatch carries the 1.5s session.end budget internally.
    try:
        from ...mods.events import dispatch_session_end

        await dispatch_session_end(session_id)
    except Exception:
        _log.exception("mods_session_end_failed session=%s", session_id)
    # Vibe Island notch:SessionEnd(卡片移除)。对从未入卡的 id 发也无害。
    try:
        from ...vibe_island import session_ended

        session_ended(session_id)
    except Exception:
        _log.exception("vibe_island_session_end_failed session=%s", session_id)
    # 渠道会话删除 → 解绑 IM 映射并尽力推送提示（即使 removed=False，映射
    # 也可能仍指向这个已不存在的 id，同样值得清）。
    if chan:
        try:
            from ...channels.base import notify_channel_session_deleted

            await notify_channel_session_deleted(chan, session_id)
        except Exception:
            _log.exception("channel_delete_notice_failed channel=%s session=%s",
                           chan, session_id)
    if removed:
        await _heal_run_history_refs()
    return {"ok": True, "removed": removed, "files_dir": files_dir}


class BulkSessionsRequest(BaseModel):
    action: str  # "archive" | "unarchive" | "delete"
    ids: list[str]


@router.post("/api/sessions/bulk")
async def bulk_sessions(req: BulkSessionsRequest) -> dict:
    """Batch archive/unarchive/delete from the sidebar multi-select (P1-4).

    Delete skips (and reports) any session another record points at — the
    same guard as the single delete, so bulk cleanup can never create the
    dangling references DATA-06 just healed.
    """
    action = (req.action or "").strip()
    if action not in ("archive", "unarchive", "delete"):
        raise HTTPException(
            status_code=400, detail={"ok": False, "error": f"unknown action: {action}"}
        )
    ids = [str(s) for s in (req.ids or []) if str(s or "").strip()]
    refs_map: dict[str, list[str]] = {}
    if action == "delete":
        try:
            from ...maintenance import session_reference_map

            refs_map = session_reference_map(include_run_history=False)
        except Exception:  # noqa: BLE001
            refs_map = {}
    done: list[str] = []
    skipped: list[dict] = []
    for sid in ids:
        slug = _session_slug(sid)
        if not slug:
            skipped.append({"id": sid, "reason": "unknown"})
            continue
        if action == "delete":
            refs = refs_map.get(sid) or []
            if refs:
                skipped.append(
                    {"id": sid, "reason": "referenced", "references": refs[:5]}
                )
                continue
            try:
                await delete_session(sid)
            except Exception as e:  # noqa: BLE001 — report, keep going
                skipped.append({"id": sid, "reason": f"{type(e).__name__}: {e}"})
                continue
            done.append(sid)
            continue
        _session_meta_patch(
            slug, sid, {"archived": action == "archive"}, touch=False
        )
        done.append(sid)
    _log.info(
        "sessions_bulk action=%s done=%d skipped=%d", action, len(done), len(skipped)
    )
    return {"ok": True, "done": done, "skipped": skipped}


# ---- session stop (P2 contract 4) ------------------------------------------


@router.post("/api/sessions/{session_id}/stop", status_code=202)
async def stop_session(session_id: str) -> dict:
    """Stop whatever the session is doing; ALWAYS 202 ``{"stopped": true}``.

    Four shapes (P2 shared contract 4):

    * running turn → the cooperative-stop semantics of the WS ``stop`` branch:
      pause an active goal first, set the session's ``_TURN_STOP`` event (the
      turn's own machinery unwinds + settles it), and for a subagent session
      cascade-stop the descendants + settle a waiting meta;
    * parked at a permission/ask_user interrupt → heal + broadcast via the
      same ``_stop_parked_turn`` path the WS branch uses;
    * waiting subagent (no live turn of its own) → ``stop_subagent_tree``:
      finalize it stopped and cascade over every descendant;
    * idle → a no-op that still answers 202 (idempotent UX).
    """
    from ..stream import _stop_parked_turn  # lazy: api.stream imports this module

    s = _SESSIONS.get(session_id)
    slug = s["project_slug"] if s else _session_slug(session_id)
    mode = "idle"

    if session_id in _PENDING_RESUME:
        # Parked at an interrupt: no live task — heal + broadcast directly
        # (check-and-discard, so a racing second stop no-ops).
        mode = "parked"
        _PENDING_RESUME.discard(session_id)
        _PENDING_KIND.pop(session_id, None)
        tid = _RUNNING_TURNS.pop(session_id, "")
        sess = s or _ensure_session(session_id)
        if sess is not None:
            spawn_bg(_stop_parked_turn(sess, session_id, tid))
        # Subagent cascade (§5.6 配套规则): the WS stop branch runs it for a
        # parked subagent too — the heal above settles the session itself
        # "stopped" (via _stop_parked_turn → on_turn_stopped), and a stopped
        # subagent must take its running descendants with it. Without this the
        # HTTP path left them alive under a stopped parent: their results are
        # later refused at the terminal-parent gate and silently lost.
        stype = (s or {}).get("type")
        if stype is None:
            _pf = _find_meta(session_id)
            stype = (_pf[0] or {}).get("type") if _pf else None
        if stype == "subagent":
            from ...subagent_scheduler import on_stop_if_waiting, stop_descendants

            async def _stop_cascade(_sid: str = session_id) -> None:
                try:
                    await stop_descendants(_sid)
                except Exception:
                    _log.exception("stop_session_cascade_failed session=%s", _sid)

            spawn_bg(_stop_cascade())
            spawn_bg(on_stop_if_waiting(session_id))
    elif session_id in _RUNNING_TURNS or (
        (t := _TURN_TASKS.get(session_id)) is not None and not t.done()
    ):
        mode = "running"
        # Goal parity with the WS stop branch: an active goal would
        # auto-continue ~3s after the turn unwinds — pause it first.
        if slug:
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
                _log.exception("stop_session_goal_pause_failed session=%s", session_id)
        evt = _TURN_STOP.get(session_id)
        if evt is None:
            evt = _TURN_STOP[session_id] = asyncio.Event()
        evt.set()
        # Subagent cascade (§5.6 配套规则): a stopped running/waiting subagent
        # takes its descendants with it; a waiting meta with no live turn is
        # settled here. Main conversations keep their subagents running.
        stype = (s or {}).get("type")
        if stype is None:
            found = _find_meta(session_id)
            stype = (found[0] or {}).get("type") if found else None
        if stype == "subagent":
            from ...subagent_scheduler import on_stop_if_waiting, stop_descendants

            async def _stop_cascade(_sid: str = session_id) -> None:
                try:
                    await stop_descendants(_sid)
                except Exception:
                    _log.exception("stop_session_cascade_failed session=%s", _sid)

            spawn_bg(_stop_cascade())
            spawn_bg(on_stop_if_waiting(session_id))
    else:
        found = _find_meta(session_id)
        if found and _is_subagent(found[0]) and _subagent_status(found[0]) == "waiting":
            # A waiting subagent holds no turn: finalize it stopped and
            # cascade over the still-running descendants.
            mode = "waiting"
            try:
                await stop_subagent_tree(session_id, include_self=True)
            except Exception:
                _log.exception("stop_session_waiting_failed session=%s", session_id)
        # else: idle — no-op, still 202 (never leave a set event behind).

    _log.info("session_stop session=%s mode=%s", session_id, mode)
    return {"stopped": True, "mode": mode}
