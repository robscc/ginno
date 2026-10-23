"""Structured-output tools.

`render_widget` and `attach_ref` are no-op tools whose only effect is
visual: the WS layer intercepts their calls and emits `widget.emit` /
`ref.emit` events (the frontend renders a stat_list card / a d3 chart /
a reference chip). They are always allowed for every agent and are NOT
shown as ordinary tool bubbles.
"""

from __future__ import annotations

import re
import uuid

from langchain_core.tools import tool

RENDER_TOOL_NAMES = {"render_widget", "attach_ref"}

# Some models (observed live: deepseek-flash, 2026-10-23 turn d98d236a) do not
# emit attach_ref as a tool call — they write XML-ish tags into the answer:
#   <attach_ref kind="file" name="harness.py" ref_id="/abs/path"/>
# The tags then leak into the bubble as raw text and no chip appears. Parse
# them out of the prose instead of relying on the model's function calling.
_ATTACH_OPEN = "<attach_ref"
_ATTACH_CLOSE = "</attach_ref"
_ATTACH_CLOSE_RE = re.compile(r"</attach_ref\s*>")
_ATTACH_ATTR_RE = re.compile(
    r"""([A-Za-z_][\w-]*)\s*=\s*(?:"([^"]*)"|'([^']*)')""",
)
# A "tag" longer than this is prose that happens to mention the name; treat it
# as literal text rather than swallowing the paragraph looking for a '>'.
_ATTACH_TAG_MAX = 4096


def _tag_end(text: str, start: int) -> int:
    """Index just past the closing ``>`` of the tag starting at ``start``.

    Quote-aware (attribute values may contain ``>``). Returns -1 when the tag
    is unterminated (streaming fragment) or absurdly long.
    """
    limit = min(len(text), start + _ATTACH_TAG_MAX)
    quote = ""
    for i in range(start, limit):
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
        elif ch in ("'", '"'):
            quote = ch
        elif ch == ">":
            return i + 1
        elif ch == "<" and i > start:
            # Another tag started before this one closed — malformed.
            return -1
    return -1


def _parse_attach_tag(tag: str) -> dict | None:
    attrs = {
        m.group(1): (m.group(2) if m.group(2) is not None else m.group(3))
        for m in _ATTACH_ATTR_RE.finditer(tag)
    }
    if not attrs:
        return None
    name = attrs.get("name") or ""
    ref = attrs.get("ref_id") or attrs.get("ref") or ""
    if not name and not ref:
        return None
    return {"kind": attrs.get("kind") or "file", "name": name, "ref_id": ref}


def split_attach_tags(text: str) -> tuple[str, list[dict]]:
    """Split ``text`` into (displayable text, attach_ref payloads).

    Complete tags are removed from the text; an unterminated tag at the end is
    left in place (the streaming filter holds it back and retries next chunk).
    """
    # Element-style calls (``<attach_ref …></attach_ref>``) leave a closing tag
    # behind once the opening one is consumed — drop those too.
    text = _ATTACH_CLOSE_RE.sub("", text)
    out: list[str] = []
    refs: list[dict] = []
    i = 0
    while True:
        j = text.find(_ATTACH_OPEN, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out), refs
        out.append(text[i:j])
        end = _tag_end(text, j)
        if end < 0:
            out.append(text[j:])
            return "".join(out), refs
        parsed = _parse_attach_tag(text[j:end])
        if parsed is None:
            out.append(text[j:end])
        else:
            refs.append(parsed)
        i = end


def _held_suffix_len(buf: str) -> int:
    """Length of the tail that might still grow into a tag ('' if none)."""
    idx = buf.rfind("<")
    if idx < 0:
        return 0
    tail = buf[idx:]
    if _ATTACH_OPEN.startswith(tail):
        return len(tail)              # a partial prefix of "<attach_ref"
    if tail.startswith(_ATTACH_OPEN):
        # A full opener: hold it only while it is still unterminated.
        return -1 if _tag_end(buf, idx) < 0 else 0
    if _ATTACH_CLOSE.startswith(tail):
        return len(tail)              # a partial prefix of "</attach_ref"
    if tail.startswith(_ATTACH_CLOSE):
        return 0 if ">" in tail else -1
    return 0


class AttachTagStreamFilter:
    """Incremental ``split_attach_tags`` for a token stream.

    ``feed`` returns the text that is safe to show plus any completed refs,
    holding back a trailing partial tag (which arrives split across chunks).
    ``flush`` releases whatever is still buffered at end of turn.
    """

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, text: str) -> tuple[str, list[dict]]:
        if not text:
            return "", []
        self._buf += text
        held = _held_suffix_len(self._buf)
        if held == -1:
            # Unterminated tag: prose before it is safe to show now; keep the
            # tag itself buffered until it closes (or flush).
            cut = self._buf.rfind("<")
            chunk, self._buf = self._buf[:cut], self._buf[cut:]
        elif held > 0:
            chunk, self._buf = self._buf[:-held], self._buf[-held:]
        else:
            chunk, self._buf = self._buf, ""
        return split_attach_tags(chunk)

    def flush(self) -> tuple[str, list[dict]]:
        chunk, self._buf = self._buf, ""
        return split_attach_tags(chunk)



def _normalize_stat_list(data) -> None:
    """Coerce a malformed ``stat_list`` payload in place so the UI can render it.

    Models wrap ``items`` in an extra envelope — observed live (2026-10-08,
    MiniMax via the model hub): ``data = {"items": {"item": [...]}, ...}``
    (an XML-style habit). The frontend's StatList does ``items.map`` on
    whatever arrives, so a truthy non-array crashes the whole React app —
    on the live event AND again on every history replay of the checkpoint,
    making the client unlaunchable. Unwrap a single list-valued dict, keep
    a stray list of items under another key, wrap a lone object, else drop.
    """
    if not isinstance(data, dict):
        return
    items = data.get("items")
    if isinstance(items, list):
        data["items"] = [it for it in items if isinstance(it, dict)]
        return
    if isinstance(items, dict):
        lists = [v for v in items.values() if isinstance(v, list)]
        if len(lists) == 1:
            data["items"] = [it for it in lists[0] if isinstance(it, dict)]
            return
    data["items"] = []


def widget_event(args: dict, tc_id: str | None = None) -> dict:
    """Build the `widget.emit` / history-replay payload.

    `render_id` is stamped here from the tool-call id (unique per invocation).
    The model sometimes echoes a reused `data.id` hoping the UI will refresh;
    we ignore that and never let two calls share a render_id.
    """
    rid = (tc_id or "").strip() or str(uuid.uuid4())
    data = args.get("data")
    if args.get("kind") == "stat_list":
        _normalize_stat_list(data)
    return {
        "kind": args.get("kind", "widget"),
        "data": data,
        "render_id": rid,
    }


@tool
def render_widget(kind: str, data: dict, summary: str = "") -> str:
    """Render a rich widget card in the chat instead of plain text.

    Use this when a structured view is clearer than prose (status lists,
    metrics, breakdowns, trends). Supported kinds:
      - "stat_list": data = {"title": str, "items": [{"label": str,
        "value"?: str, "status"?: "done"|"running"|"pending"|"ok"|"error"}]}
      - "chart": data = {"type": "bar"|"line"|"area"|"pie", "title": str,
        "x": <key>, "y": <key>, "data": [{"<x>": ..., "<y>": <number>}, ...],
        "format"?: "number"|"percent"|"currency"}
        data must be a flat array of <=30 objects with real computed numbers
        (never invented), aggregated before charting; one measure per chart.
        For pie, send the slices you want shown — the UI will not re-fold them.
    After rendering, also give a one-line textual summary.
    """
    return summary or f"[rendered widget: {kind}]"


@tool
def attach_ref(kind: str, name: str, ref_id: str = "") -> str:
    """Attach a clickable reference chip below the message.

    kind in {"file", "workflow", "doc", "link"}. name is the display label.
    For kind="file", ref_id should be the file's absolute path; a relative
    ref_id is resolved against the session workspace.
    """
    return f"[attached {kind}: {name}]"
