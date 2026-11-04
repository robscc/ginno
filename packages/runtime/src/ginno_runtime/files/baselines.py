"""Session baselines for the code-panel live diff (code-panel-diff-design.md §6.1).

When the agent writes a file for the FIRST time in a session, we snapshot the
pre-write content under ``(session_id, absolute path)``. Later writes to the
same file never overwrite the baseline — it is the "before the agent touched
it" anchor the diff endpoint renders against, and it works in non-git
directories (the design's S5).

Bounds (subagent constraints): per session at most ``PER_SESSION_MAX_FILES``
entries and per file at most ``MAX_FILE_BYTES`` of snapshotted content.
Everything beyond the bounds still records the pre-write ``base_version``
(a stat identity) with ``degraded=True`` — the frontend then treats the change
as a whole-file replace instead of asking for a content diff. The in-memory map
is the authoritative read/write path; a per-session mirror file
``projects/<slug>/sessions/<session_id>/baselines.json`` is written synchronously
after each snapshot and lazily restored on the first access after a restart
(design §6.1 内存+磁盘双存). Sessions without a resolvable project slug (and
unit tests using anonymous session ids) simply stay memory-only.

Everything here is best-effort and never raises into the callers (the tool
write path and the WS broadcast must not break a turn over decoration).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .. import paths

#: Per-session cap on snapshotted files (goal constraint: 200).
PER_SESSION_MAX_FILES = 200
#: Single-file content cap (goal constraint: 2 MB). Beyond it the baseline
#: keeps only the version token and marks the entry ``degraded``.
MAX_FILE_BYTES = 2 * 1024 * 1024

# session_id -> {realpath str -> {"content": str | None, "base_version": str,
#                                 "degraded": bool}}
# ``content is None`` means "no pre-write content available" — either the file
# did not exist before (an added file) or the content was beyond the caps
# (``degraded`` is then True).
_BASELINES: dict[str, dict[str, dict]] = {}


def _key(p: Path) -> str:
    """Stable map key: realpath, so symlinked mounts and macOS ``/private/tmp``
    match the path the tool actually wrote (same rule as code_changes.match_root_id)."""
    try:
        return os.path.realpath(str(p))
    except OSError:
        return str(p)


def _version_of(p: Path) -> str:
    """stat identity identical to api/code.py's read/write (``size:mtime_ns``)."""
    try:
        st = p.stat()
        return f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        return ""


def snapshot_before_write(session_id: str, path: Path) -> dict | None:
    """Record the pre-write state of ``path`` for ``session_id`` — first write only.

    Called from ``tools/builtin.py``'s ``write_file`` / ``edit_file`` before the
    bytes hit the disk. Returns the stored entry (``None`` when the session is
    unknown/blank) — for tests; callers must not branch on it.
    """
    sid = (session_id or "").strip()
    if not sid:
        return None
    _load_session(sid)
    p = Path(path)
    key = _key(p)
    sess = _BASELINES.setdefault(sid, {})
    existing = sess.get(key)
    if existing is not None:
        return existing  # first-write-wins: later writes keep the original

    if not p.is_file():
        # The agent creates the file: an empty baseline (content None) marks it
        # as "added relative to the session baseline".
        entry = {"content": None, "base_version": "", "degraded": False}
        sess[key] = entry
        return entry

    version = _version_of(p)
    try:
        size = p.stat().st_size
    except OSError:
        size = MAX_FILE_BYTES + 1
    if size > MAX_FILE_BYTES or len(sess) >= PER_SESSION_MAX_FILES:
        # Over the caps: keep the version token only, mark degraded so the
        # frontend does a whole-file replace instead of a content diff.
        entry = {"content": None, "base_version": version, "degraded": True}
    else:
        try:
            # P1 (2026-11-04): probe utf-8 → gbk → latin-1 so a GBK file's
            # baseline decodes the SAME way /api/code/diff decodes current
            # content (api/code.py _decode). The old utf-8-replace produced a
            # mojibake left pane in the inline diff for GBK files.
            data = p.read_bytes()
            content = None
            for enc in ("utf-8", "gbk", "latin-1"):
                try:
                    content = data.decode(enc)
                    break
                except UnicodeDecodeError:
                    continue
            if content is None:  # latin-1 never fails; kept for clarity
                content = data.decode("latin-1", errors="replace")
        except OSError:
            content = None
        entry = {"content": content, "base_version": version, "degraded": False}
    sess[key] = entry
    _save_session(sid)
    return entry


def get_baseline(session_id: str, path: Path) -> dict | None:
    """The stored baseline entry for ``(session_id, path)``, or ``None`` when
    the agent never wrote this file in this session (the endpoint then answers
    ``baseline_exists: False`` and the frontend falls back)."""
    sid = (session_id or "").strip()
    _load_session(sid)
    sess = _BASELINES.get(sid)
    if not sess:
        return None
    e = sess.get(_key(Path(path)))
    return dict(e) if e is not None else None


def base_version_of(session_id: str, path: Path) -> str | None:
    """The baseline version token for the ``code.changed`` event, or ``None``
    when there is no baseline (the file was never agent-written this session)."""
    e = get_baseline(session_id, path)
    return None if e is None else e.get("base_version") or ""


def clear_session(session_id: str) -> None:
    """Drop every baseline of ``session_id`` (session-delete hook).

    Also removes the on-disk ``baselines.json`` (design §6.1 磁盘持久化) —
    a deleted session must not resurrect its baselines on the next restart.
    Best-effort: any OSError is swallowed (the in-memory drop already took).
    """
    sid = (session_id or "").strip()
    _BASELINES.pop(sid, None)
    _LOADED.discard(sid)
    _DISK_PATHS.pop(sid, None)
    p = _disk_path(sid)
    if p is None:
        return
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


# ---- disk persistence (code-panel-diff-design.md §6.1 内存+磁盘双存) --------

#: Schema marker for the on-disk ``baselines.json``.
_DISK_SCHEMA = 1
#: Session ids whose disk file has already been read (or was found absent) —
#: prevents re-reading on every lookup. Cleared by ``clear_session``.
_LOADED: set[str] = set()
#: Resolved disk paths per session id (``None`` = no slug — memory-only).
#: Cleared by ``clear_session`` so a recreated session re-resolves.
_DISK_PATHS: dict[str, Path | None] = {}


def _disk_path(session_id: str) -> Path | None:
    """``projects/<slug>/sessions/<session_id>/baselines.json``, or ``None``
    when the session has no resolvable project slug (anonymous test sessions
    and deleted sessions — those simply stay memory-only)."""
    sid = (session_id or "").strip()
    if not sid:
        return None
    if sid in _DISK_PATHS:
        return _DISK_PATHS[sid]
    try:
        from ..session_meta import _session_slug

        slug = _session_slug(sid)
    except Exception:  # noqa: BLE001 — best-effort decoration
        slug = None
    p = paths.session_files_dir(str(slug), sid) / "baselines.json" if slug else None
    _DISK_PATHS[sid] = p
    return p


def _save_session(session_id: str) -> None:
    """Mirror ``_BASELINES[session_id]`` to disk after a snapshot (synchronous:
    the file is small — 200 entries × ≤2 MB content bounded by the caps — and
    snapshots already did a disk read of the pre-write bytes, so one more
    small write does not move the write path's latency profile). Never raises.
    """
    p = _disk_path(session_id)
    if p is None:
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(
                {"schema": _DISK_SCHEMA, "entries": _BASELINES.get(session_id, {})},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, p)
    except (OSError, TypeError, ValueError):
        try:
            p.with_suffix(".json.tmp").unlink(missing_ok=True)
        except OSError:
            pass


def _load_session(session_id: str) -> None:
    """Restore a session's baselines from disk into the in-memory map — lazily,
    on the first access after a restart, so startup never scans the disk.
    Corrupt JSON / missing fields / wrong schema degrade to an empty baseline
    set (the diff endpoint then answers ``baseline_exists: False``, same as a
    session that never wrote anything). Never raises.
    """
    sid = (session_id or "").strip()
    if sid in _LOADED:
        return
    _LOADED.add(sid)
    if sid in _BASELINES:
        return
    p = _disk_path(sid)
    if p is None or not p.is_file():
        return
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        entries = data.get("entries")
        if not isinstance(entries, dict):
            return
        clean: dict[str, dict] = {}
        for k, e in entries.items():
            if not isinstance(k, str) or not isinstance(e, dict):
                continue
            clean[k] = {
                "content": e.get("content"),
                "base_version": str(e.get("base_version") or ""),
                "degraded": bool(e.get("degraded")),
            }
        if clean:
            _BASELINES[sid] = clean
    except (OSError, ValueError):
        pass
