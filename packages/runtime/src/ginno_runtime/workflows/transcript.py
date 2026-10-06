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


async def begin_run_session(wf: dict, run: dict, project_slug: str = "default") -> str | None:
    """Create the run's transcript session; returns its id (None on failure)."""
    try:
        from ..api import sessions as _sessions_api
        from ..session_meta import _session_meta_patch

        req = _sessions_api.CreateSessionRequest(
            project_slug=project_slug,
            workspace="",  # create_session keys on the per-session dir
            title=f"⚙️ {(wf or {}).get('name') or 'Workflow'}",
            type="workflow",
        )
        meta = await _sessions_api.create_session(req)
        if meta.get("ok") is False or not meta.get("id"):
            return None
        sid = meta["id"]
        _session_meta_patch(project_slug, sid, {"workflow_run_id": (run or {}).get("id")})
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
