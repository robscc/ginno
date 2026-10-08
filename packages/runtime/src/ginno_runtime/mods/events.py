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
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("ginno.mods")

DEFAULT_DEADLINE_MS = 10_000
# session.end carries a 1.5s total budget (design §3.2 / §15.4).
SESSION_END_DEADLINE_MS = 1_500
# session.compact sits on the TURN path (checked before each turn's messages
# land) — a 2s cap keeps a hung mod from delaying the turn it precedes.
COMPACT_DEADLINE_MS = 2_000
# agent.spawn is a one-off user/model action, not a per-turn tap; 5s leaves a
# deliberating mod room without making a denied spawn feel hung.
AGENT_SPAWN_DEADLINE_MS = 5_000


async def _classic_dispatch(event_name: str, context: dict, matcher: str | None = None) -> list:
    """Run the classic hooks (hooks/dispatcher.py: settings.json hooks plus
    plugin ``hooks.json`` entries) — ALWAYS before the mods broker (design
    §10 ordering: settings hooks first, mods after). No hooks configured →
    an empty list and no subprocess; never raises."""
    try:
        from ..hooks.dispatcher import HookEvent
        from ..server_shared import _hooks

        if _hooks is None:
            return []
        return await _hooks.dispatch(HookEvent(name=event_name, context=context), matcher=matcher)
    except Exception:  # noqa: BLE001 — classic hooks must never break the engine
        log.exception("classic hook dispatch failed event=%s", event_name)
        return []


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
    # Classic SessionStart hooks first (observe), then the mods bus.
    await _classic_dispatch(
        "SessionStart", {"session_id": session_id, "cwd": meta.get("workspace") or ""}
    )
    payload = {
        "sessionId": session_id,
        "cwd": meta.get("workspace") or "",
        "surface": None,  # panes land in P2 (design §7.4)
        "isInteractive": False,
    }
    await maybe_dispatch(session_id, "session.start", payload)


async def dispatch_session_end(session_id: str) -> None:
    # Classic SessionEnd hooks first (observe), then the mods bus.
    await _classic_dispatch("SessionEnd", {"session_id": session_id})
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


@dataclass
class PromptSubmitResult:
    """The prompt.submit consumption contract (P1, design §15.6): ``text`` is
    the (possibly rewritten) prompt body, ``blocked`` ends the turn outright
    (the frontend gets a notice), ``context`` rides the steer channel as a
    companion HumanMessage, ``mod`` is the answering mod's name when the
    broker reported one (it labels the injected context's origin). ``source``
    says who blocked: the classic hooks ("classic") or the mods bus ("mod")."""

    text: str
    blocked: bool = False
    context: str | None = None
    mod: str | None = None
    source: str = "mod"


def _settle(result: Any, key: str) -> Any:
    """Read one answer key off a chain-settle value (§6.1 convention): the
    broker merges the final hook answer into the event input at top level;
    an ``answer`` object next to it is accepted too — both spellings appear
    in the wild."""
    if not isinstance(result, dict):
        return None
    top = result.get(key)
    if top is not None:
        return top
    answer = result.get("answer")
    if isinstance(answer, dict):
        return answer.get(key)
    return None


def _mod_name(result: Any) -> str | None:
    mod = _settle(result, "mod")
    return mod if isinstance(mod, str) and mod else None


async def dispatch_prompt_submit(session_id: str, text: str) -> PromptSubmitResult:
    """Full P1 semantics (§15.6): classic UserPromptSubmit hooks run first
    (their ``rewrite`` replaces the prompt text, ``block`` drops it), then the
    mods bus answers ``{drop}`` (turn ends with a notice), ``{text}`` (body
    rewrite) and/or ``{context}`` (extra context, injected on the steer
    channel by the caller with a ``Message from the "<mod>" mod:`` origin).
    Results are liberal per §15.6: a non-string ``text`` keeps the original,
    ``context`` keeps only its string lines."""
    text = text or ""
    for r in await _classic_dispatch("UserPromptSubmit", {"prompt": text}):
        if r.block:
            return PromptSubmitResult(text=text, blocked=True, source="classic")
        if r.rewrite:
            text = r.rewrite
    payload = {"sessionId": session_id, "text": text, "origin": {"kind": "composer"}}
    result = await maybe_dispatch(session_id, "prompt.submit", payload)
    if not isinstance(result, dict):
        return PromptSubmitResult(text=text)
    mod = _mod_name(result)
    drop = _settle(result, "drop")
    if drop:  # §15.6: drop must be a string — a reason here, but stay liberal
        log.info("mods prompt.submit drop session=%s reason=%r", session_id, drop)
        return PromptSubmitResult(text=text, blocked=True, mod=mod)
    if isinstance(_settle(result, "text"), str):
        text = _settle(result, "text")
    raw_ctx = _settle(result, "context")
    if isinstance(raw_ctx, str):
        lines = [raw_ctx]
    elif isinstance(raw_ctx, list):
        lines = [x for x in raw_ctx if isinstance(x, str)]
    else:
        lines = []
    context = "\n".join(x for x in (ln.strip() for ln in lines) if x) or None
    return PromptSubmitResult(text=text, context=context, mod=mod)


def _deny_reason(value: Any) -> str | None:
    """Normalize the DSH deny shapes (§15.5) into a reason string:
    ``true`` / ``"reason"`` / ``{"reason": ..}`` / ``{"deny": ..}``."""
    if value is True:
        return "denied by mod"
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        inner = value.get("reason") or value.get("deny") or value.get("denied")
        return _deny_reason(inner) if inner is not None else None
    return None


async def dispatch_tool_call(session_id: str, tool: str, args: dict) -> dict | None:
    """The P1 tool.call consumption contract. Returns a normalized action dict
    (or None = allowed / no mod / timeout — the broker timeout policy is 放行,
    §6.1); graph.permission_node consumes it:

    - ``{"deny": "<reason>"}`` — the tool never runs, blocked bubble (existing
      P0 shape; ``true`` / ``{"reason": ..}`` normalize via :func:`_deny_reason`);
    - ``{"args": {...}}`` — the engine substitutes the rewritten args and
      re-runs the permission policy on them;
    - ``{"result": {"value": ..., "isError"?: bool}}`` — takeover: the tool
      does not execute, ``value`` becomes the tool result (error-shaped when
      ``isError``);
    - ``{"inject": "<text>"}`` — extra context, appended to this turn's
      message stream on the steer channel.

    Priority deny > result > args; ``inject`` may coexist with any of them.
    The optional ``mod`` key (answering mod's name) rides along when the
    broker reported it."""
    payload = {"sessionId": session_id, "tool": tool, "args": args if isinstance(args, dict) else {}}
    result = await maybe_dispatch(session_id, "tool.call", payload)
    if not isinstance(result, dict):
        return None
    action: dict[str, Any] = {}
    reason = _deny_reason(_settle(result, "deny"))
    if reason:
        action["deny"] = reason
    took = _settle(result, "result")
    if isinstance(took, dict) and "value" in took:
        action["result"] = took
    rewritten = _settle(result, "args")
    if isinstance(rewritten, dict) and rewritten:
        action["args"] = rewritten
    inject = _settle(result, "inject")
    if isinstance(inject, str) and inject.strip():
        action["inject"] = inject
    mod = _mod_name(result)
    if mod:
        action["mod"] = mod
    return action or None


async def dispatch_tool_check(session_id: str, tool: str, args: dict) -> dict | None:
    """tool.check (§5.3, P1): raised after the tool.call chain, before
    policy.decide. Returns ``{"decision": "allow"|"deny"|"ask", "reason"?: str}``
    or None (no answer / no mod → the policy decides as usual)."""
    payload = {"sessionId": session_id, "tool": tool, "args": args if isinstance(args, dict) else {}}
    result = await maybe_dispatch(session_id, "tool.check", payload)
    decision = _settle(result, "decision")
    if not isinstance(decision, str) or decision.lower() not in ("allow", "deny", "ask"):
        return None
    out: dict[str, Any] = {"decision": decision.lower()}
    reason = _settle(result, "reason")
    if isinstance(reason, str) and reason:
        out["reason"] = reason
    return out


async def dispatch_command_run(session_id: str, command: str, args: str) -> str | None:
    """command.run (§5.3): the user ran ``/name``, no builtin/skill matched and
    a mod registered the name — the broker routes the event to the owning
    mod's runner. Returns the mod's ``reply`` string (top level or nested
    ``answer.reply``) or None; the caller delivers it as a notice."""
    payload = {"sessionId": session_id, "command": command, "args": args or ""}
    result = await maybe_dispatch(session_id, "command.run", payload)
    reply = _settle(result, "reply")
    return reply if isinstance(reply, str) and reply.strip() else None


async def dispatch_session_compact(
    session_id: str, *, tokens: int, threshold: int, force: bool, messages: int
) -> bool:
    """session.compact (§5.3, P2): raised once compaction has actually been
    decided (threshold/manual passed, split found) — not on every turn check.
    A chain that answers ``{skip}`` (truthy, §6.1 settle convention) aborts
    THIS compaction only; the next turn re-checks the threshold. Returns True
    when skipped. Never raises (maybe_dispatch funnels the failure modes)."""
    payload = {
        "sessionId": session_id,
        "estimatedTokens": max(0, int(tokens)),
        "threshold": max(0, int(threshold)),
        "reason": "manual" if force else "threshold",
        "messages": max(0, int(messages)),
    }
    result = await maybe_dispatch(
        session_id, "session.compact", payload, deadline_ms=COMPACT_DEADLINE_MS
    )
    skipped = bool(_settle(result, "skip"))
    if skipped:
        log.info("mods session.compact skip session=%s reason=%s", session_id, payload["reason"])
    return skipped


async def dispatch_agent_spawn(
    session_id: str, agent: str, task: str, *, origin: str = "", depth: int = 0
) -> dict | None:
    """agent.spawn (§5.3): a subagent spawn is about to start. Returns
    ``{"deny": reason, "mod": name}`` when the chain answered ``{deny}``
    (DSH shapes normalize via :func:`_deny_reason`), else None (observe /
    no mod / timeout → the spawn proceeds). P2 does NOT model-rewrite the
    spawn — the answer's ``model`` key is ignored here by design."""
    payload = {
        "sessionId": session_id,
        "agent": agent or "general",
        "task": task or "",
        "origin": origin or "",
        "depth": max(0, int(depth)),
    }
    result = await maybe_dispatch(
        session_id, "agent.spawn", payload, deadline_ms=AGENT_SPAWN_DEADLINE_MS
    )
    if not isinstance(result, dict):
        return None
    reason = _deny_reason(_settle(result, "deny"))
    if not reason:
        return None
    return {"deny": reason, "mod": _mod_name(result)}
