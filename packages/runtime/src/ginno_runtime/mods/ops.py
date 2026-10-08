"""Python-backed ``$`` ops for the mods bus (claude-code-mods-design.md §5.4).

The broker forwards ``$.session.*`` calls here as ``call`` frames; P0 ships
the read family only (id/cwd/root/model/version/turns/usage/messages) —
anything else answers ``no-implementation`` so a mod sees an explicit named
failure instead of silence (规范 §"未实现成员是命名 reject").

Facts come from the live session registry (server_shared._SESSIONS), the
on-disk session meta, the usage ledger, and a checkpointer history rebuild.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("ginno.mods")

# session.messages cap (design §5.4): the last 4096 entries suffice for any
# mod view; full history stays available through the transcript UI.
_MESSAGES_LIMIT = 4096


class OpError(Exception):
    """Op failure carried verbatim into the result frame's code/message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _runtime_version() -> str:
    try:
        from importlib.metadata import version

        return version("ginno-runtime")
    except Exception:  # noqa: BLE001 — frozen builds may lack dist-info
        return "0.1.0"


def _session_facts(session_id: str) -> dict:
    """Merge the in-memory entry and the on-disk meta for one session."""
    from ..server_shared import _SESSIONS
    from ..session_meta import _find_meta

    entry = _SESSIONS.get(session_id) or {}
    meta, _slug = _find_meta(session_id) or ({}, None)
    return {
        "entry": entry,
        "meta": meta if isinstance(meta, dict) else {},
        "slug": _slug or entry.get("project_slug") or "",
        "workspace": entry.get("workspace") or (meta or {}).get("workspace") or "",
        "model": entry.get("model_name") or (meta or {}).get("model") or "",
    }


async def _rebuild_history(session_id: str, limit: int = _MESSAGES_LIMIT) -> list:
    """Rebuild the persisted conversation from the file checkpointer.

    Same rebuild the history endpoint uses (api/sessions.get_session_history);
    returns raw LangChain messages, trimmed to the most recent ``limit``.
    """
    from ..checkpointer import FileCheckpointer
    from ..session_meta import _session_slug

    slug = _session_slug(session_id)
    if not slug:
        return []
    cfg = {"configurable": {"thread_id": session_id}}
    tup = await FileCheckpointer(slug).aget_tuple(cfg)
    if not tup or not tup.checkpoint:
        return []
    messages = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    return list(messages)[-limit:]


def _text_of(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multimodal blocks → the text ones only
        return "\n".join(
            b.get("text") or "" for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return str(content)


def to_canonical(message: Any) -> dict | None:
    """LangChain message → the canonical mod shape
    ``{role, text, toolUses|tool_use_id}``. Tool names are already host names
    (the broker maps aliases before the mod sees them, design §15.8)."""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    if isinstance(message, HumanMessage):
        return {"role": "user", "text": _text_of(message)}
    if isinstance(message, AIMessage):
        return {
            "role": "assistant",
            "text": _text_of(message),
            "toolUses": [
                {"tool_use_id": tc.get("id") or "", "tool": tc.get("name") or "", "input": tc.get("args") or {}}
                for tc in (getattr(message, "tool_calls", None) or [])
            ],
        }
    if isinstance(message, ToolMessage):
        return {"role": "toolResult", "text": _text_of(message), "tool_use_id": getattr(message, "tool_call_id", "")}
    return None  # SystemMessage and friends never reach a mod


# ---- the op table (ns="session") -----------------------------------------------

async def _op_id(args: dict, session_id: str) -> str:
    return session_id


async def _op_cwd(args: dict, session_id: str) -> str:
    return _session_facts(session_id)["workspace"]


async def _op_root(args: dict, session_id: str) -> str:
    # Ginno sessions are workspace-scoped (the session files dir IS the cwd);
    # there is no separate repo root, so root == cwd by convention.
    return _session_facts(session_id)["workspace"]


async def _op_model(args: dict, session_id: str) -> str:
    return _session_facts(session_id)["model"]


async def _op_version(args: dict, session_id: str) -> str:
    return _runtime_version()


async def _op_usage(args: dict, session_id: str) -> dict:
    """Canonical mods shape (design §15.7): ``{context: {tokens, window,
    percent}, rateLimits: []}`` — mods deref ``.context`` (token-weather was
    built on it), so returning the flat usage-store totals here silently broke
    every reading mod. Context size proxies the LAST LLM call's whole-prompt
    input (cumulative totals span calls and are not the window occupancy).
    The runtime does not track a model context window; ``mods.contextWindow``
    (default 200k) stands in. Unknown session → zero tokens, never null."""
    from .. import usage_store
    from ..server_shared import _USAGE_BY_SESSION
    from .bridge_utils import load_mods_settings

    window = int(load_mods_settings().get("contextWindow") or 200_000)
    tokens = 0
    try:
        rows = usage_store.query_requests(session_id=session_id, page=1, page_size=1).get("rows") or []
        if rows:
            last = rows[0]
            tokens = (
                (last.get("input_tokens") or 0)
                + (last.get("cache_read_tokens") or 0)
                + (last.get("cache_creation_tokens") or 0)
            )
    except Exception:  # noqa: BLE001 — usage log miss falls through to totals
        pass
    if not tokens:
        acc = usage_store.session_totals(session_id) or _USAGE_BY_SESSION.get(session_id)
        if acc:
            tokens = (
                (acc.get("input_tokens") or 0)
                + (acc.get("output_tokens") or 0)
                + (acc.get("cache_read_tokens") or 0)
                + (acc.get("cache_creation_tokens") or 0)
            )
    return {
        "context": {
            "tokens": tokens,
            "window": window,
            "percent": round(tokens * 100 / window, 1) if window > 0 else 0,
        },
        "rateLimits": [],
    }


async def _op_messages(args: dict, session_id: str) -> list[dict]:
    history = await _rebuild_history(session_id)
    out = [c for c in (to_canonical(m) for m in history) if c is not None]
    return out


async def _op_turns(args: dict, session_id: str) -> int:
    """Number of user prompts in the persisted history. Steer injections and
    their `[turn context]` companions are engine plumbing, not prompts."""
    from ..server_shared import STEER_CONTEXT_KEY

    history = await _rebuild_history(session_id)
    turns = 0
    for m in history:
        if type(m).__name__ != "HumanMessage":
            continue
        kw = getattr(m, "additional_kwargs", None) or {}
        if kw.get(STEER_CONTEXT_KEY) or kw.get("ginno_steer") or kw.get("ginno_subagent_result"):
            continue  # steered messages / their context body / scheduler wakes
        turns += 1
    return turns


_SESSION_OPS = {
    "id": _op_id,
    "cwd": _op_cwd,
    "root": _op_root,
    "model": _op_model,
    "version": _op_version,
    "usage": _op_usage,
    "turns": _op_turns,
    "messages": _op_messages,
}


# ---- the op table (ns="command", design §5.4 P1) -------------------------------


async def _op_command_register(args: dict, session_id: str, mod: str) -> dict:
    """``$.command.register {name, description?}`` → the slash-command registry
    (commands/mod_commands.py). The calling mod rides the call frame; without
    it the command could not be routed back on ``/name``."""
    from ..commands import mod_commands

    try:
        entry = mod_commands.register(
            name=str(args.get("name") or ""),
            mod=mod,
            description=str(args.get("description") or ""),
        )
    except ValueError as e:
        raise OpError("invalid-argument", str(e)) from None
    log.info("mods command.register name=%s mod=%s", entry["name"], entry["mod"])
    return {"ok": True, **entry}


async def _op_command_list(args: dict, session_id: str, mod: str) -> dict:
    from ..commands import mod_commands

    return {"commands": mod_commands.list_commands()}


_COMMAND_OPS = {
    "register": _op_command_register,
    "list": _op_command_list,
}


async def handle(ns: str, method: str, args: dict, session_id: str, mod: str = "") -> Any:
    """Entry the ModChannel's call server routes to. Scope: the session read
    family plus the command registry (P1, design §5.4) — everything else
    answers no-implementation."""
    if ns == "command":
        op = _COMMAND_OPS.get(method)
        if op is None:
            raise OpError("no-implementation", f"no implementation for command.{method}")
        return await op(args if isinstance(args, dict) else {}, session_id, mod)
    if ns != "session":
        raise OpError("no-implementation", f"no implementation for {ns}.{method}")
    op = _SESSION_OPS.get(method)
    if op is None:
        raise OpError("no-implementation", f"no implementation for session.{method}")
    if not session_id:
        raise OpError("not-found", "session context missing on the call frame")
    return await op(args if isinstance(args, dict) else {}, session_id)
