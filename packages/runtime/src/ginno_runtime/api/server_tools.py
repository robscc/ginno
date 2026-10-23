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

import hashlib
import json
import re
import urllib.parse
from typing import Any

# The narration prefix Z.ai emits for its built-in tools. Distinct enough that
# a bare startswith gate won't catch the model's own prose. The gateway flips
# the header to Chinese (``**🌐 Z.ai 内置工具: …**``, ``**输入:**``,
# ``**输出:**``, ``*正在服务器上执行...*``) whenever the model is answering in
# Chinese — same machine narration, so both spellings must be recognized.
_ZAI_BUILTIN_PREFIX = "**🌐 Z.ai Built-in Tool:"
_ZAI_BUILTIN_PREFIX_ZH = "**🌐 Z.ai 内置工具"

# Any gateway built-in call header: a bold run whose first glyphs are an emoji
# (``**🌐 Z.ai 内置工具: web_search_prime**``, ``**✍️ write_file**``,
# ``**📊 render_widget**``). Bolding prose never looks like this — the leading
# emoji plus a snake_case name immediately after the ``**`` is what separates a
# machine header from something like ``**Input:**`` or ``**要点速览**``.
_CALL_HEADER_RE = re.compile(
    r"\*\*\s*([\u2190-\u2BFF\uFE0F\u200D\U0001F000-\U0001FAFF]{1,4})\s*"
    r"(?:Z\.ai\s*(?:Built-in\s+Tool|内置工具)\s*[:：]\s*)?"
    r"([A-Za-z][A-Za-z0-9_]*)\s*\*\*"
)
_OUTPUT_HEADER_RE = re.compile(r"\*\*\s*(?:Output|输出)\s*[:：]\s*\*\*")
_RESULT_SUMMARY_RE = re.compile(r"_result_summary\s*[:：]")
# The dump's own label — ``**web_search_prime_result_summary:**`` in bold, or
# bare. A dump is often emitted with no ``**Output:**`` header at all, so the
# label is a marker in its own right; it spans the whole label so no
# ``**web_search_prime`` scrap survives as prose.
_DUMP_LABEL_RE = re.compile(
    r"\*\*\s*[A-Za-z][A-Za-z0-9_]*_result_summary\s*[:：]\s*\*\*"
    r"|[A-Za-z][A-Za-z0-9_]*_result_summary\s*[:：]"
)
# Closes a call segment (header + args + this line). Nothing after it belongs
# to the call, so a call announced just before real prose can't eat the prose.
_EXECUTING_RE = re.compile(
    r"\*\s*(?:Executing on server\.\.\.|正在服务器上执行\.\.\.)\s*\*"
)
# Hits scraped out of a dump payload. The gateway TRUNCATES the payload it
# narrates (``"link": "https://press...`` + a raw newline — not JSON), so a
# strict parse fails on exactly the dumps worth showing; this reads the
# well-formed prefix by field instead.
_HIT_TEXT_RE = re.compile(
    r'"title"\s*:\s*"((?:[^"\\]|\\.)*)"\s*,\s*"(?:link|url)"\s*:\s*"(https?://[^"\\\s]+)"'
    r'(?:\s*,\s*"content"\s*:\s*"((?:[^"\\]|\\.)*)")?',
    re.DOTALL,
)
# A fenced JSON args block, as every call segment carries it.
_ARGS_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)
# "**Input:**" / "**输入:**" — the line between a call header and its args.
_INPUT_LABEL_RE = re.compile(r"\*\*\s*(?:Input|输入)\s*[:：]?\s*\*\*")
# How far past a header its args/dump may start. The gateway puts them
# immediately after; anything farther is the model bolding a word.
_MARKER_WINDOW = 400


def _is_call_header(text: str, m: re.Match) -> bool:
    """A bold emoji header only counts as a gateway call when the call shape
    follows it — an input label, a fenced JSON args block, or its "executing"
    line. Without this, ordinary prose like ``**⚡ fast**`` would read as a
    built-in call and get stripped along with everything up to the next
    marker."""
    seg = text[m.end() : m.end() + _MARKER_WINDOW]
    return bool(
        _INPUT_LABEL_RE.search(seg)
        or _ARGS_FENCE_RE.search(seg)
        or _EXECUTING_RE.search(seg)
    )


def _is_output_header(text: str, m: re.Match) -> bool:
    """``**Output:**`` heads a dump only when the dump label follows."""
    return bool(_DUMP_LABEL_RE.search(text[m.end() : m.end() + _MARKER_WINDOW]))


def _collect_markers(text: str) -> list[tuple[int, str, re.Match]]:
    """Narration segments in document order: ``(start, kind, match)``.

    Kinds: ``call`` (a built-in invocation), ``output`` (its ``**Output:**``
    header) and ``dump`` (the result-summary label + payload, which often
    arrives with no header at all).
    """
    calls = [
        (m.start(), "call", m)
        for m in _CALL_HEADER_RE.finditer(text)
        if _is_call_header(text, m)
    ]
    outputs = [
        (m.start(), "output", m)
        for m in _OUTPUT_HEADER_RE.finditer(text)
        if _is_output_header(text, m)
    ]
    dumps = [(m.start(), "dump", m) for m in _DUMP_LABEL_RE.finditer(text)]
    markers = calls + outputs + dumps
    markers.sort(key=lambda x: x[0])
    return markers

# Server-side search tool names we recognize (extensible as gateways add more).
SERVER_SEARCH_TOOL_NAMES = frozenset({"web_search_prime", "web_search"})

# UI display name for synthesized tool bubbles: the frontend's webRefMap and
# the runtime's _web_ref_map both key on ``web_search`` (the local tool name),
# so the gateway tool must present under it for `web|sN` citations to resolve.
DISPLAY_TOOL_NAME = "web_search"


def is_server_tool_narration(text: str) -> bool:
    """True when an AI ``text`` block is *entirely* the gateway's tool
    narration rather than the model's own prose. Matches the two block shapes a
    call produces: the ``**🌐 Z.ai Built-in Tool: … / Executing on server...``
    intro (English or Chinese) and the ``**Output:** ``
    <tool>_result_summary: […json…]`` result dump.

    Blocks the gateway consolidated prose+narration into are NOT narration as
    a whole — run ``split_narration`` on those to keep the prose."""
    t = (text or "").lstrip()
    if not t:
        return False
    if t.startswith(_ZAI_BUILTIN_PREFIX) or t.startswith(_ZAI_BUILTIN_PREFIX_ZH):
        return True
    head = _CALL_HEADER_RE.match(t)
    if head and _is_call_header(t, head):
        return True
    if "web_search_prime_result_summary" in t[:120]:
        return True
    return bool(_OUTPUT_HEADER_RE.match(t)) and "_result_summary" in t[:200]


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).netloc or "").lower()
    except ValueError:
        return ""


def _parse_hit_payload(content: Any) -> list[dict]:
    """``tool_result.content`` → ``[{url, title, content, page_age}]``.

    Observed as a JSON *string* — ``[[{…}, …]]`` (a list wrapping the list) —
    but gateways have also been seen sending native lists, single objects, and
    the narration's ``[{"text": [{…}, …]}]`` envelope (the hits sit one level
    down under a ``text`` key). Items missing any URL are dropped; ``link`` is
    Z.ai's field name.
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
    hits: list[dict] = []
    for it in _iter_hit_items(content):
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


# Wrapper keys the hits can hide behind in a gateway payload (list-of-lists,
# ``[{"text": [...]}]``, paged envelopes). Recursed into only while looking
# for hit-shaped dicts, so a real hit's own ``content`` string is never
# re-parsed as a container.
_HIT_CONTAINER_KEYS = ("text", "results", "content", "data", "items")


def _iter_hit_items(items: list) -> Any:
    for it in items:
        if isinstance(it, list):
            yield from _iter_hit_items(it)
        elif isinstance(it, dict):
            if it.get("link") or it.get("url"):
                yield it
                continue
            for key in _HIT_CONTAINER_KEYS:
                val = it.get(key)
                if isinstance(val, (list, dict)):
                    yield from _iter_hit_items([val])


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


def narration_tainted(text: str) -> bool:
    """True when *text* holds a narration segment anywhere — not just at its
    start — including one still arriving. The live stream sees narration as a
    sequence of small deltas, so the header is usually split across them: the
    tell-tales ("🌐 Z.ai", a result-summary key) are matched before the header
    is complete instead of waiting for it.

    Segment markers are validated (``_collect_markers``), so a model that
    simply bolds ``**Output:**`` in its own answer is not mistaken for a
    gateway dump.
    """
    if not text:
        return False
    if "🌐 Z.ai" in text or "_result_summary" in text:
        return True
    return bool(_collect_markers(text))


def split_narration(text: str) -> tuple[str, list[dict]]:
    """Split a text block into (model prose, recovered built-in calls).

    The gateway sometimes consolidates a whole step into ONE text block:
    prose, then every call's narration (``**🌐 Z.ai 内置工具: …**`` +
    ``**输入:**`` + the JSON args + ``*正在服务器上执行...*``), then the dumps
    (``**Output:**`` + ``**web_search_prime_result_summary:** […json…]``),
    with prose resuming in between. Such a block is not narration *as a
    whole*, so a block-level gate can only choose between leaking 12 KB of
    JSON and swallowing the model's answer — this splits at segment level.

    Every built-in the gateway narrates is recovered, not just searches
    (``✍️ write_file``, ``📊 render_widget`` …): each group carries the parsed
    ``args``, and a search call additionally picks up ``hits`` from its dump.

    Calls are paired with dumps **in order of appearance** (the dumps carry no
    call id; the gateway emits them FIFO against the calls it announced); a
    dump that yields no hits — e.g. ``MCP error -400`` — produces no hits but
    still consumes its call, so later queries can't drift. Returned groups
    carry a ``tool_id`` derived from the text itself, so the history renderer
    derives the same ids for the same block and the gateway's replayed
    transcript collapses instead of doubling.

    Tolerant by construction: text with no markers comes back untouched with
    no groups, and an unterminated dump is treated as narration.
    """
    if not text:
        return "", []
    parts = split_narration_parts(text)
    return (
        tidy_prose("".join(p for kind, p in parts if kind == "text")),
        [p for kind, p in parts if kind == "group"],
    )


def split_narration_parts(text: str) -> list[tuple[str, Any]]:
    """``split_narration``'s ordered form: ``("text", str)`` and
    ``("group", dict)`` alternated in document order.

    Order matters for display: the cards belong where the searches happened,
    interleaved with the prose the model wrote around them, not collected at
    the end of the bubble.
    """
    if not text:
        return []
    markers = _collect_markers(text)
    if not markers:
        return [("text", text)]

    parts: list[tuple[str, Any]] = []
    pending: list[dict] = []  # search calls awaiting their dump, FIFO
    digest = hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]
    seq = 0  # group ordinal — assigned on creation, NOT on emit: the searches
    # are created before their dumps arrive, so an emit-time index would hand
    # several of them the same id and collapse them into one card.
    cursor = 0

    def _add_text(chunk: str) -> None:
        # Leading whitespace is what the removed "executing" line leaves
        # behind; stripping it keeps markdown headings from turning into
        # indented blocks.
        chunk = tidy_prose(chunk).strip()
        if chunk:
            parts.append(("text", chunk))

    for i, (start, kind, m) in enumerate(markers):
        if start < cursor:
            continue  # inside a span already consumed
        _add_text(text[cursor:start])
        next_start = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        if kind == "call":
            # A call segment ends at its "executing" line — NOT at the next
            # marker: the gateway sometimes announces a call right before real
            # prose, and swallowing to the next marker would eat the answer.
            end = next_start
            exec_m = _EXECUTING_RE.search(text, start, next_start)
            if exec_m:
                end = exec_m.end()
            name = m.group(2)
            grp = {
                "tool_id": f"narr-{digest}-{seq}",
                "name": name,
                "args": _args_in(text[start:end]),
            }
            seq += 1
            grp["query"] = _query_of(grp["args"])
            if name in SERVER_SEARCH_TOOL_NAMES:
                pending.append(grp)
            else:
                parts.append(("group", grp))
            cursor = end
        else:
            # ``**Output:**`` immediately followed by a dump label is one
            # segment, not two: strip the header and let the dump marker do
            # the pairing, otherwise the header would consume a call that its
            # own payload answers — every later query then drifts by one.
            if kind == "output" and i + 1 < len(markers) and markers[i + 1][1] == "dump":
                cursor = m.end()
                continue
            from_start = start if kind == "dump" else m.end()
            hits, consumed = _dump_hits(text[from_start:next_start])
            if hits or kind == "dump":
                cursor = from_start + consumed
            else:
                # A bare "**Output:**" header (the gateway uses it for every
                # built-in) — strip the header only, the sentence after it is
                # the model's prose.
                cursor = m.end()
            # One dump per call, hits or not (an "MCP error -400" dump still
            # consumes its call) — otherwise every later query misaligns.
            call = pending.pop(0) if pending else None
            if call:
                call["hits"] = hits
                if hits:
                    parts.append(("group", call))
    _add_text(text[cursor:])
    return parts


def tidy_prose(text: str) -> str:
    """Clean up what segment removal leaves behind: the dumps are indented
    display payloads, so cutting them out strands runs of spaces on otherwise
    blank lines. Trailing whitespace goes, and 3+ blank lines collapse to the
    one blank line markdown actually means (paragraph break)."""
    if not text:
        return ""
    lines = [line.rstrip() for line in text.split("\n")]
    out: list[str] = []
    blanks = 0
    for line in lines:
        if line:
            blanks = 0
            out.append(line)
            continue
        blanks += 1
        if blanks <= 1:
            out.append("")
    return "\n".join(out).strip("\n")


def _args_in(span: str) -> dict:
    """The JSON args of a call segment (its fenced block), or ``{}``."""
    m = _ARGS_FENCE_RE.search(span)
    candidates = [m.group(1)] if m else []
    if not candidates:
        m = re.search(r"\{.*\}", span, re.DOTALL)
        if m:
            candidates = [m.group(0)]
    for raw in candidates:
        try:
            v = json.loads(raw)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return {}


def _dump_hits(span: str) -> tuple[list[dict], int]:
    """Parse one dump segment → (hits, chars consumed).

    A well-formed payload is parsed as JSON so the exact extent is known and
    whatever follows it in the same segment survives as prose. The gateway
    more often narrates a TRUNCATED payload (long strings cut to ``...`` with
    raw newlines), which no JSON parser accepts — those fall back to scanning
    the single payload line for hit fields and consuming to the line's end.
    """
    m = _RESULT_SUMMARY_RE.search(span)
    if not m:
        return [], len(span)  # nothing recognizable — swallow the segment
    start = next((i for i in range(m.end(), len(span)) if span[i] in "[{"), None)
    if start is None:
        return [], len(span)
    try:
        payload, end = json.JSONDecoder().raw_decode(span, start)
        return _parse_hit_payload(payload), end
    except ValueError:
        pass
    nl = span.find("\n", start)
    end = len(span) if nl == -1 else nl
    return _parse_hit_text(span[start:end]), end


def _parse_hit_text(payload: str) -> list[dict]:
    """Hit fields scraped from a truncated (non-JSON) dump payload."""
    hits: list[dict] = []
    for title, url, content in _HIT_TEXT_RE.findall(payload):
        hits.append(
            {
                "url": _unescape(url),
                "title": _unescape(title),
                "content": _unescape(content or ""),
                "page_age": "",
            }
        )
    return hits


def _unescape(s: str) -> str:
    if "\\" not in s:
        return s
    try:
        return json.loads(f'"{s}"')
    except ValueError:
        return s.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


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
