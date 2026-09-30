"""Agent-side code-change surfacing (code-panel S3, design §4.2-3).

When the agent writes or edits a file (``write_file`` / ``edit_file``) the code
panel has to learn about it: the tree marks the file, an open **clean** tab
reloads, an open **dirty** tab gets S2's conflict bar instead of being
overwritten. The transport deliberately reuses the inline-images pattern
(``files/images.py``) rather than inventing a mechanism — and NOT the hook
system, whose ``PostToolUse`` is declared but never dispatched (design §7,
"已验证不可行"):

1. the tool appends a compact machine marker to its own result text,
2. the WS layer parses it and emits one ``code.changed`` per file,
3. the marker is stripped everywhere a human or the model could see it — the
   agent node's LLM-bound copy (``graph.strip_tool_code_markers``), the
   persisted-history / live tool-bubble renderer
   (``api.messages_ui._tool_content_str``, which feeds both).

Marker shape — a JSON array of ``{path, op, version}`` with ABSOLUTE paths, so
spaces and CJK filenames round-trip safely::

    <!--ginno-code:[{"path":"/abs/x.py","op":"write","version":"123:456789"}]-->

``op`` is ``"write"`` (``write_file``) or ``"edit"`` (``edit_file``).

``version`` is ``f"{size}:{st_mtime_ns}"`` re-``stat``-ed AFTER the write — the
exact value ``api/code.py``'s ``read``/``write`` produce. That identity is what
lets the panel drive a pushed change through S2's existing conflict bar instead
of a parallel UI (brief §0): the pushed version is compared with the buffer's
``base_version`` like any other save/read mismatch.
"""

from __future__ import annotations

import json
import os
import re

#: Marker wrapper. Non-greedy so the first ``]-->`` closes it, and DOTALL so a
#: pretty-printed payload (newlines inside the JSON) still parses.
_MARKER_RE = re.compile(r"<!--ginno-code:(\[.*?\])-->", re.S)

#: The only two ops the wire event and the frontend tree understand.
_VALID_OPS = ("write", "edit")


def encode_code_marker(changes: list[dict]) -> str:
    """Build the marker trailer for a list of change dicts."""
    return "<!--ginno-code:" + json.dumps(list(changes), ensure_ascii=False) + "-->"


def parse_code_marker(text: str) -> list[dict]:
    """Extract change dicts from a marker, or ``[]`` if none/invalid.

    Per entry: a non-empty string ``path`` and an ``op`` in
    ``("write", "edit")`` are required; anything else is dropped rather than
    emitted half-formed. ``version`` is passed through as a string (``""`` when
    absent/unusable) — the tree mark is still worth surfacing, and the panel
    treats an empty version conservatively.
    """
    if not isinstance(text, str) or "<!--ginno-code:" not in text:
        return []
    m = _MARKER_RE.search(text)
    if not m:
        return []
    try:
        items = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    out: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        path = it.get("path")
        op = it.get("op")
        if not isinstance(path, str) or not path:
            continue
        if op not in _VALID_OPS:
            continue
        version = it.get("version")
        out.append({"path": path, "op": op, "version": version if isinstance(version, str) else ""})
    return out


def strip_code_marker(text: str) -> str:
    """Remove any marker (and a preceding trailing newline) from tool text."""
    if not isinstance(text, str) or "<!--ginno-code:" not in text:
        return text
    return _MARKER_RE.sub("", text).rstrip()


def match_root_id(path: str, roots: list[dict]) -> str | None:
    """Longest-prefix root id for an absolute path, or ``None``.

    ``roots`` is ``api/code.py``'s wire list (``[{"id", "path", ...}]`` — the
    session's mounts plus the workspace). Longest prefix wins, the same rule the
    frontend applies to place the file in the tree, and the same rule
    ``builtin.mount_access`` uses for nested mounts.

    Both sides are ``realpath``-ed: macOS ``/tmp`` is really ``/private/tmp``,
    and a mount registered through a symlink must still match the path the tool
    actually wrote. A file outside every root yields ``None`` (the panel then
    has nowhere to put it) — this is a best-effort lookup, never an error.
    """
    if not isinstance(path, str) or not path or not roots:
        return None
    try:
        target = os.path.realpath(path)
    except OSError:
        target = path
    best_id: str | None = None
    best_len = -1
    for r in roots:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        rp = r.get("path")
        if not isinstance(rid, str) or not rid or not isinstance(rp, str) or not rp:
            continue
        try:
            base = os.path.realpath(rp)
        except OSError:
            base = rp
        if target != base and not target.startswith(base.rstrip(os.sep) + os.sep):
            continue
        if len(base) > best_len:
            best_id, best_len = rid, len(base)
    return best_id