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
  chain is reconstructed. Cleared outputs are replaced by a short marker that
  KEEPS a head excerpt of the original (``microcompact_head_chars``, default
  1200 chars): the E3 summarizer builds its transcript only AFTER this
  rewrite, and its prompt asks for code snippets to survive — a bare
  "[old tool output cleared]" starved it of raw material, structurally
  degrading code-heavy session summaries. The head keeps the beginning of
  the output (where command lines / code usually live) while freeing the
  bulk of the bytes.
* No model call — a pure state rewrite, cheap. Tool invocation itself stays
  visible (the AIMessage ``tool_calls`` and the ToolMessage pairing survive);
  only the bulky output body is replaced by the marker + head excerpt. The
  model can re-invoke the tool if it needs the full content again.

Only runs when no interrupt is pending (``state.next`` empty) — never disturb
a turn paused at a permission prompt (same guard as E3).
"""

from __future__ import annotations

import uuid
from hashlib import sha256
from typing import Any

from langchain_core.messages import (
    BaseMessage,
    RemoveMessage,
    ToolMessage,
)

from . import jsonx
from .compaction import _copy_with_new_id, find_split_index
from .world_state import context_settings

CLEARED_TOOL_OUTPUT = "[old tool output cleared]"  # legacy full-clear marker

# Replacement body for cleared outputs: marker + head excerpt of the original.
# The E3 summarizer sees the transcript only AFTER this rewrite and its prompt
# demands code snippets survive — a bare marker starved it (P1 audit 2026-11).
_CLEARED_HEAD_PREFIX = "[old tool output cleared, head kept]\n"

# Prefix match (not equality) so BOTH marker variants (legacy full clear and
# head-kept) are recognized as already-cleared → idempotent across upgrades.
_CLEARED_PREFIX = "[old tool output"


def _is_cleared(content: Any) -> bool:
    return isinstance(content, str) and content.startswith(_CLEARED_PREFIX)


def cleared_outputs_notice(messages: list[BaseMessage]) -> tuple[int, str | None]:
    """Identify the notice by tool calls, whose IDs survive checkpoint rewrites."""
    calls = [m.tool_call_id for m in messages if isinstance(m, ToolMessage) and _is_cleared(m.content)]
    if not calls:
        return 0, None
    digest = sha256(jsonx.dumps(calls).encode()).hexdigest()
    return len(calls), f"microcompact:{digest}"


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


def _cleared_content(original: str, head_chars: int) -> str:
    """Replacement body: marker + head excerpt, trailing whitespace stripped.

    Stripping the tail matters when the cut lands mid-whitespace — a ragged
    trailing newline block costs tokens and tells the model nothing.
    """
    return f"{_CLEARED_HEAD_PREFIX}{original[:head_chars]}".rstrip()


def rewrite_with_cleared(
    messages: list[BaseMessage], positions: set[int], head_chars: int = 1200
) -> list[BaseMessage]:
    """Delete-all + re-add in order, ``positions`` replaced by marker + head.

    Every message is re-added with a NEW id (see module docstring for why) —
    so the RemoveMessage batch actually clears the whole history and the
    re-added batch lands in the given order. Callers must have verified every
    message carries an id. Cleared positions keep the first ``head_chars``
    of their original content (see ``_cleared_content``).
    """
    new_messages: list[BaseMessage] = [RemoveMessage(id=m.id) for m in messages]
    for i, m in enumerate(messages):
        if i in positions:
            # ``_clearable_positions`` only selects str contents; ``str()`` is
            # a defensive no-op for direct unit-level calls.
            original = m.content if isinstance(m.content, str) else str(m.content)
            new_messages.append(
                ToolMessage(
                    content=_cleared_content(original, head_chars),
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
    Stats include the number cleared, net characters freed after excerpts,
    and the notice ID shared by the stream and history.
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
    head_chars = max(0, int(settings.get("microcompact_head_chars", 1200)))
    positions = _clearable_positions(messages, split, min_chars)
    if not positions:
        return None

    chars_freed = sum(
        len(str(messages[i].content))
        - len(_cleared_content(str(messages[i].content), head_chars))
        for i in positions
    )
    rewritten = rewrite_with_cleared(messages, positions, head_chars)
    _, notice_id = cleared_outputs_notice(rewritten)
    await graph.aupdate_state(config, {"messages": rewritten}, as_node="agent")
    return {
        "cleared_tool_outputs": len(positions),
        "chars_freed": max(0, chars_freed),
        "notice_id": notice_id,
    }
