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

import logging
import re
import uuid
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    ToolMessage,
)

from .lang import t
from .tokens import estimate_messages_tokens
from .world_state import (
    SUMMARY_MSG_PREFIX,
    render_reinjection,
    summary_lead_in,
)

log = logging.getLogger("ginno.compaction")

# Structured summarization prompt (Claude-Code-style 9-section template with
# Ginno adaptations: machine-marker guard + first-line UI preview contract +
# "Work Completed"/"Context for Continuing Work" tail for the kept-turns
# layout). The bracketed marker list below must stay in sync with
# world_state.ALL_CONTEXT_PREFIXES.
_SUMMARY_SYSTEM = (
    "You are the conversation summarizer for an AI agent. Your output REPLACES "
    "the original messages in the agent's context — everything after your "
    "summary continues the session, so the summary must let the agent keep "
    "working without re-reading anything.\n\n"

    "Output format (strict):\n"
    "- First write a private <analysis> block: walk through the conversation "
    "chronologically and note every user request, decision, file, error and "
    "open thread. This block will be discarded before the summary is used.\n"
    "- Then write the final <summary> block. Its FIRST line must be a single "
    "short overview sentence (it is shown as the UI preview); the numbered "
    "sections follow.\n\n"

    "Verbatim-retention rules (highest priority):\n"
    "- Quote the user's own words precisely: constraints, preferences, "
    "corrections, and anything about safety, permissions, privacy or "
    "credentials must be preserved verbatim (word for word), never "
    "paraphrased.\n"
    "- Keep concrete values: file paths, commands, numbers, names, URLs, "
    "error strings.\n"
    "- 'User messages' means only what the human actually typed. Messages "
    "starting with bracketed machine markers such as [world state update], "
    "[turn context], [conversation summary], [world state re-injection] or "
    "[goal context] are system-injected context, NOT user messages; tool "
    "calls and tool outputs are not user messages either.\n\n"

    "The summary must contain these numbered sections (keep the English "
    "headings, write the content in the conversation's primary language):\n"
    "1. Primary Request and Intent — all explicit requests and intent.\n"
    "2. Key Technical Concepts — technologies/frameworks involved.\n"
    "3. Files and Code Sections — files created/read/modified and why; "
    "include the important snippets, signatures or paths.\n"
    "4. Errors and Fixes — every error encountered and how it was fixed, "
    "including user feedback that triggered the fix.\n"
    "5. Problem Solving — problems solved, investigations and conclusions.\n"
    "6. All User Messages — list ALL actual user messages (excluding the "
    "machine-marker messages above); preserve safety-relevant instructions "
    "verbatim.\n"
    "7. Pending Tasks — everything still open.\n"
    "8. Work Completed — what the just-compacted earlier part accomplished "
    "(recent turns are kept verbatim after the summary).\n"
    "9. Context for Continuing Work — everything needed to seamlessly "
    "continue: current state, decisions that constrain the future, and the "
    "immediate next step with the verbatim quote of the last relevant "
    "request.\n\n"

    "Style: dense and faithful; keep every fact listed above and drop small "
    "talk. No preamble, no closing remarks, no offers to help. Output only "
    "the <analysis> block followed by the <summary> block."
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


def is_steered(m: BaseMessage) -> bool:
    """True for a mid-turn steered message (docs/steering-design.md §3.6).

    Such a message is absorbed INSIDE a running turn, so it does not start one.
    Both compaction and microcompact keep the last ``compact_keep_turns`` user
    turns verbatim, and counting a steer as a turn boundary would drag that
    window forward by one turn per steer — a chatty user would watch their
    older context get compacted sooner than they asked for.
    """
    return bool((getattr(m, "additional_kwargs", None) or {}).get("ginno_steer"))


def find_split_index(messages: list[BaseMessage], keep_turns: int) -> int:
    """Index of the HumanMessage that starts the kept tail.

    Everything before the index gets summarized; the N-th-from-last user turn
    and everything after it stays verbatim. Returns 0 when there is nothing
    worth compacting (fewer than keep_turns+1 user turns).
    """
    human_idx = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, HumanMessage) and not is_steered(m)
    ]
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
            # Preserve additional_kwargs — carries the agent_id attribution
            # tag (graph.py) among provider extras like reasoning_content;
            # dropping it would lose per-turn attribution after compaction.
            additional_kwargs=dict(getattr(m, "additional_kwargs", None) or {}),
            id=new_id,
        )
    return m


async def maybe_compact_history(
    session: dict[str, Any],
    config: dict,
    ctx_factory=None,
    force: bool = False,
) -> dict | None:
    """Check threshold and compact. Returns stats dict or None.

    ``session`` is the server's in-memory session dict (needs "graph",
    "model", "project_slug", "session_id"). ``ctx_factory`` builds the
    SessionCtx for the E4 world re-injection text.

    ``force=True`` is the manual ``/compact`` path: explicit user intent, so
    it bypasses the token threshold AND the ``compaction_enabled`` auto flag.
    The interrupt guard below still applies — never rewrite state of a turn
    parked at a permission prompt.
    """
    from .world_state import context_settings

    settings = context_settings()
    if not force and not settings.get("compaction_enabled", True):
        return None

    graph = session["graph"]
    state = await graph.aget_state(config)
    if state is None or getattr(state, "next", None):
        return None  # nothing stored, or paused at an interrupt — leave alone
    messages = list((state.values or {}).get("messages", []))
    threshold = int(settings.get("compact_threshold_tokens", 500000))
    if not force and estimate_messages_tokens(messages) < threshold:
        return None

    keep_turns = max(1, int(settings.get("compact_keep_turns", 3)))
    split = find_split_index(messages, keep_turns)
    if split <= 0:
        return None
    old, keep = messages[:split], messages[split:]

    model = session.get("model")
    if model is None:
        return None

    # Mods tap (claude-code-mods-design.md §5.3 session.compact, P2): a chain
    # that answers {skip} aborts THIS compaction only — the next turn re-checks
    # the threshold. Raised only once compaction is actually decided (not on
    # every turn's threshold check); the 2s deadline keeps the turn path fast.
    from .mods.events import dispatch_session_compact

    session_id = str(session.get("session_id") or "")
    if await dispatch_session_compact(
        session_id,
        tokens=int(estimate_messages_tokens(messages)),
        threshold=threshold,
        force=force,
        messages=len(messages),
    ):
        log.info("compaction skipped by mod session=%s force=%s", session_id, force)
        return None

    transcript = "\n".join(_msg_line(m) for m in old)
    summary_resp = await model.ainvoke(
        [
            _summary_system_message(),
            HumanMessage(
                content=t(
                    f"Summarize the following conversation:\n\n{transcript}",
                    f"请总结以下对话记录:\n\n{transcript}",
                )
            ),
        ]
    )
    summary = _parse_summary_output(str(getattr(summary_resp, "content", "") or "")).strip()
    if not summary:
        return None

    new_messages: list[BaseMessage] = [
        RemoveMessage(id=m.id) for m in messages if getattr(m, "id", None)
    ]
    summary_msg = HumanMessage(
        content=f"{SUMMARY_MSG_PREFIX}\n{summary_lead_in()}\n\n{summary}",
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


_SUMMARY_TAG_RE = re.compile(r"<summary>\s*(.*?)\s*</summary>", re.DOTALL)
_ANALYSIS_TAG_RE = re.compile(r"<analysis>\s*.*?\s*</analysis>", re.DOTALL)


def _parse_summary_output(raw: str) -> str:
    """Extract the summary body from the model output.

    Priority: ``<summary>...</summary>`` if present; otherwise strip any
    ``<analysis>`` block and use the remainder. Plain text (models that
    ignore the requested format) passes through unchanged.
    """
    if not raw:
        return ""
    m = _SUMMARY_TAG_RE.search(raw)
    if m:
        return m.group(1).strip()
    return _ANALYSIS_TAG_RE.sub("", raw).strip()


def _summary_system_message():
    from langchain_core.messages import SystemMessage

    return SystemMessage(content=_SUMMARY_SYSTEM)
