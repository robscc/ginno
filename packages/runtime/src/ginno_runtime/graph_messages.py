"""Message transforms for the main agent graph.

Split out of ``graph.py`` (structural refactor, no behavior change). Every
name here is re-exported from ``ginno_runtime.graph`` so existing callers and
tests keep importing it from there.
"""

from __future__ import annotations

from xml.sax.saxutils import escape as _xml_escape

from langchain_core.messages import HumanMessage, ToolMessage

from .lang import t
from .server_shared import STEER_CONTEXT_KEY


def text_of_content(content) -> str:
    """Concatenated text of a message ``content`` (str or multimodal list).

    Multimodal content (e.g. a HumanMessage carrying text + image blocks) is a
    list of provider blocks; join the text parts so downstream consumers (wiki
    retrieval, skill detection) keep working for image-first messages.
    Thinking blocks (extended-thinking Anthropic models via the hub) are
    skipped — they are reasoning, not output; stringifying the whole list used
    to corrupt payloads (summarize-from-session parsed ``str(list)`` → garbage).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                if b:
                    parts.append(b)
                continue
            btype = b.get("type") if isinstance(b, dict) else getattr(b, "type", None)
            if btype == "thinking":
                continue  # reasoning, never part of the usable output
            text = b.get("text") if isinstance(b, dict) else getattr(b, "text", None)
            if text:
                parts.append(str(text))
        return "\n".join(parts)
    return ""


# Keep images only on the most recent K user turns; older turns' images are
# replaced by a text placeholder before the LLM call so multi-image sessions
# don't bloat the context (every turn otherwise re-sends ALL prior base64
# images). Module constant so tests can monkeypatch a small value — mirrors the
# CHAT_TIMEOUT_S / CHUNK_TIMEOUT_S / RECENCY_WINDOW_DAYS pattern.
IMAGE_KEEP_TURNS = 2


def _is_image_block(b) -> bool:
    """True for provider image blocks (OpenAI `image_url` / Anthropic `image`)."""
    return isinstance(b, dict) and b.get("type") in ("image_url", "image")


def is_midturn_injected(m) -> bool:
    """True for a HumanMessage that steering absorbed INSIDE a running turn:
    either the steered message itself or the companion carrying its attached
    documents (``server_shared.STEER_CONTEXT_KEY``, the very shape a turn-start
    ``[turn context]`` message has).

    Neither starts a turn, so neither may count as one.
    """
    kwargs = getattr(m, "additional_kwargs", None) or {}
    return bool(kwargs.get("ginno_steer") or kwargs.get(STEER_CONTEXT_KEY))


def strip_old_images(messages, keep_turns: int = IMAGE_KEEP_TURNS):
    """Return a copy of ``messages`` with old turns' images stripped.

    The most recent ``keep_turns`` user turns keep their image blocks; any
    older HumanMessage has its image blocks replaced by a single text
    placeholder noting how many were dropped. Text blocks are preserved.

    Mid-turn injected HumanMessages (see :func:`is_midturn_injected`) are not
    turn boundaries: they are skipped when counting, and the retained window is
    the whole suffix from the N-th-from-last real turn on — so they keep their
    own images too (they are newer than the window's first kept turn).
    Counting them was a silent regression: one turn that absorbed two steered
    messages pushed the window two entries forward and stripped the images of
    the very user message that turn had started with.

    NEVER mutates the input — a fresh list of fresh message objects is returned,
    so the persisted state (checkpointer → UI history / time-travel) keeps full
    image fidelity; only the LLM call sees the trimmed copy.
    """
    human_idx = [
        i
        for i, m in enumerate(messages)
        if isinstance(m, HumanMessage) and not is_midturn_injected(m)
    ]
    if keep_turns > 0 and human_idx:
        start = human_idx[-min(keep_turns, len(human_idx))]
        # Everything from that index on stays verbatim — identical to
        # ``human_idx[-keep_turns:]`` for a history with no injected messages,
        # and correct for one that has them.
        keep = {
            i
            for i, m in enumerate(messages)
            if isinstance(m, HumanMessage) and i >= start
        }
    else:
        keep = set()
    out = []
    for i, m in enumerate(messages):
        content = getattr(m, "content", "")
        if i in keep or not isinstance(content, list) or not any(_is_image_block(b) for b in content):
            out.append(m)
            continue
        n_img = sum(1 for b in content if _is_image_block(b))
        text_blocks = [b for b in content if not _is_image_block(b)]
        placeholder = {
            "type": "text",
            "text": t(f"[{n_img} earlier images omitted]", f"[{n_img} 张历史图片已省略]"),
        }
        new_content = text_blocks + [placeholder] if text_blocks else [placeholder]
        if isinstance(m, HumanMessage):
            out.append(HumanMessage(content=new_content, id=m.id))
        else:
            # e.g. a browser screenshot ToolMessage ([text, image_url] blocks):
            # strip the old image but PRESERVE the message class — rebuilding
            # it as HumanMessage would corrupt the tool-call pairing.
            out.append(m.model_copy(update={"content": new_content}))
    return out


def strip_tool_image_markers(messages):
    """Return a copy with code-generated-image markers removed from ToolMessage
    bodies (send-only; the persisted ToolMessages keep the markers).

    The bash tool appends a ``<!--ginno-images:[...]-->`` trailer so the WS
    layer and history builder can surface generated pictures. The model should
    not see the raw marker — images are display-only — so it is stripped from
    the LLM-bound copy, mirroring ``strip_old_images``' send-only semantics.
    """
    from .files.images import strip_images_marker

    out = []
    changed = False
    for m in messages:
        content = getattr(m, "content", "")
        if (
            isinstance(m, ToolMessage)
            and isinstance(content, str)
            and "<!--ginno-images:" in content
        ):
            out.append(m.model_copy(update={"content": strip_images_marker(content)}))
            changed = True
        else:
            out.append(m)
    return out if changed else messages


def strip_tool_code_markers(messages):
    """Return a copy with code-panel change markers removed from ToolMessage
    bodies (send-only; the persisted ToolMessages keep the markers).

    ``write_file`` / ``edit_file`` append a ``<!--ginno-code:[...]-->`` trailer so
    the WS layer can broadcast ``code.changed`` (code-panel S3, design §4.2-3).
    It is machine metadata: the model must not see its own marker, let alone
    echo it back into a tool result. Same copy-only convention as
    ``strip_tool_image_markers`` above.
    """
    from .files.code_changes import strip_code_marker

    out = []
    changed = False
    for m in messages:
        content = getattr(m, "content", "")
        if (
            isinstance(m, ToolMessage)
            and isinstance(content, str)
            and "<!--ginno-code:" in content
        ):
            out.append(m.model_copy(update={"content": strip_code_marker(content)}))
            changed = True
        else:
            out.append(m)
    return out if changed else messages


def collect_turn_images(messages) -> list[str]:
    """Absolute paths of code-generated images produced in the current turn.

    Scans the ToolMessages of the current turn (everything after the most
    recent HumanMessage) and gathers the paths recorded in bash image markers.
    The agent node lifts these into the AIMessage's ``additional_kwargs`` — a
    durable anchor that survives microcompact (which clears old ToolMessage
    bodies) so the history builder can re-emit the images on replay.
    """
    from .files.images import parse_images_marker

    # The current turn is everything after the last HumanMessage; walk it
    # forward so the paths come out in chronological order.
    start = 0
    for i, m in enumerate(messages):
        if isinstance(m, HumanMessage):
            start = i + 1
    found: list[str] = []
    for m in messages[start:]:
        if not isinstance(m, ToolMessage):
            continue
        content = getattr(m, "content", "")
        if not isinstance(content, str):
            continue
        for p in parse_images_marker(content):
            if p not in found:
                found.append(p)
    return found


def _latest_human_text(messages) -> str:
    """The most recent user message text — used as the wiki retrieval query."""
    for m in reversed(messages):
        if isinstance(m, HumanMessage):
            return text_of_content(getattr(m, "content", ""))
    return ""


# Model-facing wrapper for a mid-turn steered message (docs/steering-design.md
# §3.5). Claude Code puts an equivalent sentence in the message CONTENT, which
# forces its UI to parse the prefix back out; here the persisted HumanMessage
# keeps the user's raw words (the transcript renders them verbatim and the
# history builder matches it by ``ginno_steer.steer_id``) and only this
# model-facing COPY carries the wrapper — the same copy-only convention as
# strip_old_images / strip_tool_image_markers / _mark_cache_tail above.
STEER_HEAD = (
    "The user sent a new message while you were working. Treat it as the newest "
    "instruction and adjust the current work accordingly."
)


def _steer_wrapped_text(text: str) -> str:
    return (
        f"<ginno_steer>\n{STEER_HEAD}\n<message>\n"
        f"{_xml_escape(text)}\n</message>\n</ginno_steer>"
    )


def _wrap_steered_for_model(history: list) -> list:
    """Copy-only: give each steered message the model-facing mid-turn wrapper.

    Two content shapes reach here. A plain steer is a str. A steer the user
    attached images to is a multimodal list (``[{"type": "text", …}, {"type":
    "image_url", …}]``), and there the wrapper may only replace the TEXT block —
    splicing the marker into an image block would corrupt the provider payload,
    and an image-only steer has nothing to wrap at all and is passed through
    untouched.
    """
    out: list = []
    for m in history:
        steer = (getattr(m, "additional_kwargs", None) or {}).get("ginno_steer")
        content = getattr(m, "content", None)
        if not steer:
            out.append(m)
            continue
        if isinstance(content, str):
            out.append(m.model_copy(update={"content": _steer_wrapped_text(content)}))
            continue
        if isinstance(content, list):
            texts: list[str] = []
            rest: list = []
            for b in content:
                if isinstance(b, str):
                    if b:
                        texts.append(b)
                    continue
                if (
                    isinstance(b, dict)
                    and b.get("type") == "text"
                    and (b.get("text") or "").strip()
                ):
                    texts.append(str(b["text"]))
                    continue
                rest.append(b)
            if not texts:
                out.append(m)  # image-only steer: nothing to wrap
                continue
            out.append(
                m.model_copy(
                    update={
                        "content": [
                            {"type": "text", "text": _steer_wrapped_text("\n".join(texts))},
                            *rest,
                        ]
                    }
                )
            )
            continue
        out.append(m)
    return out


def _annotate_turn_tools(history: list, allowed: list) -> list:
    """Copy-only: tell the model which tools are ACTUALLY bound this turn.

    Classifier tool-selection (classifier/middlewares.py) may narrow the
    bound toolset below what the stable system prompt declares. Rewriting
    that declaration per turn would thrash the prompt prefix cache, so the
    per-turn truth rides on the tail HumanMessage instead — which changes
    every turn anyway, so caching loses nothing. Copy semantics match
    _wrap_steered_for_model (persisted state untouched).
    """
    if not history:
        return history
    names = [t.name for t in allowed]
    shown = ", ".join(names[:40]) + (", …" if len(names) > 40 else "")
    note = f"[Bound tools this turn: {shown}. Calls to other tools will fail.]"
    tail = history[-1]
    if not isinstance(tail, HumanMessage):
        return history
    content = tail.content
    if isinstance(content, str):
        return history[:-1] + [tail.model_copy(update={"content": f"{content}\n\n{note}"})]
    if isinstance(content, list):
        return history[:-1] + [
            tail.model_copy(update={"content": [*content, {"type": "text", "text": note}]})
        ]
    return history
