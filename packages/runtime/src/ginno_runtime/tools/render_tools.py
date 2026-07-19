"""Structured-output tools.

`render_widget` and `attach_ref` are no-op tools whose only effect is
visual: the WS layer intercepts their calls and emits `widget.emit` /
`ref.emit` events (the frontend renders a stat_list card / a reference
chip). They are always allowed for every agent and are NOT shown as
ordinary tool bubbles.
"""

from __future__ import annotations

from langchain_core.tools import tool

RENDER_TOOL_NAMES = {"render_widget", "attach_ref"}


@tool
def render_widget(kind: str, data: dict, summary: str = "") -> str:
    """Render a rich widget card in the chat instead of plain text.

    Use this when a structured view is clearer than prose (status lists,
    metrics, breakdowns). Supported kind:
      - "stat_list": data = {"title": str, "items": [{"label": str,
        "value"?: str, "status"?: "done"|"running"|"pending"|"ok"|"error"}]}
    After rendering, also give a one-line textual summary.
    """
    return summary or f"[rendered widget: {kind}]"


@tool
def attach_ref(kind: str, name: str, ref_id: str = "") -> str:
    """Attach a clickable reference chip below the message.

    kind in {"file", "workflow", "doc", "link"}. name is the display label.
    """
    return f"[attached {kind}: {name}]"
