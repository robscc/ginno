"""History compaction — local summarization (plan E3 + E4).

Ginno re-sends the full message history on every model call and (pre-E3)
never trimmed it, so long sessions grew without bound. This module ports the
"local" branch of Codex's compaction ladder:

* Trigger: at turn entry, estimated history tokens exceed
  ``settings.context.compact_threshold_tokens`` (default 500k).
* Split: at a user-turn boundary, keeping the most recent
  ``compact_keep_turns`` user turns verbatim.
* Summarize the prefix with the session's own model (no tools bound).
* Rewrite the thread state via ``graph.update_state``: delete all old
  messages, append ``[conversation summary]`` + fresh copies of the kept
  tail. Correct order = summary BEFORE the kept turns, hence the full
  delete-then-re-add (add_messages appends new ids in given order).
* E4: after compaction the caller re-injects the current WorldState so the
  compressed history can't lose the world facts (date, role, permissions).

Only runs when no interrupt is pending (``state.next`` empty) — never
disturb a turn paused at a permission prompt.
"""

from __future__ import annotations

import uuid
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)

from .tokens import estimate_messages_tokens
from .world_state import (
    SUMMARY_MSG_PREFIX,
    render_reinjection,
)

_SUMMARY_SYSTEM = (
    "You are a conversation summarizer. Condense the transcript into a dense, "
    "faithful summary the assistant can continue working from: user goals, "
    "decisions, file paths, tool results that still matter, open questions. "
    "Keep concrete values (names, numbers, paths) — drop small talk. Answer in "
    "the transcript's primary language. No preamble, just the summary."
)


def _msg_line(m: BaseMessage) -> str:
    role = getattr(m, "type", "msg")
    content = getattr(m, "content", "")
    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                t = b.get("text") or ""
                if t:
                    parts.append(t)
            elif isinstance(b, str):
                parts.append(b)
        content = "\n".join(parts)
    text = str(content or "").strip()
    if len(text) > 1500:
        text = text[:1500] + "…"
    tcs = getattr(m, "tool_calls", None)
    if tcs:
        calls = ", ".join(tc.get("name", "?") for tc in tcs if isinstance(tc, dict))
        text = (text + f"\n[调用工具: {calls}]").strip()
    return f"{role}: {text}"


def find_split_index(messages: list[BaseMessage], keep_turns: int) -> int:
    """Index of the HumanMessage that starts the kept tail.

    Everything before the index gets summarized; the N-th-from-last user turn
    and everything after it stays verbatim. Returns 0 when there is nothing
    worth compacting (fewer than keep_turns+1 user turns).
    """
    human_idx = [i for i, m in enumerate(messages) if isinstance(m, HumanMessage)]
    if len(human_idx) <= keep_turns:
        return 0
    return human_idx[-keep_turns]


def _copy_with_new_id(m: BaseMessage) -> BaseMessage:
    new_id = f"keep_{uuid.uuid4().hex[:10]}"
    if isinstance(m, HumanMessage):
        return HumanMessage(content=m.content, id=new_id)
    if isinstance(m, ToolMessage):
        return ToolMessage(
            content=m.content,
            tool_call_id=getattr(m, "tool_call_id", None),
            name=getattr(m, "name", None),
            id=new_id,
        )
    if isinstance(m, AIMessage):
        return AIMessage(
            content=m.content,
            tool_calls=list(getattr(m, "tool_calls", None) or []),
            id=new_id,
        )
    return m


async def maybe_compact_history(
    session: dict[str, Any],
    config: dict,
    ctx_factory=None,
) -> dict | None:
    """Check threshold and compact. Returns stats dict or None.

    ``session`` is the server's in-memory session dict (needs "graph",
    "model", "project_slug", "session_id"). ``ctx_factory`` builds the
    SessionCtx for the E4 world re-injection text.
    """
    from .world_state import context_settings

    settings = context_settings()
    if not settings.get("compaction_enabled", True):
        return None

    graph = session["graph"]
    state = await graph.aget_state(config)
    if state is None or getattr(state, "next", None):
        return None  # nothing stored, or paused at an interrupt — leave alone
    messages = list((state.values or {}).get("messages", []))
    threshold = int(settings.get("compact_threshold_tokens", 500000))
    if estimate_messages_tokens(messages) < threshold:
        return None

    keep_turns = max(1, int(settings.get("compact_keep_turns", 3)))
    split = find_split_index(messages, keep_turns)
    if split <= 0:
        return None
    old, keep = messages[:split], messages[split:]

    model = session.get("model")
    if model is None:
        return None
    transcript = "\n".join(_msg_line(m) for m in old)
    summary_resp = await model.ainvoke(
        [
            _summary_system_message(),
            HumanMessage(content=f"请总结以下对话记录:\n\n{transcript}"),
        ]
    )
    summary = str(getattr(summary_resp, "content", "") or "").strip()
    if not summary:
        return None

    new_messages: list[BaseMessage] = [
        RemoveMessage(id=m.id) for m in messages if getattr(m, "id", None)
    ]
    summary_msg = HumanMessage(
        content=f"{SUMMARY_MSG_PREFIX}\n以下是此前对话的摘要（原始消息已被压缩）：\n\n{summary}",
        id=f"summary_{uuid.uuid4().hex[:10]}",
    )
    new_messages.append(summary_msg)
    new_messages.extend(_copy_with_new_id(m) for m in keep)
    await graph.aupdate_state(config, {"messages": new_messages}, as_node="agent")

    # E4 — re-assert the world after compression.
    reinject_text = None
    if ctx_factory is not None:
        reinject_text = render_reinjection(ctx_factory())

    return {
        "compacted_messages": len(old),
        "kept_messages": len(keep),
        "summary_chars": len(summary),
        "reinject": reinject_text,
    }


def _summary_system_message():
    from langchain_core.messages import SystemMessage

    return SystemMessage(content=_SUMMARY_SYSTEM)
