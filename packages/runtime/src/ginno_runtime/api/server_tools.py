"""Z.ai / GLM gateway server-side web-search blocks.

Z.ai's Anthropic-compatible endpoint runs ``web_search_prime`` inside the
model: the results ride back inside the ASSISTANT message as a
``server_tool_use`` block (request echo) followed by a ``tool_result`` block
whose ``content`` is a JSON string of ``{title, link, content, refer}`` items
— and the model narrates the whole call in plain ``text`` blocks
("**🌐 Z.ai Built-in Tool: web_search_prime** … **Output:**
``web_search_prime_result_summary:`` […raw JSON…]"). None of that matches the
Anthropic-native ``web_search_tool_result`` type the engine already knows, so
without the normalization here the narration leaks into the chat bubble as raw
JSON and the hits never register as citable turn sources.

Shared by the live stream (api/stream/engine.py) and history replay
(api/messages_ui.py). Everything is tolerant parsing — the shapes below come
from a real turn (2026-10-17, session 3e09b51e), and gateways drift.
"""

from __future__ import annotations

import json
import urllib.parse
from typing import Any

# The narration prefix Z.ai emits for its built-in tools. Distinct enough that
# a bare startswith gate won't catch the model's own prose.
_ZAI_BUILTIN_PREFIX = "**🌐 Z.ai Built-in Tool:"

# Server-side search tool names we recognize (extensible as gateways add more).
SERVER_SEARCH_TOOL_NAMES = frozenset({"web_search_prime", "web_search"})

# UI display name for synthesized tool bubbles: the frontend's webRefMap and
# the runtime's _web_ref_map both key on ``web_search`` (the local tool name),
# so the gateway tool must present under it for `web|sN` citations to resolve.
DISPLAY_TOOL_NAME = "web_search"


def is_server_tool_narration(text: str) -> bool:
    """True when an AI ``text`` block is the gateway's tool narration rather
    than the model's own prose. Matches the two block shapes a call produces:
    the ``**🌐 Z.ai Built-in Tool: … / Executing on server...`` intro and the
    ``**Output:** web_search_prime_result_summary: […json…]`` result dump."""
    t = (text or "").lstrip()
    if not t:
        return False
    if t.startswith(_ZAI_BUILTIN_PREFIX):
        return True
    if "web_search_prime_result_summary" in t[:120]:
        return True
    return t.startswith("**Output:**") and "_result_summary" in t[:200]


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""


def _parse_hit_payload(content: Any) -> list[dict]:
    """``tool_result.content`` → ``[{url, title, content, page_age}]``.

    Observed as a JSON *string* — ``[[{…}, …]]`` (a list wrapping the list) —
    but gateways have also been seen sending native lists or single objects.
    Items missing any URL are dropped; ``link`` is Z.ai's field name.
    """
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except ValueError:
            return []
    if isinstance(content, dict):
        content = content.get("content") or content.get("data") or [content]
    if not isinstance(content, list):
        return []
    # unwrap the [[...]] envelope
    items: list[Any] = []
    for el in content:
        if isinstance(el, list):
            items.extend(el)
        else:
            items.append(el)
    hits: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = str(it.get("link") or it.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        hits.append(
            {
                "url": url,
                "title": str(it.get("title") or ""),
                "content": str(it.get("content") or ""),
                "page_age": str(it.get("page_age") or ""),
            }
        )
    return hits


def _query_of(args: Any) -> str:
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return args.strip()
    if isinstance(args, dict):
        for key in ("search_query", "query", "q"):
            v = args.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
    return ""


def glm_server_search_groups(content: Any) -> list[dict]:
    """Extract server-side search calls from an assembled AI message.

    Returns ``[{tool_id, name, query, hits}]`` in block order — one entry per
    ``server_tool_use`` that a matching ``tool_result`` answered. Unpaired
    blocks (either side) yield nothing: half a call has nothing to show or
    cite. ``hits`` items are normalized to the ``{url, title, content,
    page_age}`` shape ``register_server_search_hits`` consumes.
    """
    if not isinstance(content, list):
        return []
    requests: dict[str, dict] = {}
    results: dict[str, list[dict]] = {}
    order: list[str] = []
    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "server_tool_use":
            tid = str(b.get("id") or "")
            name = str(b.get("name") or "")
            if tid and name in SERVER_SEARCH_TOOL_NAMES and tid not in requests:
                requests[tid] = {"name": name, "query": _query_of(b.get("input"))}
                order.append(tid)
        elif bt == "tool_result":
            tid = str(b.get("tool_use_id") or "")
            if tid in requests and tid not in results:
                hits = _parse_hit_payload(b.get("content"))
                if hits:
                    results[tid] = hits
    return [
        {**requests[tid], "tool_id": tid, "hits": results[tid]}
        for tid in order
        if tid in results
    ]


def format_hits_block(name: str, query: str, hits: list[tuple[str, dict]]) -> str:
    """Render hits in the local ``web_search`` output contract so the existing
    display and ``[sN]`` resolution work unchanged:
    ``[s1] Title — host\\n    https://url`` (+ snippet line).

    ``hits`` pairs each hit with its registered source id (``None`` → ``[·]``,
    the same placeholder the local tool uses when registration is off).
    """
    lines = [f"[search:{name}] {query!r} — {len(hits)} results:", ""]
    for sid, h in hits:
        title = h.get("title") or h["url"]
        snippet = " ".join((h.get("content") or "").split())
        lines.append(f"[{sid or '·'}] {title} — {_host_of(h['url'])}")
        lines.append(f"    {h['url']}")
        if snippet:
            lines.append(f"    Snippet: {snippet}")
        lines.append("")
    return "\n".join(lines).rstrip("\n")
