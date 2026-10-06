"""Engine event taps (claude-code-mods-design.md §5.3) + payload shapes.

The five P0 events — session.start / session.end / turn.start / turn.complete
/ prompt.submit / tool.call — each get one thin ``dispatch_*`` wrapper here;
the mount-site insertions stay 3-8 lines and funnel through
:func:`maybe_dispatch`, which short-circuits (no I/O, no await-worthy work)
when the ModChannel is not connected or no mod registered the event.
Every failure mode resolves with the original payload / a None answer:
mods are strictly additive, a misbehaving bus can never stall a turn.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ginno.mods")

DEFAULT_DEADLINE_MS = 10_000
# session.end carries a 1.5s total budget (design §3.2 / §15.4).
SESSION_END_DEADLINE_MS = 1_500


async def maybe_dispatch(session_id: str, event: str, payload: dict, deadline_ms: int = DEFAULT_DEADLINE_MS) -> dict:
    """The one funnel every mount site calls. Returns the (possibly rewritten,
    P1) event input; observe-only in P0. Never raises."""
    try:
        from .channel import get_channel

        ch = get_channel()
        ch.ensure_started()  # lazy: the first event of a session starts the bus
        if not ch.active_for(event):
            return payload
        return await ch.dispatch_event(session_id, event, payload, deadline_ms)
    except Exception:  # noqa: BLE001 — the taps must never break the engine
        log.exception("mods dispatch failed event=%s", event)
        return payload


# ---- per-event wrappers (payload shapes per design §5.3 / §15.7) ----------------


async def dispatch_session_start(session_id: str, meta: dict) -> None:
    payload = {
        "sessionId": session_id,
        "cwd": meta.get("workspace") or "",
        "surface": None,  # panes land in P2 (design §7.4)
        "isInteractive": False,
    }
    await maybe_dispatch(session_id, "session.start", payload)


async def dispatch_session_end(session_id: str) -> None:
    payload = {"sessionId": session_id, "reason": "other"}
    await maybe_dispatch(session_id, "session.end", payload, deadline_ms=SESSION_END_DEADLINE_MS)


async def dispatch_turn_start(session_id: str, turn_id: str) -> None:
    payload = {"sessionId": session_id, "turnId": turn_id}
    await maybe_dispatch(session_id, "turn.start", payload)


def spawn_turn_complete(
    session_id: str,
    turn_id: str,
    *,
    answer: str,
    duration_ms: int,
    is_aborted: bool,
    reason: str,
) -> None:
    """Detached turn.complete (§15.4: never blocks the completion bookkeeping).

    ``reason`` ∈ answer | aborted | error; the three engine close-out branches
    (completed / TurnStopped / error) all funnel here."""
    from ..server_shared import spawn_bg

    usage: dict[str, Any] | None = None
    try:
        from ..server_shared import _USAGE_BY_SESSION

        acc = _USAGE_BY_SESSION.get(session_id)
        usage = dict(acc) if acc else None
    except Exception:  # noqa: BLE001
        usage = None
    payload = {
        "sessionId": session_id,
        "turnId": turn_id,
        "answer": answer or "",
        "durationMs": max(0, int(duration_ms)),
        "isAborted": bool(is_aborted),
        "reason": reason,
        "usage": usage,
    }
    spawn_bg(maybe_dispatch(session_id, "turn.complete", payload))


async def dispatch_prompt_submit(session_id: str, text: str) -> str:
    """Returns the (P0 unchanged) prompt text. P1 adds drop/rewrite + the
    context algorithm (§15.6) on this same seam."""
    payload = {"sessionId": session_id, "text": text or "", "origin": {"kind": "composer"}}
    result = await maybe_dispatch(session_id, "prompt.submit", payload)
    return result.get("text") if isinstance(result.get("text"), str) else (text or "")


async def dispatch_tool_call(session_id: str, tool: str, args: dict) -> str | None:
    """Returns a deny reason string when a mod answered ``{deny}``, else None
    (allowed / no mod / timeout — the broker timeout policy is 放行, §6.1).
    The deny string is rendered with the same bubble shape as the hooks
    rejection in graph.permission_node."""
    payload = {"sessionId": session_id, "tool": tool, "args": args if isinstance(args, dict) else {}}
    result = await maybe_dispatch(session_id, "tool.call", payload)
    if not isinstance(result, dict):
        return None
    # Settle-value convention (§6.1): the broker replies with the final event
    # input plus, when a hook denied, a top-level ``deny`` (also accepted
    # nested under ``answer`` — both spellings appear in the wild).
    deny = result.get("deny")
    if not isinstance(deny, dict):
        answer = result.get("answer")
        deny = answer if isinstance(answer, dict) else {}
    if "deny" in deny or deny.get("denied"):
        reason = deny.get("reason") or deny.get("deny")
        return str(reason) if reason and reason is not True else "denied by mod"
    return None
