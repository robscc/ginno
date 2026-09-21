"""Message → UI block conversion (chat history rendering).

Turns persisted LangChain messages into the chat UI's {role, blocks} shape;
shared by the history endpoint (api/sessions.py) and available to the
streaming layer for live rendering helpers.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .. import workflows as wf_store
from ..goals.templates import context_row_text as goal_context_row
from ..knowledge.citations import parse_citation_block, strip_citation_block
from ..tools.artifact_tools import ARTIFACT_TOOL_NAMES
from ..tools.render_tools import RENDER_TOOL_NAMES, widget_event
from ..tools.workflow_tools import RUN_CACHE, WORKFLOW_TOOL_NAMES
from ..world_state import (
    ALL_CONTEXT_PREFIXES,
    GOAL_CONTEXT_PREFIX,
    TURN_CONTEXT_PREFIX,
    UPDATE_MSG_PREFIX,
)

# Bullets a world-state update message can start with when it was checkpointed
# by a build that dropped the machine prefix — healed into context rows too.
LEGACY_WS_UPDATE_MARKERS = (
    "- 你在当前角色下的可用工具数量变化",
    "- MCP 工具已更新",
    "- Skills 已更新",
    "Skills 已更新",
)

# Slash-skill turns: commands/resolver.substitute_skill replaces the leading
# ``/name`` with the SKILL.md body wrapped in ``<skill name="...">`` (model
# scaffolding, see wrap_skill_body). The persisted HumanMessage carries it,
# but the user bubble must show what the user actually typed — the SKILL.md
# injection is prompt plumbing, not conversation (same contract as the
# TURN_CONTEXT hide and the citation-block strip).
_SKILL_WRAP_RE = re.compile(
    r'^\s*<skill name="([^"]+)">.*?</skill>\s*(.*)$', re.DOTALL
)
_SKILL_REQUEST_RE = re.compile(r"User request:\s*(.*)$", re.DOTALL)


def parse_skill_wrap(text: Any) -> tuple[str, str] | None:
    """``<skill name="X">body</skill> …`` → ``(name, user_request)``.

    Returns ``None`` for ordinary text. ``user_request`` is the
    ``User request:`` tail that wrap_skill_body appends (empty string when the
    skill was invoked bare, e.g. just ``/todoist``).
    """
    if not isinstance(text, str):
        return None
    m = _SKILL_WRAP_RE.match(text)
    if not m:
        return None
    tail = m.group(2) or ""
    rm = _SKILL_REQUEST_RE.search(tail)
    return m.group(1), (rm.group(1).strip() if rm else "")


def skill_display_text(text: str) -> str | None:
    """Skill-wrapped user message → the user-facing one-liner ``/name request``
    (``None`` when *text* is not skill-wrapped). Feeds titles/seeds so the
    sidebar never shows the raw SKILL.md injection."""
    parsed = parse_skill_wrap(text)
    if not parsed:
        return None
    name, req = parsed
    return f"/{name} {req}" if req else f"/{name}"


def _skill_block(parsed: tuple[str, str]) -> dict:
    name, req = parsed
    blk: dict = {"kind": "skill", "name": name}
    if req:
        blk["text"] = req
    return blk


def _human_ui_blocks(content: Any) -> list[dict]:
    """HumanMessage content → UI blocks; a skill-wrapped slash-skill turn folds
    into a compact ``skill`` block. Handles both plain-text and multimodal
    content (image + skill text part)."""
    if isinstance(content, str):
        parsed = parse_skill_wrap(content)
        if parsed:
            return [_skill_block(parsed)]
        return _content_ui_blocks(content)
    if isinstance(content, list):
        blocks: list[dict] = []
        folded = False
        for item in content:
            text = (
                item.get("text")
                if isinstance(item, dict) and item.get("type") == "text"
                else item if isinstance(item, str) else None
            )
            if not folded and isinstance(text, str):
                parsed = parse_skill_wrap(text)
                if parsed:
                    blocks.append(_skill_block(parsed))
                    folded = True
                    continue
            if isinstance(item, (dict, str)):
                blocks.extend(_content_ui_blocks([item]))
        return blocks
    return _content_ui_blocks(content)


def _image_block_url(b: dict) -> str | None:
    """Normalize a provider image block (OpenAI ``image_url`` / Anthropic
    ``image``) to a displayable URL (data URL for base64 sources)."""
    if b.get("type") == "image_url":
        iu = b.get("image_url") or {}
        u = iu.get("url") if isinstance(iu, dict) else None
        return u or None
    src = b.get("source") or {}
    if isinstance(src, dict):
        if src.get("type") == "url":
            return src.get("url") or None
        if src.get("data"):
            return f"data:{src.get('media_type') or 'image/png'};base64,{src['data']}"
    return None


def _content_ui_blocks(content: Any) -> list[dict]:
    """Message content (str or multimodal list) -> UI text/image blocks."""
    blocks: list[dict] = []
    if isinstance(content, str):
        if content.strip():
            blocks.append({"kind": "text", "text": content})
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, str):
                if b.strip():
                    blocks.append({"kind": "text", "text": b})
            elif isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    t = b.get("text") or ""
                    if t.strip():
                        blocks.append({"kind": "text", "text": t})
                elif bt in ("image", "image_url"):
                    url = _image_block_url(b)
                    if url:
                        blocks.append({"kind": "image", "url": url})
    return blocks


def _tool_content_str(content: Any) -> str:
    """ToolMessage.content (str or list of provider blocks) -> plain text for
    the UI tool bubble. Image parts become an ``[image]`` marker. The
    code-generated-image trailer (bash) is stripped — it is machine metadata
    rendered as real image blocks instead (see _messages_to_ui)."""
    from ..files.images import strip_images_marker

    if isinstance(content, str):
        return strip_images_marker(content)
    if isinstance(content, list):
        parts: list[str] = []
        for b in content:
            if isinstance(b, str):
                parts.append(b)
            elif isinstance(b, dict):
                bt = b.get("type")
                if bt == "text":
                    parts.append(b.get("text") or "")
                elif bt in ("image", "image_url"):
                    parts.append("[image]")
                else:
                    parts.append(json.dumps(b, ensure_ascii=False, default=str))
        return strip_images_marker("\n".join(p for p in parts if p))
    if content is None:
        return ""
    return strip_images_marker(json.dumps(content, ensure_ascii=False, default=str))


# Live WS tool outputs are capped to keep frames small; the history endpoint
# returns the full untruncated result, so expanding a bubble after reload can
# show more than what streamed live.
TOOL_OUTPUT_WS_LIMIT = 4000


def _truncate_for_ws(text: str) -> str:
    if len(text) <= TOOL_OUTPUT_WS_LIMIT:
        return text
    return text[:TOOL_OUTPUT_WS_LIMIT] + f"\n…（已截断，完整 {len(text)} 字符）"


# Keys most likely to carry the "headline" argument of a tool call, in display
# priority order. ask_user → question (the brief tool-bubble preview before
# the card lands reads the question text); bash → command; file tools → path;
# search/fetch → query/url.
_ARGS_PREVIEW_KEYS = ("question", "command", "path", "pattern", "url", "query", "content")
_ARGS_PREVIEW_CAP = 500


def _tool_args_preview(name: str, args: dict) -> str:
    """One-line preview of a tool call's args for the tool bubble.

    Lets the user see WHAT a tool is doing (e.g. the bash command) rather than
    just the generic label ("执行命令中…"). Returns the most informative string
    argument, collapsed to one line and truncated to a cap so huge payloads
    (e.g. write_file content) never ride the WebSocket / history payload.
    """
    if not isinstance(args, dict):
        return ""
    for key in _ARGS_PREVIEW_KEYS:
        v = args.get(key)
        if isinstance(v, str) and v.strip():
            break
    else:
        v = next((x for x in args.values() if isinstance(x, str) and x.strip()), "")
    text = str(v).strip()
    if not text:
        return ""
    text = " ".join(text.split())  # collapse newlines → keep it one line
    return text if len(text) <= _ARGS_PREVIEW_CAP else text[: _ARGS_PREVIEW_CAP - 1] + "…"


# web_search tool output lines: "[s1] Title — host\n    https://url"
_WEB_RESULT_RE = re.compile(r"\[(s\d+)\][^\n]*\n\s+(https?://\S+)")


def _web_ref_map(tool_results: dict) -> dict[str, str]:
    """sN → URL, reconstructed from persisted web_search outputs (the citation
    contract's `web|sN` ids are meaningless without the tool result that minted
    them; history replay resolves them here so the 来源 card is clickable)."""
    ref_map: dict[str, str] = {}
    for content in tool_results.values():
        if not content or "[s" not in content:
            continue
        for m in _WEB_RESULT_RE.finditer(content):
            ref_map[m.group(1).lower()] = m.group(2)
    return ref_map


def _resolve_source_items(items: list[dict], ref_map: dict[str, str]) -> list[dict]:
    if not ref_map:
        return items
    out = []
    for it in items:
        ref = it.get("ref") or ""
        if it.get("kind") == "web" and re.fullmatch(r"s\d+", ref, re.IGNORECASE):
            url = ref_map.get(ref.lower())
            if url:
                it = {**it, "ref": url}
        out.append(it)
    return out


def _text_with_citations(t: str, blocks: list[dict]) -> None:
    """Append a text block for *t*, folding a trailing ``<ginno_citations>``
    block into a ``sources`` block (citations-design.md §5.6 history replay).
    The raw block is machine metadata — never shown as prose, so it is
    stripped UNCONDITIONALLY: an empty block (or one whose entries all fail
    validation) still parses to zero entries but must not leak into display
    text (2026-08-21: turn b1463216 rendered a raw empty block)."""
    entries = parse_citation_block(t)
    t = strip_citation_block(t)
    if t.strip():
        blocks.append({"kind": "text", "text": t})
    if entries:
        # sources render under the prose (the block sits at the reply's end)
        blocks.append(
            {
                "kind": "sources",
                "items": [
                    {"kind": e.get("kind"), "ref": e.get("ref"), "note": e.get("note") or ""}
                    for e in entries
                ],
            }
        )


def _ai_content_blocks(content: Any) -> list[dict]:
    """AIMessage.content (str or list of provider blocks) -> UI text/thinking/image blocks."""
    blocks: list[dict] = []
    if isinstance(content, str):
        if content:
            _text_with_citations(content, blocks)
    elif isinstance(content, list):
        for b in content:
            if isinstance(b, dict):
                bt = b.get("type")
                if bt == "thinking":
                    t = b.get("thinking") or b.get("text") or ""
                    if t:
                        blocks.append({"kind": "thinking", "text": t})
                elif bt == "text":
                    t = b.get("text") or ""
                    if t:
                        _text_with_citations(t, blocks)
                elif bt in ("image", "image_url"):
                    url = _image_block_url(b)
                    if url:
                        blocks.append({"kind": "image", "url": url})
            elif isinstance(b, str) and b:
                _text_with_citations(b, blocks)
    return blocks


def _run_id_in(text: str) -> str | None:
    m = re.search(r"run_id=([0-9a-f]{6,})", text or "")
    return m.group(1) if m else None


def _json_or_none(text: str) -> dict | None:
    """Tolerant JSON-object parse of a tool result (None when it is not a JSON
    object — e.g. the literal ``(interrupted)`` a stopped turn writes)."""
    try:
        v = json.loads(text)
    except (TypeError, ValueError):
        return None
    return v if isinstance(v, dict) else None


def _ui_options(raw: object) -> list[str]:
    """Coerce a tool-call ``options`` arg into card labels.

    Models JSON-ENCODE array arguments — observed live 2026-09-20 (turn
    aa49c475) as ``'["装进仓库", "装进全局"]'``. Iterating that string renders
    ONE OPTION PER CHARACTER, so never iterate a raw value here."""
    from ..tools.ask_tools import normalize_options

    return normalize_options(raw)


def _question_block(tid: str | None, args: dict, result: str) -> dict | None:
    """ask_user tool call → UI question block, or None if it is not a card.

    No side store is needed: the checkpoint carries the card fields in the
    tool-call args and the user's choice in the tool result — an empty result
    means still parked (pending), ``(interrupted)`` means stopped while parked
    (skipped), otherwise the result is the tool's JSON receipt.

    Returns None for a call that NEVER SHOWED A CARD: an input-validation
    failure (the model stringified ``options``) or an ``[error]`` refusal (ask
    budget spent / unattended turn). Those replay as ordinary tool bubbles,
    matching what the live stream showed — rendering them as cards put two
    junk cards (one per character!) into a real transcript.
    """
    res = (result or "").strip()
    receipt = None
    if res and res != "(interrupted)":
        receipt = _json_or_none(res)
        if not isinstance(receipt, dict) or "ok" not in receipt:
            return None
    blk: dict = {
        "kind": "question",
        "question": str(args.get("question") or ""),
        "options": _ui_options(args.get("options")),
        "allowFreeText": bool(args.get("allow_free_text", True)),
        "status": "pending",
    }
    if tid:
        blk["id"] = tid
    header = str(args.get("header") or "").strip()
    if header:
        blk["header"] = header
    if not res:
        return blk  # still parked
    if receipt is None:
        # Stopped mid-park: never leave the card interactive on replay — a
        # pending block reads as a live ask.
        blk["status"] = "skipped"
        return blk
    if receipt.get("skipped") or receipt.get("source") == "skipped":
        blk["status"] = "skipped"
        return blk
    blk["status"] = "answered"
    blk["answer"] = str(receipt.get("answer") or receipt.get("free_text") or "")
    idx = receipt.get("option_index")
    blk["optionIndex"] = idx if isinstance(idx, int) else None
    return blk


def _gen_image_blocks(
    paths_list: list[str], slug: str | None, session_id: str | None, seen: set[str]
) -> list[dict]:
    """Resolve code-generated image paths (from the bash marker / the
    ``ginno_images`` anchor) into UI image blocks: ``{kind:"image", fileId,
    name, mtime}``. The frontend builds the actual URL from ``fileId`` via its
    BASE-aware download helper (+ ``?t=mtime`` cache-busting).

    Self-heals: a path not yet in the file ledger (e.g. the live emit was lost
    to a restart) is registered here so replay still resolves it. ``seen``
    de-duplicates by file id across the merged bubble's sources.
    """
    from .. import artifacts as art_store
    from .. import files as files_mod

    blocks: list[dict] = []
    if not slug or not paths_list:
        return blocks
    reg = files_mod.get_registry(slug)
    for p in paths_list:
        pp = Path(p).expanduser()
        if not pp.is_file():
            continue
        norm = files_mod.norm_path(str(pp))
        entry = reg.find_by_path(norm)
        if entry is None:
            art = art_store.add_artifact(slug, "image", pp.name, norm, session_id or "")
            entry = reg.register(
                pp.name, str(pp), kind="image", session_id=session_id or "",
                artifact_id=art.get("id"),
            )
        fid = entry.get("id")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        blocks.append(
            {
                "kind": "image",
                "fileId": fid,
                "name": entry.get("name", pp.name),
                "mtime": int(entry.get("mtime") or 0),
            }
        )
    return blocks


def _messages_to_ui(
    messages: list[Any],
    agent_id: str | None,
    attached_files: list[dict] | None = None,
    project_slug: str | None = None,
    session_id: str | None = None,
) -> list[dict]:
    """Convert stored LangChain messages into the chat UI's {role, blocks} shape.

    Consecutive assistant steps between two human messages are merged into a
    single assistant bubble, matching how a live turn renders (one bubble/turn).
    Special tools (render_widget/attach_ref/workflow_*) reproduce their visual
    blocks; ordinary tools fold their ToolMessage result into a tool block.

    ``project_slug``/``session_id`` let code-generated images (bash marker →
    ``ginno_images`` anchor) resolve to file-ledger ids for inline rendering.
    """
    from ..files.images import parse_images_marker

    results: dict[str, str] = {}
    raw_results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            raw = getattr(m, "content", "")
            raw_results[getattr(m, "tool_call_id", None)] = raw if isinstance(raw, str) else ""
            results[getattr(m, "tool_call_id", None)] = _tool_content_str(raw)
    # Citation ids (web|sN) resolve against the web_search outputs of the SAME
    # transcript — build the map once, apply to every sources block below.
    ref_map = _web_ref_map(results)

    ui: list[dict] = []
    acc: list[dict] | None = None
    acc_id: str | None = None
    # Real per-bubble attribution read from the agent_id tag that agent_node
    # writes into AIMessage.additional_kwargs (graph.py). A merged bubble spans
    # exactly one turn, so the first tagged message wins; the session-level
    # ``agent_id`` argument stays as the fallback for old, untagged sessions.
    acc_agent: str | None = None
    # De-dupe generated-image blocks within a merged bubble (they can arrive via
    # the ginno_images anchor on several AIMessages and/or the bash tool marker).
    # Blocks accumulate separately and are appended at the bubble's end so the
    # pictures render after the turn's prose/tool blocks regardless of which
    # step produced them.
    acc_imgs: set[str] = set()
    acc_img_blocks: list[dict] = []
    # Steer bands awaiting an assistant step to attach to (see the steered
    # HumanMessage branch below): a band that lands before the turn's first
    # AIMessage would otherwise have to render as a standalone row, and the live
    # view (which inserts the band into the bubble it is already streaming into)
    # would then diverge from the replay.
    pending_bands: list[dict] = []

    def flush_assistant() -> None:
        nonlocal acc, acc_id, acc_agent, acc_imgs, acc_img_blocks
        if acc:
            if acc_img_blocks:
                acc.extend(acc_img_blocks)
            ui.append({"id": acc_id, "role": "assistant", "agentId": acc_agent or agent_id, "blocks": acc})
        acc = None
        acc_id = None
        acc_agent = None
        acc_imgs = set()
        acc_img_blocks = []

    for m in messages:
        if isinstance(m, HumanMessage):
            content_raw = getattr(m, "content", "")
            # Mid-turn steered message (docs/steering-design.md §3.5): a REAL user
            # message — never folded into a context row — but it must not break
            # the assistant bubble either. One turn is ONE bubble, and the
            # injection point renders as a full-width band inside it (§4.2/§4.3),
            # so the band joins the open accumulator instead of flushing it.
            steer_kw = (getattr(m, "additional_kwargs", None) or {}).get("ginno_steer")
            if steer_kw:
                band = {
                    "kind": "steer",
                    "text": content_raw if isinstance(content_raw, str) else "",
                    "steerId": steer_kw.get("steer_id"),
                    "injectedAt": steer_kw.get("injected_at") or 0,
                }
                if acc is not None:
                    acc.append(band)
                else:
                    pending_bands.append(band)
                continue
            # WorldState scaffolding messages (plan C2/E3/E4/B1): render the
            # user-facing ones as centered "context" rows (chips in the
            # transcript); hide the per-turn context bundle entirely — it is
            # model scaffolding, not conversation.
            if isinstance(content_raw, str) and (
                content_raw.startswith(ALL_CONTEXT_PREFIXES)
                or content_raw.startswith(LEGACY_WS_UPDATE_MARKERS)
            ):
                if content_raw.startswith(TURN_CONTEXT_PREFIX):
                    continue
                flush_assistant()
                # Goal steering messages (continuation / objective-updated) fold
                # into a SHORT centered row — the full prompt is model
                # scaffolding, not conversation (goal-design.md §4.3.2).
                if content_raw.startswith(GOAL_CONTEXT_PREFIX):
                    display = goal_context_row(content_raw)
                else:
                    # The update prefix is a machine marker — never show it.
                    display = content_raw
                    if display.startswith(UPDATE_MSG_PREFIX):
                        display = display[len(UPDATE_MSG_PREFIX):].lstrip("\n")
                ui.append(
                    {
                        "id": getattr(m, "id", None),
                        "role": "system",
                        "blocks": [{"kind": "context", "text": display}],
                    }
                )
                continue
            flush_assistant()
            blocks = _human_ui_blocks(content_raw)
            if attached_files and not ui:
                # first user bubble carries the turn's file chips
                file_blocks = [
                    {
                        "kind": "file",
                        "fileId": f.get("id"),
                        "name": f.get("name"),
                        "path": f.get("path"),
                        "fileKind": f.get("kind"),
                    }
                    for f in attached_files
                ]
                blocks = file_blocks + blocks
            if blocks:
                ui.append({"id": getattr(m, "id", None), "role": "user", "blocks": blocks, "turnId": getattr(m, "id", None)})
        elif isinstance(m, AIMessage):
            if acc is None:
                acc = []
                acc_id = getattr(m, "id", None)
                # Steer bands absorbed before this step lead the bubble.
                if pending_bands:
                    acc.extend(pending_bands)
                    pending_bands.clear()
            if acc_agent is None:
                acc_agent = (getattr(m, "additional_kwargs", None) or {}).get("agent_id")
            step = list(_ai_content_blocks(getattr(m, "content", "")))
            # Resolve `web|sN` citation ids to URLs (from web_search outputs)
            # so the 来源 card is clickable on history replay.
            for blk in step:
                if blk.get("kind") == "sources" and blk.get("items"):
                    blk["items"] = _resolve_source_items(blk["items"], ref_map)
            rk = (getattr(m, "additional_kwargs", None) or {}).get("reasoning_content")
            if rk:
                step.insert(0, {"kind": "thinking", "text": rk})
            for tc in getattr(m, "tool_calls", None) or []:
                nm = tc.get("name")
                args = tc.get("args") or {}
                tid = tc.get("id")
                res = results.get(tid, "")
                if nm == "render_widget":
                    ev = widget_event(args, tid)
                    step.append({
                        "kind": "widget",
                        "widgetKind": ev["kind"],
                        "data": ev["data"],
                        "renderId": ev["render_id"],
                    })
                elif nm == "attach_ref":
                    step.append({
                        "kind": "ref",
                        "refKind": args.get("kind", "file"),
                        "name": args.get("name", ""),
                        "refId": args.get("ref_id", ""),
                    })
                elif nm in WORKFLOW_TOOL_NAMES:
                    rid = _run_id_in(res)
                    run = (RUN_CACHE.get(rid) if rid else None) or (wf_store.get_run(rid) if rid else None)
                    if run:
                        step.append({"kind": "workflow", "run": run})
                    else:
                        step.append({
                            "kind": "tool", "id": tid, "name": nm, "content": res,
                            "pending": False, "argsPreview": _tool_args_preview(nm, args),
                        })
                elif nm == "ask_user" and (_qb := _question_block(tid, args, res)):
                    # The question card IS the UI for this call — but only for
                    # a call that actually parked or returned a receipt;
                    # _question_block returns None for an error result, which
                    # falls through to the ordinary tool bubble below.
                    step.append(_qb)
                elif nm in ARTIFACT_TOOL_NAMES or nm in RENDER_TOOL_NAMES:
                    pass  # silent / already handled above
                else:
                    step.append({
                        "kind": "tool", "id": tid, "name": nm, "content": res,
                        "pending": False, "argsPreview": _tool_args_preview(nm, args),
                    })
                    # Fallback path for code-generated images: if the turn ended
                    # before agent_node could lift the bash marker into
                    # additional_kwargs (crash/interrupt), the persisted
                    # ToolMessage still carries it — surface the pictures here.
                    for blk in _gen_image_blocks(
                        parse_images_marker(raw_results.get(tid, "")),
                        project_slug, session_id, acc_imgs,
                    ):
                        acc_img_blocks.append(blk)
            # Durable anchor: agent_node lifts this turn's bash-generated image
            # paths into additional_kwargs["ginno_images"] (survives microcompact,
            # which clears old ToolMessage bodies). Resolve them to image blocks.
            gi = (getattr(m, "additional_kwargs", None) or {}).get("ginno_images")
            if isinstance(gi, list):
                for blk in _gen_image_blocks(gi, project_slug, session_id, acc_imgs):
                    acc_img_blocks.append(blk)
            acc.extend(step)
        # ToolMessage: folded into the tool blocks above
    # A steer band no assistant step ever followed (the transcript ends on it):
    # render it as its own full-width row rather than dropping the user's words.
    for band in pending_bands:
        ui.append({"id": None, "role": "user", "blocks": [band]})
    pending_bands.clear()
    flush_assistant()
    return ui
