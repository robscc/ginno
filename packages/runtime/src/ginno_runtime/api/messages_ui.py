"""Message → UI block conversion (chat history rendering).

Turns persisted LangChain messages into the chat UI's {role, blocks} shape;
shared by the history endpoint (api/sessions.py) and available to the
streaming layer for live rendering helpers.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from .. import workflows as wf_store
from ..goals.templates import context_row_text as goal_context_row
from ..tools.artifact_tools import ARTIFACT_TOOL_NAMES
from ..tools.render_tools import RENDER_TOOL_NAMES
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
    the UI tool bubble. Image parts become an ``[image]`` marker."""
    if isinstance(content, str):
        return content
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
        return "\n".join(p for p in parts if p)
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False, default=str)


# Live WS tool outputs are capped to keep frames small; the history endpoint
# returns the full untruncated result, so expanding a bubble after reload can
# show more than what streamed live.
TOOL_OUTPUT_WS_LIMIT = 4000


def _truncate_for_ws(text: str) -> str:
    if len(text) <= TOOL_OUTPUT_WS_LIMIT:
        return text
    return text[:TOOL_OUTPUT_WS_LIMIT] + f"\n…（已截断，完整 {len(text)} 字符）"


def _ai_content_blocks(content: Any) -> list[dict]:
    """AIMessage.content (str or list of provider blocks) -> UI text/thinking/image blocks."""
    blocks: list[dict] = []
    if isinstance(content, str):
        if content:
            blocks.append({"kind": "text", "text": content})
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
                        blocks.append({"kind": "text", "text": t})
                elif bt in ("image", "image_url"):
                    url = _image_block_url(b)
                    if url:
                        blocks.append({"kind": "image", "url": url})
            elif isinstance(b, str) and b:
                blocks.append({"kind": "text", "text": b})
    return blocks


def _run_id_in(text: str) -> str | None:
    m = re.search(r"run_id=([0-9a-f]{6,})", text or "")
    return m.group(1) if m else None


def _messages_to_ui(
    messages: list[Any], agent_id: str | None, attached_files: list[dict] | None = None
) -> list[dict]:
    """Convert stored LangChain messages into the chat UI's {role, blocks} shape.

    Consecutive assistant steps between two human messages are merged into a
    single assistant bubble, matching how a live turn renders (one bubble/turn).
    Special tools (render_widget/attach_ref/workflow_*) reproduce their visual
    blocks; ordinary tools fold their ToolMessage result into a tool block.
    """
    results: dict[str, str] = {}
    for m in messages:
        if isinstance(m, ToolMessage):
            results[getattr(m, "tool_call_id", None)] = _tool_content_str(getattr(m, "content", ""))

    ui: list[dict] = []
    acc: list[dict] | None = None
    acc_id: str | None = None

    def flush_assistant() -> None:
        nonlocal acc, acc_id
        if acc:
            ui.append({"id": acc_id, "role": "assistant", "agentId": agent_id, "blocks": acc})
        acc = None
        acc_id = None

    for m in messages:
        if isinstance(m, HumanMessage):
            content_raw = getattr(m, "content", "")
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
            blocks = _content_ui_blocks(content_raw)
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
            step = list(_ai_content_blocks(getattr(m, "content", "")))
            rk = (getattr(m, "additional_kwargs", None) or {}).get("reasoning_content")
            if rk:
                step.insert(0, {"kind": "thinking", "text": rk})
            for tc in getattr(m, "tool_calls", None) or []:
                nm = tc.get("name")
                args = tc.get("args") or {}
                tid = tc.get("id")
                res = results.get(tid, "")
                if nm == "render_widget":
                    step.append({"kind": "widget", "widgetKind": args.get("kind", "widget"), "data": args.get("data")})
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
                        step.append({"kind": "tool", "id": tid, "name": nm, "content": res, "pending": False})
                elif nm in ARTIFACT_TOOL_NAMES or nm in RENDER_TOOL_NAMES:
                    pass  # silent / already handled above
                else:
                    step.append({"kind": "tool", "id": tid, "name": nm, "content": res, "pending": False})
            acc.extend(step)
        # ToolMessage: folded into the tool blocks above
    flush_assistant()
    return ui
