"""Code panel — S1 read endpoints (roots/list/read) + live diff.

Split from the former single-module ``api/code.py``;面板总述见
``api/code/__init__.py``。content helpers 只服务本文件的端点,随迁至此;
``rev=HEAD`` 的 git blob 读取在 ``_git._read_head``。
"""

from __future__ import annotations

import os
from pathlib import Path

from ...files.extractors import IMAGE_EXTS
from ._common import (
    BINARY_SNIFF_BYTES,
    EDITABLE_MAX_BYTES,
    HEAVY_DIRS,
    HIDDEN_COUNT_CAP,
    LIST_MAX_ENTRIES,
    MAX_DIFF_BYTES,
    PREVIEW_MAX_BYTES,
    PREVIEW_MAX_LINES,
    CodeAccessError,
    _ENCODINGS,
    _LANGUAGES,
    _decode,
    _err,
    _list_roots,
    _norm_rel,
    _session_ctx,
    _unknown_session,
    _write_state,
    resolve_code_target,
    router,
)
from ._git import _read_head


# ---- content helpers ------------------------------------------------------


def _detect_encoding(sample: bytes) -> str:
    for enc in _ENCODINGS:
        try:
            sample.decode(enc)
            return enc
        except UnicodeDecodeError:
            continue
    return "latin-1"


def _language_for(name: str) -> str:
    suffix = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if name.lower() in ("dockerfile", "containerfile"):
        return "dockerfile"
    return _LANGUAGES.get(suffix, "plaintext")


def _count_immediate(d: Path) -> int | None:
    """Immediate children of ``d``, capped at ``HIDDEN_COUNT_CAP``.

    One ``os.scandir`` getdents loop — no per-entry ``stat``, so it is far
    cheaper than the parent listing's own metadata pass. The caller's parent
    listing already ran one scandir; this adds AT MOST ONE more scandir per
    heavy dir in that level, which is bounded by how many heavy dirs the level
    holds and therefore acceptable.

    Returns ``HIDDEN_COUNT_CAP`` as a sentinel meaning "at least this many", or
    ``None`` when the directory cannot be read (never raises).
    """
    try:
        n = 0
        with os.scandir(d) as it:
            for _ in it:
                n += 1
                if n >= HIDDEN_COUNT_CAP:
                    return HIDDEN_COUNT_CAP
        return n
    except OSError:
        return None


def _entry(child: Path) -> dict:
    """One directory entry. File/dir win over "symlink" so a symlinked dir
    stays expandable; a broken link reports "symlink".

    A heavy dir additionally carries ``hidden_count`` (its immediate children,
    so the placeholder row can say "已隐藏 12,483 项"); it is ``None`` for every
    other entry, and for a heavy dir whose children cannot be counted."""
    try:
        if child.is_dir():
            typ = "dir"
        elif child.is_file():
            typ = "file"
        elif child.is_symlink():
            typ = "symlink"
        else:
            typ = "other"
    except OSError:
        typ = "other"
    size: int | None = None
    mtime: int | None = None
    try:
        st = child.stat()
        mtime = int(st.st_mtime)
        if typ == "file":
            size = st.st_size
    except OSError:
        pass
    hidden = typ == "dir" and child.name in HEAVY_DIRS
    return {
        "name": child.name,
        "type": typ,
        "size": size,
        "mtime": mtime,
        "hidden": hidden,
        "hidden_count": _count_immediate(child) if hidden else None,
    }


def _list_dir(d: Path) -> tuple[list[dict], bool]:
    entries = [_entry(c) for c in d.iterdir()]
    # Dirs first, then case-insensitive name (the tree re-sorts with a numeric
    # collator, but keep the wire order stable for non-UI consumers).
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    truncated = len(entries) > LIST_MAX_ENTRIES
    return entries[:LIST_MAX_ENTRIES], truncated



# ---- endpoints ------------------------------------------------------------


@router.get("/api/code/roots")
async def code_roots(project_slug: str = "default", session_id: str = "") -> dict:
    """Every root the panel can browse: mounts (primary first) + session."""
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _unknown_session(session_id)
    return {"ok": True, "roots": _list_roots(ctx)}


@router.get("/api/code/list")
async def code_list(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    path: str = "",
) -> dict:
    """One directory level. Heavy dirs are flagged ``hidden``, not omitted."""
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _unknown_session(session_id)
    try:
        target, root_info = resolve_code_target(ctx, root, path, write=False)
    except CodeAccessError as e:
        return _err(e.code, e.message)
    if not target.is_dir():
        return _err("not-directory", f"Not a directory: {path!r}")
    try:
        entries, truncated = _list_dir(target)
    except OSError as e:
        return _err("denied-path", f"Failed to read directory: {e}")
    return {
        "ok": True,
        "root": root_info["id"],
        "path": _norm_rel(path),
        "entries": entries,
        "truncated": truncated,
    }


@router.get("/api/code/read")
async def code_read(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    path: str = "",
    rev: str = "worktree",
) -> dict:
    """File contents. ``rev=HEAD`` reads the git blob (diff baseline); the
    default reads the working tree. Degrades to a read-only preview for huge
    files and returns ``text: null`` for binary ones."""
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _unknown_session(session_id)
    try:
        target, root_info = resolve_code_target(ctx, root, path, write=False)
    except CodeAccessError as e:
        return _err(e.code, e.message)
    if not target.is_file():
        return _err("not-text", f"Not a file: {path!r}")

    rel = _norm_rel(path)
    language = _language_for(target.name)
    if (rev or "").strip().upper() == "HEAD":
        return _read_head(root_info, rel, language)

    try:
        st = target.stat()
    except OSError as e:
        return _err("not-text", f"Failed to read file: {e}")
    size = st.st_size
    version = f"{size}:{st.st_mtime_ns}"
    editable, reason = _write_state(ctx, target, root_info)

    # Images are shown as PICTURES, never as text — including SVG, whose bytes
    # are text but whose content is a picture.
    #
    # Checked BEFORE the binary sniff on purpose: it makes the classification
    # independent of the sniff, so a text-formatted image lands here instead of
    # being mistaken for source, and a small raster that happened to carry no
    # NUL in its opening bytes can no longer fall through to the text path.
    # IMAGE_EXTS is the repo's single definition of "image"
    # (files/extractors.py), so a new format is picked up in one place.
    if target.suffix.lower() in IMAGE_EXTS:
        return {
            "ok": True,
            "root": root_info["id"],
            "path": rel,
            "version": version,
            "encoding": "binary",
            "language": language,
            "editable": False,
            "readonly_reason": "image",
            "text": None,
            "eof": True,
        }

    try:
        with target.open("rb") as fh:
            head = fh.read(BINARY_SNIFF_BYTES)
    except OSError as e:
        return _err("not-text", f"Failed to read file: {e}")
    if b"\x00" in head:
        # A non-image binary: no text form exists, so the panel shows the
        # "cannot open as text" notice (plus the external-app escape hatch).
        return {
            "ok": True,
            "root": root_info["id"],
            "path": rel,
            "version": version,
            "encoding": "binary",
            "language": language,
            "editable": False,
            "readonly_reason": "binary",
            "text": None,
            "eof": True,
        }

    if size > EDITABLE_MAX_BYTES:
        # Read-only preview: first PREVIEW_MAX_BYTES bytes / PREVIEW_MAX_LINES
        # lines, decoded with the encoding the sniff window detected (the cut
        # may split a character, so decode leniently on the preview chunk).
        enc = _detect_encoding(head)
        try:
            with target.open("rb") as fh:
                data = fh.read(PREVIEW_MAX_BYTES)
        except OSError as e:
            return _err("not-text", f"Failed to read file: {e}")
        text = data.decode(enc, errors="replace")
        lines = text.splitlines()
        if len(lines) > PREVIEW_MAX_LINES:
            lines = lines[:PREVIEW_MAX_LINES]
        return {
            "ok": True,
            "root": root_info["id"],
            "path": rel,
            "version": version,
            "encoding": enc,
            "language": language,
            "editable": False,
            "readonly_reason": "too-large",
            "text": "\n".join(lines),
            "eof": False,
        }

    try:
        data = target.read_bytes()
    except OSError as e:
        return _err("not-text", f"Failed to read file: {e}")
    text, enc = _decode(data)
    return {
        "ok": True,
        "root": root_info["id"],
        "path": rel,
        "version": version,
        "encoding": enc,
        "language": language,
        "editable": editable,
        "readonly_reason": reason,
        "text": text,
        "eof": True,
    }



# ---- live diff (code-panel-diff-design.md §6.2) ---------------------------


@router.get("/api/code/diff")
async def code_diff(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    path: str = "",
) -> dict:
    """Return the agent-session baseline vs. the current on-disk content.

    The baseline is the pre-write snapshot ``files/baselines.py`` recorded at the
    file's FIRST agent write of the session (works in non-git directories — the
    design's S5). Response shape (goal contract)::

        {ok, path, root,
         base_content: str | null,    # null → frontend treats it as added /
                                      # degraded (whole-file replace)
         base_version: str,           # stat identity of the pre-write state
         current_content: str | null, # null when absent/too large
         current_version: str,        # stat identity NOW ("" when absent)
         degraded: bool,              # baseline content unavailable (caps)
         baseline_exists: bool}       # False → the agent never wrote this file
                                      # in this session; no baseline fields mean
                                      # anything

    Read-only: same fence as ``read`` (roots/containment/deny), no writes, and
    no git involvement — a non-git root answers exactly like a git one.
    """
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _unknown_session(session_id)
    try:
        target, _root_info = resolve_code_target(ctx, root, path, write=False)
    except CodeAccessError as e:
        return _err(e.code, e.message)

    from ...files.baselines import get_baseline

    base = get_baseline(session_id, target)
    if base is None:
        return {
            "ok": True,
            "path": _norm_rel(path),
            "root": root,
            "baseline_exists": False,
            "base_content": None,
            "base_version": "",
            "current_content": None,
            "current_version": "",
            "degraded": False,
        }

    current_content: str | None = None
    current_version = ""
    if target.is_file():
        try:
            data = target.read_bytes()
            if len(data) <= MAX_DIFF_BYTES and b"\x00" not in data[:BINARY_SNIFF_BYTES]:
                text, _enc = _decode(data)
                current_content = text
            current_version = f"{len(data)}:{target.stat().st_mtime_ns}"
        except OSError:
            pass

    return {
        "ok": True,
        "path": _norm_rel(path),
        "root": root,
        "baseline_exists": True,
        "base_content": base.get("content"),
        "base_version": base.get("base_version") or "",
        "current_content": current_content,
        "current_version": current_version,
        "degraded": bool(base.get("degraded")),
    }

