"""Microcompaction — clear stale tool outputs at turn entry (rung below E3).

Ginno re-sends the full message history on every model call. Between "nothing
happens" and the E3 full-summary compaction at ``compact_threshold_tokens``,
every old tool result keeps occupying every request. This module is the
lighter rung below E3 in the ladder (Codex/Claude-Code style microcompact):

* Trigger: at turn entry, BEFORE E3 measures tokens — always runs (no token
  threshold), so stale outputs are cleared as soon as they become history.
* Selection: ``ToolMessage`` s before the compaction split point, i.e. outside
  the most recent ``compact_keep_turns`` user turns. Outputs that were never
  large (≤ ``microcompact_min_chars``) are cheap to keep and often carry state
  ("ok", todo confirmations) — they are skipped, as are already-cleared ones.
* Rewrite: delete-all + re-add in original order, every message re-added with
  a NEW id (same pattern as E3's ``_copy_with_new_id``). New ids are required
  for TWO reasons: (1) add_messages cancels a RemoveMessage when the same id
  is re-added in the same batch (in-place update, no re-ordering) — only
  new-id re-adds are actually removed-then-appended in the given order;
  (2) FileCheckpointer's delta mode detects history rewrites by message-id
  mismatch only — same-id content edits would be silently lost when the delta
  chain is reconstructed. Cleared outputs are replaced by
  ``CLEARED_TOOL_OUTPUT``.
* No model call — a pure state rewrite, cheap. Tool invocation itself stays
  visible (the AIMessage ``tool_calls`` and the ToolMessage pairing survive);
  only the bulky output body is dropped. The model can re-invoke the tool if
  it needs the content again.

Only runs when no interrupt is pending (``state.next`` empty) — never disturb
a turn paused at a permission prompt (same guard as E3).
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    RemoveMessage,
    ToolMessage,
)

from .compaction import _copy_with_new_id, find_split_index
from .world_state import context_settings

CLEARED_TOOL_OUTPUT = "[old tool output cleared]"

# Prefix match (not equality) so future variants of the marker stay idempotent.
_CLEARED_PREFIX = "[old tool output"


def _is_cleared(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(_CLEARED_PREFIX)


def _clearable_positions(messages: list[BaseMessage], split: int, min_chars: int) -> set[int]:
    """Indices (within ``messages[:split]``) of ToolMessages worth clearing."""
    out: set[int] = set()
    for i, m in enumerate(messages[:split]):
        if not isinstance(m, ToolMessage):
            continue
        content = getattr(m, "content", "")
        if not isinstance(content, str):
            continue
        if len(content) <= min_chars or _is_cleared(content):
            continue
        out.add(i)
    return out


def rewrite_with_cleared(
    messages: list[BaseMessage], positions: set[int]
) -> list[BaseMessage]:
    """Delete-all + re-add in order, ``positions`` replaced by the marker.

    Every message is re-added with a NEW id (see module docstring for why) —
    so the RemoveMessage batch actually clears the whole history and the
    re-added batch lands in the given order. Callers must have verified every
    message carries an id.
    """
    new_messages: list[BaseMessage] = [RemoveMessage(id=m.id) for m in messages]
    for i, m in enumerate(messages):
        if i in positions:
            new_messages.append(
                ToolMessage(
                    content=CLEARED_TOOL_OUTPUT,
                    tool_call_id=getattr(m, "tool_call_id", None),
                    name=getattr(m, "name", None),
                    status=getattr(m, "status", None),
                    id=f"mc_{uuid.uuid4().hex[:10]}",
                )
            )
        else:
            new_messages.append(_copy_with_new_id(m))
    return new_messages


async def maybe_microcompact_history(
    session: dict[str, Any],
    config: dict,
) -> dict | None:
    """Clear stale tool outputs. Returns stats dict or None.

    ``session`` is the server's in-memory session dict (needs "graph").
    Stats: ``cleared_tool_outputs`` (count) and ``chars_freed``.
    """
    settings = context_settings()
    if not settings.get("microcompact_enabled", True):
        return None

    graph = session["graph"]
    state = await graph.aget_state(config)
    if state is None or getattr(state, "next", None):
        return None  # nothing stored, or paused at an interrupt — leave alone
    messages = list((state.values or {}).get("messages", []))
    if any(not getattr(m, "id", None) for m in messages):
        return None  # id-less messages can't be safely delete-rewritten

    keep_turns = max(1, int(settings.get("compact_keep_turns", 3)))
    split = find_split_index(messages, keep_turns)
    if split <= 0:
        return None
    min_chars = int(settings.get("microcompact_min_chars", 500))
    positions = _clearable_positions(messages, split, min_chars)
    if not positions:
        return None

    chars_freed = sum(
        len(str(messages[i].content)) - len(CLEARED_TOOL_OUTPUT) for i in positions
    )
    await graph.aupdate_state(
        config,
        {"messages": rewrite_with_cleared(messages, positions)},
        as_node="agent",
    )
    return {
        "cleared_tool_outputs": len(positions),
        "chars_freed": max(0, chars_freed),
    }
