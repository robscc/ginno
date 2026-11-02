"""Mirror workflow agent-step conversations into replayable sessions.

Workflow agent steps run inside the engine (goal→tools loop in
``_run_agent_turn``) — without this their dialogue exists only as run events.
Scheduled agent tasks, by contrast, produce fully replayable sessions, and
the user reads both the same way. This module gives every workflow run a real
session (type ``workflow``, title ``⚙️ …``, meta ``workflow_run_id``) and
appends each agent turn's messages (Human=step goal, then the AI/tool loop)
to its checkpoint — the sessions list shows the run and the chat UI replays
the conversation exactly like a scheduled task's session.

Mirrors are pure appends (pure-append invariant, checkpointer delta mode):
never rewrite an existing message. Parallel-loop items serialize on a
per-session lock; a failure to mirror is logged and swallowed — losing the
transcript must never fail the run.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

from ..checkpointer import FileCheckpointer
from ..server_shared import _log

_LOCKS: dict[str, asyncio.Lock] = {}


def _lock(sid: str) -> asyncio.Lock:
    return _LOCKS.setdefault(sid, asyncio.Lock())


# 前端 sessionVisibility.ts 整类隐藏的 type(2026-11-02 泄漏事故:定时任务
# 影子会话里 agent 调 workflow_run 工具,转录会话以 type=workflow 顶层落进
# 「我的对话」)。挂到隐藏父会话名下即可复用同一套「隐藏沿子树传递」。
_HIDDEN_PARENT_TYPES = {"scheduled", "team", "team-member"}


def _hidden_parent(present_in: str | None) -> str | None:
    """触发会话若属隐藏类(scheduled/team/...),返回其 id(否则 None)。"""
    if not present_in:
        return None
    try:
        from ..session_meta import _resolve_session_meta

        pm = _resolve_session_meta(str(present_in))
        return str(present_in) if pm and pm.get("type") in _HIDDEN_PARENT_TYPES else None
    except Exception:  # noqa: BLE001 — 判定失败按普通会话处理(顶层可见)
        return None


async def begin_run_session(
    wf: dict, run: dict, project_slug: str = "default", present_in: str | None = None
) -> str | None:
    """Create the run's transcript session; returns its id (None on failure).

    ``present_in``: the chat session that triggered the run. When it is a
    hidden-kind session (scheduled shadow / team), the transcript is stamped
    its ``parent_session_id`` so the frontend's transitive hiding keeps it
    out of「我的对话」— the scheduled timeline's run view is its entry point.
    Runs triggered headless BY the scheduler (``run.origin == "schedule"``)
    have no trigger session to hang from; those transcripts are created as
    ``type="scheduled"`` and hide with that whole class instead."""
    try:
        from ..api import sessions as _sessions_api
        from ..session_meta import _session_meta_patch

        parent = _hidden_parent(present_in)
        req = _sessions_api.CreateSessionRequest(
            project_slug=project_slug,
            workspace="",  # create_session keys on the per-session dir
            title=f"⚙️ {(wf or {}).get('name') or 'Workflow'}",
            type="scheduled" if (run or {}).get("origin") == "schedule" else "workflow",
        )
        meta = await _sessions_api.create_session(req)
        if meta.get("ok") is False or not meta.get("id"):
            return None
        sid = meta["id"]
        # parent 走 meta patch 而非请求字段:请求里的 parent_session_id 会把
        # 会话当 subagent 处理(type 覆盖/depth 上限/工具集收紧),转录会话
        # 只需要在磁盘索引上挂链——前端隐藏传递只读索引。
        patch: dict = {"workflow_run_id": (run or {}).get("id")}
        if parent:
            patch["parent_session_id"] = parent
        _session_meta_patch(project_slug, sid, patch)
        return sid
    except Exception:  # noqa: BLE001 — transcript is best-effort
        _log.exception("workflow_transcript_session_failed run=%s", (run or {}).get("id"))
        return None


async def mirror_turn(
    session_id: str | None, project_slug: str, goal: str, msgs: list, node_id: str = ""
) -> None:
    """Append one agent turn (Human=goal + AI/tool messages) to the session."""
    if not session_id:
        return
    turn_msgs = [m for m in msgs if getattr(m, "type", None) != "system"]
    if not turn_msgs:
        return
    try:
        async with _lock(session_id):
            cp = FileCheckpointer(project_slug)
            cfg = {"configurable": {"thread_id": session_id}}
            tup = await cp.aget_tuple(cfg)
            prev = tup.checkpoint if (tup and tup.checkpoint) else {}
            values = dict(prev.get("channel_values") or {})
            values["messages"] = list(values.get("messages") or []) + turn_msgs
            versions = prev.get("channel_versions") or {}
            checkpoint = {
                "v": prev.get("v") or 1,
                "id": uuid.uuid4().hex,
                "ts": datetime.now(timezone.utc).isoformat(),
                "channel_values": values,
                "channel_versions": dict(versions),
            }
            await cp.aput(
                cfg,
                checkpoint,
                {"source": "workflow", "node": node_id, "writes": []},
                versions,
            )
    except Exception:  # noqa: BLE001
        _log.exception("workflow_transcript_mirror_failed session=%s node=%s", session_id, node_id)
