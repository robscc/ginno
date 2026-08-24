"""LLM session title — a subject summary replacing the truncated first message.

``_touch_session_title`` (api/stream.py) sets the usual 40-char truncated
placeholder from the first user message and additionally arms
``title_llm_pending`` plus the seed (that first message) on the session meta.
When the first turn completes without an interrupt, ``_stream_graph`` stashes
the assistant reply as ``title_assistant`` and spawns :func:`spawn_title_gen`:
one auxiliary call with the session's own model produces a short subject
title and replaces the placeholder through the same ``session_title`` WS
event the truncated title uses. Any failure keeps the truncated placeholder
and retries on the next cleanly-completed turn; a manual rename clears the
pending flag and always wins. Un-metered, like the compaction/memory
auxiliary calls.
"""

from __future__ import annotations

import os
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .models import build_model
from .server_shared import _log, _push_session_event, spawn_bg
from .session_meta import _find_meta, _session_meta_patch

_TITLE_SYSTEM = (
    "You are a conversation titler. Read the first exchange of a conversation "
    "and produce ONE short subject title: a noun phrase naming the topic — not "
    "a sentence, not a verbatim quote of the user's words. No quotes, no "
    "trailing punctuation, no preamble. Answer in the conversation's primary "
    "language."
)

# One in-flight title task per session: a second completed turn while the
# first title call is still running must not spawn a duplicate.
_TITLE_INFLIGHT: set[str] = set()


def _content_text(resp: Any) -> str:
    """AIMessage content -> plain text (thinking/reasoning blocks dropped).

    Providers may return a list of blocks (``['', {'thinking': ...},
    {'type': 'text', 'text': ...}]``) instead of a bare string — same
    handling as compaction._msg_line.
    """
    content = getattr(resp, "content", "") or ""
    if isinstance(content, str):
        return content
    parts: list[str] = []
    for b in content:
        if isinstance(b, dict):
            t = b.get("text") or ""
            if t:
                parts.append(t)
        elif isinstance(b, str) and b:
            parts.append(b)
    return "\n".join(parts)


def _sanitize_title(raw: str) -> str:
    """Model output -> usable single-line title; '' means unusable, abort."""
    t = (raw or "").strip()
    if not t:
        return ""
    t = t.splitlines()[0].strip()
    if t and t[0] in "[{":
        return ""  # stringified list/dict structure, not a title
    t = t.lstrip("#-* \t").strip()  # markdown header/bullet leftovers
    while len(t) >= 2:
        pair = t[0] + t[-1]
        if pair in ('""', "''", "``", "“”", "‘’"):
            t = t[1:-1].strip()  # wrapping quotes (straight + curly pairs)
        elif t.startswith("**") and t.endswith("**") and len(t) > 4:
            t = t[2:-2].strip()  # wrapping emphasis
        elif t.startswith("__") and t.endswith("__") and len(t) > 4:
            t = t[2:-2].strip()
        else:
            break
    return " ".join(t.split())[:60].strip()


def spawn_title_gen(session_id: str, turn_id: str) -> None:
    """Fire-and-forget LLM title generation; at most one per session at a time."""
    if session_id in _TITLE_INFLIGHT:
        return
    _TITLE_INFLIGHT.add(session_id)
    spawn_bg(_run_title_gen(session_id, turn_id))


async def _run_title_gen(session_id: str, turn_id: str) -> None:
    try:
        await maybe_llm_title(session_id, turn_id)
    except Exception:
        # The truncated placeholder stays; pending remains armed so the next
        # cleanly-completed turn retries.
        _log.exception("title_llm_failed session=%s", session_id)
    finally:
        _TITLE_INFLIGHT.discard(session_id)


async def maybe_llm_title(session_id: str, turn_id: str) -> None:
    """Generate and apply the LLM subject title for a pending session."""
    found = _find_meta(session_id)
    if not found:
        return  # deleted before the task ran
    meta, slug = found
    if not meta.get("title_llm_pending"):
        return  # manual rename won, or already applied
    seed = (meta.get("title_seed") or "").strip()
    assistant = (meta.get("title_assistant") or "").strip()
    if not seed or not assistant:
        return

    if os.environ.get("GINNO_FAKE_LLM"):
        return  # deterministic-demo seam: keep the truncated placeholder
    try:
        # Fresh client from the session's persisted provider/model — never the
        # live session model object, so the auxiliary call cannot consume
        # scripted test models (and tests without a patched build_model skip).
        model = build_model(meta.get("provider"), meta.get("model"))
    except Exception:
        _log.info("title_llm session=%s status=skipped reason=no_model", session_id)
        return
    resp = await model.ainvoke(
        [
            SystemMessage(content=_TITLE_SYSTEM),
            HumanMessage(content=f"User: {seed}\nAssistant: {assistant[:2000]}"),
        ]
    )
    title = _sanitize_title(_content_text(resp))
    if not title:
        _log.info("title_llm session=%s status=skipped reason=empty", session_id)
        return

    # Re-read right before the sync write with no await between: a manual
    # rename cannot interleave, so this check-at-write is the claim.
    found = _find_meta(session_id)
    if not found or not found[0].get("title_llm_pending"):
        return
    updated = _session_meta_patch(
        slug,
        session_id,
        {
            "title": title,
            "title_llm_pending": False,
            "title_seed": "",  # "" not None: _session_meta_patch skips None
            "title_assistant": "",
        },
    )
    if updated is None:
        return  # deleted mid-flight
    await _push_session_event(session_id, "session_title", {"title": title}, turn_id)
    _log.info("title_llm session=%s status=applied title=%r", session_id, title)
