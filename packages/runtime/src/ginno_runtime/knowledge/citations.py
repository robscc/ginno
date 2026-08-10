"""Unified citation framework (docs/citations-design.md).

Two responsibilities:

1. **Turn source registry** — every turn keeps the list of sources the model
   actually saw: wiki pages injected by retrieval (``origin=injected``) and,
   later, web results (``origin=search/fetch/provider``). Entries get stable
   per-turn ids (``s1..sn``) that the citation contract lets the model quote.

2. **Citation block parsing** — the model appends ONE trailing
   ``<ginno_citations>`` block (typed entries ``kind|ref|note=[…]``) to its
   answer. We parse it tolerantly, validate each entry against the turn's
   registry (three states: ``verified`` / ``index_only`` / ``unverified``),
   and strip it from display text.

The registry is keyed by session_id (one turn at a time per session — the WS
turn lock guarantees it) and survives permission-interrupt → resume cycles:
``begin`` only happens on a fresh invoke; the resume path reuses the list.
"""

from __future__ import annotations

import contextvars
import re
import urllib.parse

# ---------------------------------------------------------------------------
# Turn source registry
# ---------------------------------------------------------------------------

# session_id -> live source list of the running (or interrupt-parked) turn.
_TURN_SOURCES: dict[str, list[dict]] = {}

# Set by the turn runner around build_turn_context so injection can register
# wiki sources without threading a session_id through the graph layer.
CURRENT_TURN_SOURCES: contextvars.ContextVar[list[dict] | None] = contextvars.ContextVar(
    "ginno_turn_sources", default=None
)


def begin_turn_sources(session_id: str) -> list[dict]:
    """Start (or reset, on retry) the source list for a turn of *session_id*."""
    lst: list[dict] = []
    _TURN_SOURCES[session_id] = lst
    return lst


def end_turn_sources(session_id: str) -> list[dict]:
    """Pop the session's source list (turn ended); missing -> []."""
    return _TURN_SOURCES.pop(session_id, [])


def peek_turn_sources(session_id: str) -> list[dict] | None:
    return _TURN_SOURCES.get(session_id)


def register_source(src: dict) -> dict | None:
    """Append a source to the active turn's list (no-op outside a turn).

    Assigns the sequential id ``sN`` when absent. Returns the entry (or None
    when no turn is active, e.g. KB disabled or a workflow-embedded call).
    """
    lst = CURRENT_TURN_SOURCES.get()
    if lst is None:
        return None
    src.setdefault("id", f"s{len(lst) + 1}")
    lst.append(src)
    return src


def register_source_for(session_id: str, src: dict) -> dict | None:
    """Session-keyed variant for code that runs OUTSIDE the injection task
    (e.g. the web tools executing inside the ToolNode) — tools know their
    session_id; the turn's list persists across steps until turn end."""
    lst = _TURN_SOURCES.get(session_id)
    if lst is None:
        return None
    src.setdefault("id", f"s{len(lst) + 1}")
    lst.append(src)
    return src


def upgrade_web_source(session_id: str, url: str, title: str = "", engine: str = "") -> dict | None:
    """Mark the turn source matching *url* as fully read (``depth=fetched``);
    register it fresh when no search hit matched. Returns the source."""
    lst = _TURN_SOURCES.get(session_id)
    if lst is None:
        return None
    key = normalize_web_ref(url)
    for s in lst:
        if s.get("kind") == "web" and normalize_web_ref(s.get("identity", "")) == key:
            s["depth"] = "fetched"
            s["origin"] = "fetch"
            if title:
                s["title"] = title
            return s
    return register_source_for(
        session_id,
        {
            "kind": "web",
            "identity": url,
            "title": title or url,
            "origin": "fetch",
            "depth": "fetched",
            **({"engine": engine} if engine else {}),
        },
    )


def register_wiki_sources(results: list) -> list[dict]:
    """Register retrieval results as wiki sources of the active turn.

    ``results`` are ``knowledge.types.RetrievalResult``; each contributes its
    entry's identity. Returns the registered sources (may be empty).
    """
    out: list[dict] = []
    for r in results:
        e = getattr(r, "entry", None)
        rel = getattr(e, "relative_path", "") if e else ""
        if not rel:
            continue
        src = register_source(
            {
                "kind": "wiki",
                "identity": rel,
                "title": getattr(e, "title", "") or rel,
                "origin": "injected",
                "depth": "injected",
            }
        )
        if src:
            out.append(src)
    return out


# ---------------------------------------------------------------------------
# Citation block parsing
# ---------------------------------------------------------------------------

# Trailing block, tolerant of whitespace; legacy draft name also accepted.
_BLOCK_RE = re.compile(
    r"<\s*ginno_(?:wiki_)?citations\s*>(.*?)<\s*/\s*ginno_(?:wiki_)?citations\s*>",
    re.IGNORECASE | re.DOTALL,
)

# Brackets are OPTIONAL. The citation contract is ``note=[…]``, but models often
# emit ``note=…`` without the brackets; if we only matched the bracketed form the
# note would never split off and a web ref would be left as ``<url>|note=…`` — a
# corrupt link that cannot be opened (the reported "来源链接打不开" bug).
_NOTE_RE = re.compile(r"\|\s*note\s*=\s*\[?(.*?)\]?\s*$", re.IGNORECASE | re.DOTALL)

MAX_CITATIONS_PER_TURN = 20

VALID_KINDS = ("wiki", "web")


# A block truncated mid-way (max_tokens / stall watchdog) has the opening tag
# but no closing tag — strip from the opener to end-of-text so raw machine
# lines never leak into display text or the memory pool.
_UNCLOSED_BLOCK_RE = re.compile(r"<\s*ginno_(?:wiki_)?citations\b[^>]*>[\s\S]*$", re.IGNORECASE)


def strip_citation_block(text: str) -> str:
    """Return *text* with any citation block removed (for display/capture).

    Handles both closed blocks and truncated blocks whose closing tag never
    arrived (the model output was cut off inside the block).
    """
    out = _BLOCK_RE.sub("", text)
    out = _UNCLOSED_BLOCK_RE.sub("", out)
    return out.rstrip()


def parse_citation_block(text: str) -> list[dict]:
    """Extract typed citation entries from the trailing block, if present.

    Each entry: ``{"kind": "wiki"|"web", "ref": str, "note": str}``.
    Tolerant: blank lines skipped, missing note allowed, duplicates (same
    kind+ref, case-insensitive for wiki) dropped, capped per turn. Unknown
    kinds are ignored. The legacy ``<ginno_wiki_citations>`` block yields
    ``kind="wiki"`` entries (bare ``ref`` or ``ref|note=[…]`` lines).
    """
    m = _BLOCK_RE.search(text)
    if not m:
        return []
    entries: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for line in m.group(1).splitlines():
        line = line.strip()
        if not line:
            continue
        note = ""
        nm = _NOTE_RE.search(line)
        if nm:
            note = nm.group(1).strip()
            line = line[: nm.start()].rstrip()
        kind, sep, ref = line.partition("|")
        if not sep:
            # bare entry (legacy wiki block): assume wiki
            kind, ref = "wiki", line
        kind = kind.strip().lower()
        ref = ref.strip()
        if kind not in VALID_KINDS or not ref:
            continue
        key = (kind, ref.lower() if kind == "wiki" else normalize_web_ref(ref))
        if key in seen:
            continue
        seen.add(key)
        entries.append({"kind": kind, "ref": ref, "note": note})
        if len(entries) >= MAX_CITATIONS_PER_TURN:
            break
    return entries


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def normalize_web_ref(ref: str) -> str:
    """Normalize a URL for matching: scheme+host lowercased, no fragment,
    trailing slash dropped, common tracking params removed."""
    ref = ref.strip()
    if not re.match(r"^https?://", ref, re.IGNORECASE):
        return ref.lower()
    try:
        parts = urllib.parse.urlsplit(ref)
    except ValueError:
        return ref.lower()
    host = (parts.netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    query_pairs = []
    for k, v in urllib.parse.parse_qsl(parts.query):
        kl = k.lower()
        if kl in ("utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "fbclid", "gclid"):
            continue
        query_pairs.append((k, v))
    query = urllib.parse.urlencode(query_pairs)
    path = parts.path.rstrip("/") or "/"
    return urllib.parse.urlunsplit(("https", host, path, query, ""))


def _norm_wiki_ref(ref: str) -> str:
    """Case-insensitive wiki ref key, tolerant of leading './' segments and
    the optional ``.md`` suffix.

    Strips ``./`` as a SEGMENT prefix, not a character set — ``lstrip("./")``
    would turn ``.env`` into ``env`` and collide unrelated refs.
    """
    r = ref.strip()
    while r.startswith("./"):
        r = r[2:]
    r = r.lstrip("/")
    if r.lower().endswith(".md"):
        r = r[: -len(".md")]
    return r.lower()


def validate_citations(
    entries: list[dict],
    turn_sources: list[dict] | None,
    resolve_wiki=None,
) -> list[dict]:
    """Classify parsed entries against the turn's registry.

    - wiki: verified when the ref matches an injected source (by relative
      path or title); ``index_only`` when *resolve_wiki* (callable
      ``ref -> canonical relative_path | None``) finds it in the index though
      it was not injected this turn; else ``unverified``.
    - web: verified when ``sN`` or the normalized URL matches a turn source;
      else ``unverified``.

    Returns copies of the entries augmented with ``status`` and, when
    resolved, ``identity`` (canonical path/URL), ``title`` and ``depth``.
    """
    sources = turn_sources or []
    wiki_by_path = {_norm_wiki_ref(s.get("identity", "")): s for s in sources if s.get("kind") == "wiki"}
    wiki_by_title = {(s.get("title") or "").strip().lower(): s for s in sources if s.get("kind") == "wiki"}
    web_by_id = {s.get("id"): s for s in sources if s.get("kind") == "web"}
    web_by_url = {normalize_web_ref(s.get("identity", "")): s for s in sources if s.get("kind") == "web"}

    out: list[dict] = []
    for e in entries:
        item = dict(e)
        kind, ref = e["kind"], e["ref"]
        src: dict | None = None
        if kind == "wiki":
            key = _norm_wiki_ref(ref)
            src = wiki_by_path.get(key) or wiki_by_title.get(ref.strip().lower())
            if src:
                item.update(status="verified", identity=src.get("identity"), title=src.get("title"), depth=src.get("depth"), engine=src.get("engine") or "")
            else:
                canon = resolve_wiki(ref) if callable(resolve_wiki) else None
                if canon:
                    item.update(status="index_only", identity=canon)
                else:
                    item["status"] = "unverified"
        else:  # web
            if re.fullmatch(r"s\d+", ref, re.IGNORECASE):
                src = web_by_id.get(ref.lower())
            if src is None:
                src = web_by_url.get(normalize_web_ref(ref))
            if src:
                item.update(
                    status="verified",
                    identity=src.get("identity"),
                    title=src.get("title"),
                    depth=src.get("depth"),
                    engine=src.get("engine") or "",
                )
            else:
                item["status"] = "unverified"
        out.append(item)
    return out
