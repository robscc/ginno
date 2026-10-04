"""Code panel endpoints — read-only workspace browsing (docs/code-panel-design.md).

The panel addresses files BY ROOT + RELATIVE PATH (never an absolute path):
a *root* is either a mounted context folder (``f_xxxx``) or the session
workspace (``session``). ``roots``/``list``/``read`` are S1, ``write`` is S2,
git decoration is S3, and ``search`` (name ⌘P / content ⇧⌘F) is S4.

Fence model (design §4.6): every request runs through ``resolve_code_target``,
which applies the gates in order — root lookup, containment, symlink
re-resolution, the permanent deny regions, and (for writes) the ``ro`` tier /
``.git`` rule. Gate 4 mirrors ``tools/builtin._path_denied`` EXACTLY: the
exempt base is the SESSION WORKSPACE dir, never ``primary_path`` (a mount lives
outside ``~/.ginno``, so passing it silently disables the workspace exemption
and the session-workspace root comes back empty).

Responses follow the ``{ok, ...}`` shape used by ``files.py``/``folders.py``
(HTTP 200 even on failure); errors carry an appended-only ``code`` the UI maps
to copy.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path

from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse

from .. import context_folders as cf
from .. import paths
from ..files.extractors import IMAGE_EXTS
from ..server_shared import _SESSIONS
from ..session_meta import _find_meta
from ..tools.builtin import (
    GLOB_MAX_DIRS,
    GLOB_MAX_HITS,
    GLOB_TIME_BUDGET_S,
    _path_denied,
    mount_access,
    resolve_mounts,
)

router = APIRouter()

# One directory level: mirror cf.probe's per-level cap.
LIST_MAX_ENTRIES = 2000
# "Heavy" dirs are NOT hidden from the payload — they come back with
# ``hidden: true`` so the tree can render a clickable placeholder row instead
# of either refusing to show them or listing 12k entries.
HEAVY_DIRS = frozenset(
    {
        ".git",
        "node_modules",
        ".next",
        "dist",
        "build",
        "out",
        "target",
        "coverage",
        "__pycache__",
        ".venv",
        "venv",
        ".turbo",
        ".pytest_cache",
        ".mypy_cache",
        ".gradle",
    }
)
# Immediate-child count reported for a heavy dir (``hidden_count``) stops here;
# the value itself is then a sentinel meaning "at least this many" (the UI shows
# "9,999+"). Keeping the loop bounded means listing a directory that contains
# many heavy dirs stays cheap even when those dirs hold hundreds of thousands of
# entries.
HIDDEN_COUNT_CAP = 9999
# An editable text file may be at most this; beyond it the read degrades to a
# read-only preview (first PREVIEW_MAX_BYTES / PREVIEW_MAX_LINES).
EDITABLE_MAX_BYTES = 5 * 1024 * 1024
PREVIEW_MAX_BYTES = 2 * 1024 * 1024
PREVIEW_MAX_LINES = 5000
# Binary sniff window (a NUL byte anywhere here means "not text").
BINARY_SNIFF_BYTES = 8 * 1024
# Encoding probe order — utf-8, then gbk (Chinese files are the common miss),
# then latin-1 which never raises so a file always yields SOME text.
_ENCODINGS = ("utf-8", "gbk", "latin-1")

# Extension → Monaco basic-language id. Unknown extensions fall back to
# plaintext (the editor then word-wraps).
_LANGUAGES = {
    "py": "python",
    "pyi": "python",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "json": "json",
    "jsonc": "json",
    "md": "markdown",
    "markdown": "markdown",
    "css": "css",
    "scss": "scss",
    "less": "less",
    "html": "html",
    "htm": "html",
    "vue": "html",
    "xml": "xml",
    "svg": "xml",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "ini",
    "ini": "ini",
    "cfg": "ini",
    "conf": "ini",
    "sh": "shell",
    "bash": "shell",
    "zsh": "shell",
    "sql": "sql",
    "rs": "rust",
    "go": "go",
    "java": "java",
    "kt": "kotlin",
    "swift": "swift",
    "rb": "ruby",
    "php": "php",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "cs": "csharp",
    "lua": "lua",
    "pl": "perl",
    "r": "r",
    "scala": "scala",
    "dart": "dart",
    "dockerfile": "dockerfile",
    "graphql": "graphql",
    "ps1": "powershell",
    "bat": "bat",
}


class CodeAccessError(Exception):
    """A fence gate failed. Carries the wire error ``code`` (design §4.3)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ---- session context ------------------------------------------------------


def _mount_dirs(meta: dict) -> tuple[list[dict], str | None]:
    """Resolved mount dirs + primary path for a warm OR cold session.

    In-memory sessions (create_session / _ensure_session) already hold the
    resolved ``context_dirs``/``primary_path``; a cold session only has the
    on-disk ``context_folders``/``primary_folder`` ids, which go through the
    same ``resolve_session_dirs`` the bootstrap uses.
    """
    dirs = meta.get("context_dirs")
    if isinstance(dirs, list):
        return dirs, (meta.get("primary_path") or None)
    ids = meta.get("context_folders") or []
    return cf.resolve_session_dirs(ids, meta.get("primary_folder") or None)


def _session_ctx(project_slug: str, session_id: str) -> dict | None:
    """Resolve a session's code-panel context (cold sessions included).

    Session/slug resolution mirrors ``api/sessions.py``'s ``_ensure_session``
    EXACTLY: use the in-memory session when there is one, otherwise fall back to
    ``_find_meta``, which yields the slug from the ON-DISK project directory.
    The slug MUST come from the same resolution ``_ensure_session`` uses,
    because the workspace is derived from it — a legacy index entry that omits
    ``project_slug`` would otherwise resolve a cold session under ``default``
    and ``resolve_code_target`` would report ``root-missing`` instead of a
    usable workspace. The workspace itself is authoritative from ``paths``
    (not meta), like ``_ensure_session``.
    """
    sid = (session_id or "").strip()
    if not sid:
        return None
    warm = _SESSIONS.get(sid)
    if warm is not None:
        meta, dir_slug = warm, warm.get("project_slug")
    else:
        found = _find_meta(sid)
        if found is None:
            return None
        meta, dir_slug = found
    slug = str(meta.get("project_slug") or dir_slug or project_slug or "default")
    mount_dirs, primary_path = _mount_dirs(meta)
    mounts = resolve_mounts(mount_dirs)
    return {
        "session_id": sid,
        "project_slug": slug,
        "workspace": paths.session_files_dir(slug, sid),
        "mount_dirs": mount_dirs,
        "mounts": mounts,
        # NEVER add the session workspace here: extra_roots must mirror
        # builtin.py, where putting it in would punch through the home deny.
        "mount_roots": [p for p, _ in mounts],
        "primary_folder": meta.get("primary_folder") or None,
        "primary_path": primary_path,
    }


def _git_info(root: Path) -> tuple[bool, str | None]:
    """``(is_repo, branch)`` for a directory — branch None when unknown.

    Read straight from ``.git/HEAD`` (no subprocess): ``ref: refs/heads/x`` →
    ``x``, a detached HEAD → its short sha. ``.git`` may be a file in a
    worktree/submodule, in which case its ``gitdir:`` target is followed.
    """
    git = root / ".git"
    try:
        if not git.exists():
            return False, None
        head = git / "HEAD"
        if git.is_file():
            line = git.read_text(encoding="utf-8", errors="replace").strip()
            if line.startswith("gitdir:"):
                head = Path(line.split(":", 1)[1].strip()) / "HEAD"
        text = head.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return True, None
    if text.startswith("ref:"):
        ref = text.split(":", 1)[1].strip()
        return True, (ref.rsplit("/", 1)[-1] or None)
    return True, (text[:12] or None)


def _root_from_dir(d: dict) -> dict:
    """A mount dict (resolve_session_dirs shape) → the wire ``Root``."""
    raw = str(d.get("path") or "")
    p = Path(raw) if raw else None
    if p is not None and p.is_dir():
        is_repo, branch = _git_info(p)
    else:
        is_repo, branch = False, None
    return {
        "id": d.get("id") or "",
        "name": d.get("name") or (p.name if p else str(d.get("id") or "")),
        "path": raw,
        "access": "rw" if d.get("access") == "rw" else "ro",
        "missing": bool(d.get("missing")),
        "is_repo": is_repo,
        "branch": branch,
    }


def _list_roots(ctx: dict) -> list[dict]:
    """The session's roots: primary folder first, then the rest, then the
    session workspace (always last)."""
    dirs = list(ctx["mount_dirs"])
    primary = ctx.get("primary_folder")
    # Stable sort keeps the original mount order behind the primary.
    dirs.sort(key=lambda d: 0 if (primary and d.get("id") == primary) else 1)
    roots = [_root_from_dir(d) for d in dirs]
    ws = ctx["workspace"]
    is_repo, branch = _git_info(ws)
    roots.append(
        {
            "id": "session",
            "name": "Session workspace",
            "path": str(ws),
            "access": "rw",
            "missing": not ws.is_dir(),
            "is_repo": is_repo,
            "branch": branch,
        }
    )
    return roots


# ---- fence ----------------------------------------------------------------


def _norm_rel(p: str) -> str:
    """Normalize a client-supplied relative path to a stable posix echo."""
    rel = (p or "").strip().replace("\\", "/")
    while rel.startswith("./"):
        rel = rel[2:]
    return rel.strip("/")


def resolve_code_target(
    session: dict, root_id: str, relpath: str, *, write: bool = False
) -> tuple[Path, dict]:
    """Resolve ``root_id`` + ``relpath`` to ``(absolute_path, Root)``.

    Design §4.6 gates, in order; ``CodeAccessError`` on the first failure.
    ``write`` is unused in S1 beyond the ``ro``/``.git`` checks — it exists so
    S2's write endpoint shares this exact function.
    """
    roots = {r["id"]: r for r in _list_roots(session)}
    rid = (root_id or "session").strip() or "session"
    root = roots.get(rid)
    if root is None:
        raise CodeAccessError("unknown-root", f"Unknown workspace root: {root_id!r}")
    root_path = Path(root["path"]) if root["path"] else None
    if root_path is None or not root_path.is_dir():
        raise CodeAccessError("root-missing", f"Workspace directory does not exist: {root.get('name') or rid}")

    rel = _norm_rel(relpath)
    # Gate 2: relative only (an absolute path has no root to belong to).
    if rel.startswith("/") or Path(relpath or "").is_absolute():
        raise CodeAccessError("outside-root", "Only root-relative paths are accepted")
    # Gate 2+3: containment after resolving the path AND dereferencing
    # symlinks (realpath) — a link pointing outside the root is rejected even
    # though it lexically looked contained.
    root_real = Path(os.path.realpath(root_path))
    real = Path(os.path.realpath(root_path / rel)) if rel else root_real
    if not real.is_relative_to(root_real):
        raise CodeAccessError("outside-root", f"Path is outside the current workspace root: {relpath!r}")

    # Gate 4: permanent deny regions. Mirror builtin.py EXACTLY — base_dir is
    # the session workspace (a proper subdir of ~/.ginno), NOT primary_path:
    # primary_path lives outside ~/.ginno, so the exemption would never fire
    # and the session-workspace root would come back denied/empty.
    if _path_denied(real, session["workspace"], session["mount_roots"]):
        raise CodeAccessError("denied-path", f"Path is in a denied area: {relpath!r}")

    # Gate 5 (writes): an ``ro`` mount is a hard constraint, and nothing under
    # a ``.git`` is ever writable (even with "show all" on).
    if write:
        if root["access"] != "rw" or mount_access(real, session["mounts"]) == "ro":
            raise CodeAccessError("read-only-mount", "This directory is mounted read-only")
        if ".git" in _rel_parts(real, root_real):
            raise CodeAccessError("denied-path", ".git internals are not writable")
    return real, root


def _rel_parts(target: Path, root_real: Path) -> tuple[str, ...]:
    try:
        return target.relative_to(root_real).parts
    except ValueError:
        return ()


def _write_state(ctx: dict, target: Path, root: dict) -> tuple[bool, str | None]:
    """``(editable, readonly_reason)`` for a read, without raising."""
    if root["access"] != "rw" or mount_access(target, ctx["mounts"]) == "ro":
        return False, "read-only-mount"
    root_real = Path(os.path.realpath(root["path"]))
    if ".git" in _rel_parts(target, root_real):
        return False, "git-internal"
    return True, None


# ---- content helpers ------------------------------------------------------


def _decode(data: bytes) -> tuple[str, str]:
    """Decode with the S1 probe order; returns ``(text, encoding_used)``."""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


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


def _err(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}


# ---- endpoints ------------------------------------------------------------


@router.get("/api/code/roots")
async def code_roots(project_slug: str = "default", session_id: str = "") -> dict:
    """Every root the panel can browse: mounts (primary first) + session."""
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _err("unknown-root", f"Session not found or deleted: {session_id}")
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
        return _err("unknown-root", f"Session not found or deleted: {session_id}")
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
        return _err("unknown-root", f"Session not found or deleted: {session_id}")
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


# ---- git (S3) -------------------------------------------------------------

#: Wall-clock ceiling for any single git call. git is decoration: it may never
#: hold a request open (brief §1-3).
GIT_TIMEOUT_S = 10

#: Porcelain=v2 ``XY`` pair → the panel's single letter, in precedence order.
#: ``X`` is the index status and ``Y`` the worktree status; a pair like ``AM``
#: (staged add, then edited again) or ``.D`` is collapsed to the most
#: informative letter. ``D`` outranks everything — a file deleted in the
#: worktree is gone whatever the index says — then renames/copies/adds, then a
#: plain modification (``T``, a type change, reads as a modification).
_XY_PRECEDENCE = (
    ("D", "D"),
    ("R", "R"),
    ("C", "C"),
    ("A", "A"),
    ("U", "U"),
    ("M", "M"),
    ("T", "M"),
)


def _git_run(args: list[str], cwd: str) -> subprocess.CompletedProcess | None:
    """Run git and return ``None`` on ANY failure.

    A missing ``git`` binary (``FileNotFoundError``, an ``OSError``), a nonzero
    exit, or a timeout all collapse to ``None`` so every caller degrades
    silently. Nothing here may raise into a request handler.
    """
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, timeout=GIT_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _git_toplevel(root_path: Path) -> Path | None:
    """The repository toplevel containing ``root_path``, or ``None``.

    ``None`` covers "not a repository", "no ``git``", and "git too slow" — the
    three cases the panel must treat identically (brief §1-3).
    """
    r = _git_run(["rev-parse", "--show-toplevel"], str(root_path))
    if r is None or r.returncode != 0:
        return None
    top = r.stdout.decode("utf-8", "replace").strip()
    return Path(top) if top else None


def _root_offset(root_path: Path, toplevel: Path) -> str | None:
    """``root_path`` relative to the repository ``toplevel``.

    ``""`` when the root IS the toplevel, ``"a/b"`` when it is a subdirectory,
    ``None`` when it is not below the toplevel (never guess a mapping).
    """
    try:
        rel = os.path.relpath(os.path.realpath(root_path), os.path.realpath(toplevel))
    except (OSError, ValueError):
        return None
    if rel == ".":
        return ""
    rel = rel.replace(os.sep, "/")
    return None if rel.startswith("..") else rel


def _git_prefix(root_path: Path) -> str | None:
    """Toplevel-relative prefix of a root, or ``None`` when not in a repo."""
    top = _git_toplevel(root_path)
    return None if top is None else _root_offset(root_path, top)


def _xy_letter(xy: str) -> str:
    for ch, letter in _XY_PRECEDENCE:
        if ch in xy:
            return letter
    return "M"


def _parse_status(data: bytes) -> dict[str, str]:
    """Porcelain=v2 ``-z`` output → ``{toplevel-relative path: letter}``.

    Records are NUL-separated. Two shapes need care:

    * a renamed/copied record (type ``2``) carries the NEW path inside the
      record and its ORIGINAL path as the NEXT NUL-separated field, so the
      parser must swallow that extra token — otherwise every following record
      shifts by one and gets attributed to the wrong file;
    * paths may contain spaces, so a record is split into its fixed number of
      space-separated fields and only the remainder is taken as the path.
    """
    entries: dict[str, str] = {}
    fields = data.split(b"\0")
    i = 0
    while i < len(fields):
        rec = fields[i]
        i += 1
        if not rec:
            continue
        line = rec.decode("utf-8", "replace")
        kind = line[:1]
        if kind == "?":
            # Untracked → "new" in the tree (brief §3.2). A whole ignored or
            # untracked DIRECTORY arrives with a trailing slash; strip it so the
            # key matches the tree's own path spelling.
            entries[line[2:].rstrip("/")] = "A"
        elif kind == "!":
            entries[line[2:].rstrip("/")] = "!"
        elif kind == "1":
            # 1 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <path>
            parts = line.split(" ", 8)
            if len(parts) == 9:
                entries[parts[8]] = _xy_letter(parts[1])
        elif kind == "2":
            # 2 <XY> <sub> <mH> <mI> <mW> <hH> <hI> <X><score> <path>
            # followed by the ORIGINAL path as the next field (consumed here).
            parts = line.split(" ", 9)
            if len(parts) == 10:
                entries[parts[9]] = _xy_letter(parts[1])
            if i < len(fields):
                i += 1
        elif kind == "u":
            # u <XY> <sub> <m1> <m2> <m3> <mW> <h1> <h2> <h3> <path>
            parts = line.split(" ", 10)
            if len(parts) == 11:
                entries[parts[10]] = "U"
    return entries


def _relocate(entries: dict[str, str], offset: str) -> dict[str, str]:
    """Rewrite toplevel-relative keys as ROOT-relative, dropping the rest.

    This is the trap the brief calls out: ``git status`` speaks toplevel
    relative paths, while every other endpoint in this module (and the tree)
    speaks ROOT relative paths, so a root that is a subdirectory would otherwise
    decorate the wrong files (or nothing at all).
    """
    if not offset:
        return entries
    pfx = offset + "/"
    return {p[len(pfx):]: v for p, v in entries.items() if p.startswith(pfx)}


def _read_head(root_info: dict, rel: str, language: str) -> dict:
    """``git show HEAD:<toplevel-relative path>`` for the diff baseline.

    ``rel`` is ROOT relative, but ``git show`` resolves a ``rev:path`` from the
    repository TOPLEVEL — so a root below the toplevel needs its prefix, or the
    lookup misses every file (the S1 gap the brief asks to fix). A miss is
    ``absent``; a non-repo root degrades to the same thing rather than raising.
    """
    prefix = _git_prefix(Path(root_info["path"]))
    if prefix is None:
        return _err("absent", "Failed to read the HEAD version")
    git_path = f"{prefix}/{rel}" if prefix else rel
    r = _git_run(["show", f"HEAD:{git_path}"], root_info["path"])
    if r is None or r.returncode != 0 or not r.stdout:
        return _err("absent", "File does not exist in HEAD")
    data = r.stdout
    text, enc = _decode(data)
    return {
        "ok": True,
        "root": root_info["id"],
        "path": rel,
        "version": hashlib.sha1(data).hexdigest()[:16],
        "encoding": enc,
        "language": language,
        "editable": False,
        "readonly_reason": None,
        "text": text,
        "eof": True,
    }


# ---- write (S2) ------------------------------------------------------------


@router.put("/api/code/write")
async def code_write(req: dict) -> Response:
    """Save an open file (design §4.7-S2, brief §3.1).

    The request carries the ``base_version`` the buffer was read at. A mismatch
    means the file changed on disk since (the agent editing the same file is the
    expected case, not the exotic one), and the write is REFUSED with 409 plus
    the version currently on disk — never silently applied. That "announce, do
    not overwrite" rule is the whole point of the version token; it is also the
    only unrecoverable thing this panel could do (brief §1-2/§1-5).

    The old bytes are replaced atomically: a same-directory temp file, chmod-ed
    back to the original ``st_mode``, then ``os.replace``. Truncate-and-write
    would leave a half file behind on a crash, and a bare ``mkstemp`` rename
    would strip ``+x`` off a script and turn every ordinary 0644 file into 0600
    (brief §1-4).
    """
    body = req if isinstance(req, dict) else {}
    project_slug = str(body.get("project_slug") or "default")
    session_id = str(body.get("session_id") or "")
    root = str(body.get("root") or "session")
    path = str(body.get("path") or "")
    content = body.get("content")
    base_version = str(body.get("base_version") or "")
    # The client echoes the encoding `read` reported; defaulting to utf-8 is the
    # S1 read default too. Writing a GBK file back as UTF-8 would be silent
    # corruption (brief §1-3), so the caller's value is used verbatim.
    encoding = str(body.get("encoding") or "utf-8").strip() or "utf-8"

    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _err("unknown-root", f"Session not found or deleted: {session_id}")
    try:
        # The `ro` tier and the `.git` rule live in here — same fence as `read`,
        # deliberately not a second copy of it (design §4.6).
        target, _root_info = resolve_code_target(ctx, root, path, write=True)
    except CodeAccessError as e:
        return _err(e.code, e.message)

    if not isinstance(content, str):
        return _err("not-text", "content must be a string")

    # Distinguish "deleted out from under us" (absent — the panel should offer
    # to recreate, not to retry) from "that path is not a file" (not-text).
    if not target.exists():
        return _err("absent", f"File no longer exists: {path!r}")
    if not target.is_file():
        return _err("not-text", f"Not a file: {path!r}")
    try:
        st = target.stat()
    except OSError as e:
        return _err("not-text", f"Failed to stat file: {e}")

    # Version format must stay identical to `read`'s, or every save would look
    # like a conflict.
    disk_version = f"{st.st_size}:{st.st_mtime_ns}"
    if base_version != disk_version:
        return JSONResponse(
            {
                "ok": False,
                "code": "conflict",
                "version": disk_version,
                "message": "File was modified on disk; not saved",
            },
            status_code=409,
        )

    try:
        data = content.encode(encoding)
    except UnicodeEncodeError:
        # e.g. an emoji into GBK: the content simply has no representation in
        # the file's encoding. Say so instead of mangling it or swapping the
        # encoding behind the user's back.
        return _err("not-text", f"Content cannot be saved as {encoding} (contains characters the encoding can't represent)")
    except LookupError:
        return _err("not-text", f"Unsupported encoding: {encoding}")
    if len(data) > EDITABLE_MAX_BYTES:
        return _err("too-large", f"Content too large ({len(data) / 1048576:.1f} MB) to save")

    tmp: str | None = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp"
        )
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        # Before the rename, so the file never appears with mkstemp's 0600.
        os.chmod(tmp, st.st_mode)
        os.replace(tmp, target)
        tmp = None
    except OSError as e:
        return _err("not-text", f"Failed to write file: {e}")
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass

    # Re-stat rather than deriving the version from `data`: the returned version
    # must describe what is on disk NOW, and only stat knows that.
    try:
        st_after = target.stat()
    except OSError as e:
        return _err("not-text", f"Failed to stat file after write: {e}")
    return {"ok": True, "version": f"{st_after.st_size}:{st_after.st_mtime_ns}"}


# ---- raw bytes (for surfaces that cannot consume text) ---------------------

#: Ceiling for streaming a file's bytes whole. Same order of magnitude as the
#: read path's preview caps: past this we refuse rather than feed the webview a
#: payload it would choke on.
RAW_MAX_BYTES = 32 * 1024 * 1024

#: ext -> Content-Type for the raw stream. A whitelist on purpose: stdlib
#: ``mimetypes`` consults an OS database and varies by machine, and the one
#: thing an inline stream is for here is images. Keys mirror
#: ``files.extractors.IMAGE_EXTS``; anything else is served as
#: ``application/octet-stream`` with nosniff + an attachment disposition.
_RAW_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    # image/svg+xml so an <img> renders it inertly — SVG loaded through <img>
    # cannot run script against this origin. We deliberately do NOT offer an
    # inline/iframe surface (design §1.3 rules out HTML sandboxing).
    ".svg": "image/svg+xml",
}


@router.get("/api/code/raw")
async def code_raw(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    path: str = "",
) -> Response:
    """A file's bytes, for surfaces that cannot consume text (image preview).

    Runs the SAME fence as ``read`` (design §4.6) — a path the panel may not
    read is a path it may not stream either. The URL is cache-busted with the
    read response's ``version``, so the client builds it as
    ``/api/code/raw?...&v=<version>``.
    """
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _raw_error("unknown-root", f"Session not found or deleted: {session_id}")
    try:
        target, _root_info = resolve_code_target(ctx, root, path, write=False)
    except CodeAccessError as e:
        return _raw_error(e.code, e.message)
    if not target.is_file():
        return _raw_error("not-text", f"Not a file: {path!r}")
    try:
        size = target.stat().st_size
    except OSError as e:
        return _raw_error("not-text", f"Failed to read file: {e}")
    if size > RAW_MAX_BYTES:
        return _raw_error("too-large", f"File too large ({size / 1048576:.1f} MB) to preview")
    try:
        data = target.read_bytes()
    except OSError as e:
        return _raw_error("not-text", f"Failed to read file: {e}")

    mime = _RAW_MIME.get(target.suffix.lower())
    headers = {
        # A workspace file must never talk the webview into treating it as
        # markup: octet-stream stays octet-stream.
        "X-Content-Type-Options": "nosniff",
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if mime is None:
        headers["Content-Disposition"] = "attachment"
    return Response(
        content=data,
        media_type=mime or "application/octet-stream",
        headers=headers,
    )


def _raw_error(code: str, message: str) -> JSONResponse:
    """Failure as a non-2xx.

    ``raw`` is consumed by ``<img src>``, which cannot read a JSON body: a 200
    carrying ``{ok:false}`` would render as a corrupt image rather than as a
    failure. The code also travels in ``X-Ginno-Code`` so a caller that cares
    can say why.
    """
    status = {
        "unknown-root": 404,
        "root-missing": 404,
        "not-text": 404,
        "outside-root": 403,
        "denied-path": 403,
        "too-large": 413,
    }.get(code, 400)
    return JSONResponse(
        {"ok": False, "code": code, "message": message},
        status_code=status,
        headers={"X-Ginno-Code": code},
    )


# ---- git status (S3) -------------------------------------------------------

_EMPTY_GIT = {"ok": True, "is_repo": False, "toplevel": None, "branch": None, "entries": {}}


@router.get("/api/code/git")
async def code_git(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
) -> dict:
    """Working-tree git decoration for one root (design §4.2-5, brief §3.2).

    Best-effort by contract: a root that is not in a repository, a machine
    without ``git``, and a git call that times out ALL answer ``is_repo:false``
    with no entries and HTTP 200 — never an error. The panel must stay usable
    when git is unavailable (brief §1-3).

    ``entries`` keys are ROOT-relative posix paths. git reports them relative to
    the repository TOPLEVEL, so a root that is a subdirectory gets them
    relocated (``_relocate``) — the easiest thing here to get wrong.

    A nested repository INSIDE the root is deliberately not descended into: git
    itself reports such a directory as one untracked entry and v1 leaves its
    contents undecorated (brief §3.2).

    No server-side cache. The design floats a 2s cache (§4.1) to spare git on
    every tree expansion, but the client fetches this once per ROOT for the
    whole panel, so the cache would not save a call — it would only make the
    tree stale right after a save. The subprocess timeout is the real bound.
    """
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _err("unknown-root", f"Session not found or deleted: {session_id}")
    try:
        # Same fence as every other endpoint: an unreachable root gets the same
        # error here as it would from `list`/`read`.
        root_path, _root_info = resolve_code_target(ctx, root, "", write=False)
    except CodeAccessError as e:
        return _err(e.code, e.message)

    toplevel = _git_toplevel(root_path)
    offset = _root_offset(root_path, toplevel) if toplevel is not None else None
    if toplevel is None or offset is None:
        return dict(_EMPTY_GIT)

    # Run at the TOPLEVEL (not the root) so every path shares one frame of
    # reference and `_relocate` is the only place the offset is applied.
    r = _git_run(
        ["status", "--porcelain=v2", "-z", "--untracked-files=all", "--ignored=matching"],
        str(toplevel),
    )
    entries = (
        _relocate(_parse_status(r.stdout), offset)
        if r is not None and r.returncode == 0
        else {}
    )
    # Branch comes from the toplevel's own `.git/HEAD` (no subprocess), so it is
    # right even when the root sits several levels down or HEAD is detached.
    _is_repo, branch = _git_info(toplevel)
    return {
        "ok": True,
        "is_repo": True,
        "toplevel": str(toplevel),
        "branch": branch,
        "entries": entries,
    }


# ---- search (S4) -----------------------------------------------------------

#: Name search stops after visiting this many directory entries (files AND
#: dirs, including ones pruned as heavy/denied — the readdir slot was spent
#: either way). Same order as ``builtin.GLOB_MAX_DIRS`` so both walks degrade
#: alike (design §4.1: 扫 20000 项 / 返 50).
SEARCH_NAME_MAX_ENTRIES = 20000
#: Name search also stops on wall clock, mirroring ``grep_files``.
SEARCH_NAME_TIME_BUDGET_S = GLOB_TIME_BUDGET_S
#: Content search collects at most this many hits before stopping — the same
#: budget ``grep_files`` uses. ``limit`` only trims what is RETURNED: the scan
#: keeps collecting up to this cap so ``truncated`` can be honest when more
#: matches exist than the caller asked for (brief §1-2).
SEARCH_CONTENT_MAX_HITS = GLOB_MAX_HITS
#: Content search also bounds the walk by directory count and wall clock, the
#: two budgets ``grep_files`` already uses.
SEARCH_CONTENT_MAX_DIRS = GLOB_MAX_DIRS
SEARCH_CONTENT_TIME_BUDGET_S = GLOB_TIME_BUDGET_S
#: A file larger than this is not read: content search is for source trees, not
#: for multi-hundred-MB logs. Same ceiling the editor uses for editing.
SEARCH_MAX_FILE_BYTES = EDITABLE_MAX_BYTES
#: ``q`` ceiling in bytes — a megabyte-long query is a client bug, not a search.
SEARCH_MAX_QUERY_BYTES = 1000
#: Returned-hit page size (design §4.1: name search returns 50).
SEARCH_LIMIT_DEFAULT = 50
SEARCH_LIMIT_MAX = 200


def _subsequence(needle: str, hay: str) -> bool:
    """True when every char of ``needle`` occurs IN ORDER inside ``hay``.

    ``ch in it`` advances the character iterator, which is the whole algorithm:
    it fails as soon as a needed character is no longer ahead.
    """
    it = iter(hay)
    return all(ch in it for ch in needle)


def _name_rank(rel: str, q: str) -> int | None:
    """Match quality of a root-relative file path against a lowered query.

    ``0`` = query is a case-insensitive SUBSTRING of the basename (the common
    "I remember the file name" case), ``1`` = substring of the full path (the
    query spans a directory), ``2`` = SUBSEQUENCE of the path (typo-tolerant
    fuzzy), ``None`` = no match.

    Of the two matchers the brief allows (subsequence vs case-insensitive
    substring), this endpoint uses **subsequence**, because it backs ⌘P quick
    open: users type fragments and expect near misses to surface (``srch`` →
    ``search.py``), where a plain substring filter returns nothing for one
    dropped letter. Rank keeps the common case (a basename hit) on top so fuzzy
    noise never outranks an obvious answer.
    """
    low = rel.lower()
    base = low.rsplit("/", 1)[-1]
    if q in base:
        return 0
    if q in low:
        return 1
    return 2 if _subsequence(q, low) else None


def _search_name(
    root: Path, query: str, limit: int, ctx: dict
) -> tuple[list[str], int, bool]:
    """BFS ``root`` for files whose path matches ``query`` (design §4.2-4).

    Returns ``(paths, scanned, truncated)``: ``scanned`` counts directory
    entries VISITED (not matches), and ``truncated`` means the walk stopped
    early on the entry/time budget — more files may exist.

    The walk never descends into a ``HEAVY_DIRS`` directory and never follows or
    matches a symlink: a quick open must not spend its budget inside
    ``node_modules``, nor leave the root through a link. Directories are not
    returned — the wire ``Hit`` is just a path, and ⌘P opens a file.

    BFS (not ``os.walk``'s depth-first order) so that when the budget does run
    out, the entries nearest the root are the ones already seen — the useful
    half of the tree for a quick open.
    """
    q = query.lower()
    found: list[tuple[int, int, int, str, str]] = []
    scanned = 0
    truncated = False
    t0 = time.monotonic()
    base_dir = ctx["workspace"]
    mount_roots = ctx["mount_roots"]
    queue: deque[Path] = deque([root])
    while queue:
        if time.monotonic() - t0 > SEARCH_NAME_TIME_BUDGET_S:
            truncated = True
            break
        d = queue.popleft()
        try:
            children = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue  # unreadable dir: skip it, never fail the whole search
        for child in children:
            scanned += 1
            if scanned > SEARCH_NAME_MAX_ENTRIES:
                truncated = True
                break
            if child.is_symlink():
                continue
            if _path_denied(child, base_dir, mount_roots):
                continue
            try:
                if child.is_dir():
                    if child.name not in HEAVY_DIRS:
                        queue.append(child)
                    continue
                if not child.is_file():
                    continue
            except OSError:
                continue
            rel = child.relative_to(root).as_posix()
            rank = _name_rank(rel, q)
            if rank is not None:
                # Sort key: quality, then shallowest, then shortest, then name.
                found.append((rank, rel.count("/"), len(rel), rel.lower(), rel))
        if truncated:
            break
    found.sort()
    return [f[4] for f in found[:limit]], scanned, truncated


def _search_content(
    root: Path, rx: "re.Pattern[str]", limit: int, ctx: dict
) -> tuple[list[dict], int, bool]:
    """Walk ``root`` and match ``rx`` line by line (design §4.2-4).

    Mirrors ``tools.builtin.grep_files`` — ``os.walk`` + a per-file read + a
    per-line ``rx.search`` — with the ONE addition the design demands: the
    ``HEAVY_DIRS`` skip set is pruned on top of ``_path_denied``, because
    ``_path_denied`` does NOT exclude ``node_modules`` (it only guards
    ``~/.ssh`` / the keychain / ``~/.ginno``). Without that prune the 20000-dir
    budget is spent inside ``node_modules`` and the search returns noise.

    Files are decoded with this module's probe order (utf-8 → gbk → latin-1),
    so a GBK source still matches where ``grep_files``' utf-8-only read would
    silently miss it. Binaries (a NUL byte in the body) and files over
    ``SEARCH_MAX_FILE_BYTES`` are skipped; symlinks are never read (they can
    point outside the root, which this fence is scoped to).

    ``rx`` is case-SENSITIVE, exactly like ``grep_files``: ``q`` is a regex and
    the caller controls case with ``(?i)``. Returns ``(hits, scanned,
    truncated)``; ``scanned`` counts FILES read.
    """
    hits: list[dict] = []
    scanned = 0
    t0 = time.monotonic()
    dirs_seen = 0
    budget_out = False  # time/dir budget hit — the walk really stopped early
    capped = False  # hit budget hit
    base_dir = ctx["workspace"]
    mount_roots = ctx["mount_roots"]
    for r, dirs, files in os.walk(root):
        dirs_seen += 1
        if (
            dirs_seen > SEARCH_CONTENT_MAX_DIRS
            or time.monotonic() - t0 > SEARCH_CONTENT_TIME_BUDGET_S
        ):
            budget_out = True
            break
        # Prune BEFORE descending: heavy dirs first (the §1-1 fix), then the
        # permanent deny regions grep_files already prunes.
        dirs[:] = [
            d
            for d in dirs
            if d not in HEAVY_DIRS
            and not _path_denied(Path(r) / d, base_dir, mount_roots)
        ]
        for name in sorted(files):
            fp = Path(r) / name
            if fp.is_symlink() or _path_denied(fp, base_dir, mount_roots):
                continue
            scanned += 1
            try:
                if fp.stat().st_size > SEARCH_MAX_FILE_BYTES:
                    continue
                data = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in data:
                continue
            text, _enc = _decode(data)
            rel = fp.relative_to(root).as_posix()
            for i, line in enumerate(text.splitlines(), 1):
                if not rx.search(line):
                    continue
                hits.append({"path": rel, "line": i, "text": line})
                if len(hits) >= SEARCH_CONTENT_MAX_HITS:
                    capped = True
                    break
            if capped:
                break
        if capped:
            break
    # Honest truncation: the scan stopped early (budget) OR more matches were
    # found than are being returned. Either way the caller must be told.
    return hits[:limit], scanned, budget_out or capped or len(hits) > limit


@router.get("/api/code/search")
async def code_search(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    q: str = "",
    mode: str = "name",
    limit: int = SEARCH_LIMIT_DEFAULT,
) -> dict:
    """Search one root by name (⌘P) or content (⇧⌘F) (brief §3.1, design §4.6).

    Two rules this endpoint must not break:

    * **Never truncate silently.** ``truncated`` is set whenever the walk/scan
      stopped before covering the tree, and ``scanned`` says how much it did
      cover, so the panel can say "已扫描 N 个文件后停止，结果可能不完整" instead
      of presenting a partial result as complete. An exhausted budget is a
      *successful* response with ``truncated: true`` (the partial results are
      the useful half) — ``search-timeout`` is reserved for a hard failure and
      is not used here.
    * **No server-side cache.** The design floats one (§4.1), but the client
      calls this on demand and freshness matters right after a save or an agent
      edit, so a cache would add invalidation bugs for a call the user triggers
      explicitly (and the two budgets already bound the cost).
    """
    ctx = _session_ctx(project_slug, session_id)
    if ctx is None:
        return _err("unknown-root", f"Session not found or deleted: {session_id}")
    try:
        # Same fence as `list`/`read`: an unreachable root gets the same error
        # code here, and the walk stays inside the resolved root.
        target, _root_info = resolve_code_target(ctx, root, "", write=False)
    except CodeAccessError as e:
        return _err(e.code, e.message)

    m = (mode or "name").strip().lower()
    if m not in ("name", "content"):
        return _err("invalid-mode", f"Unknown search mode: {mode!r}")
    query = q or ""
    if not query.strip():
        return _err("invalid-query", "Search text must not be empty")
    if len(query.encode("utf-8")) > SEARCH_MAX_QUERY_BYTES:
        return _err("invalid-query", f"Search text too long (max {SEARCH_MAX_QUERY_BYTES} bytes)")

    # `limit` is a page size, not a budget: clamp rather than reject (an
    # out-of-range limit is a client bug, and 200 is a perfectly good answer).
    page = max(1, min(int(limit or SEARCH_LIMIT_DEFAULT), SEARCH_LIMIT_MAX))

    t0 = time.monotonic()
    if m == "name":
        paths, scanned, truncated = _search_name(target, query, page, ctx)
        hits: list[dict] = [{"path": p} for p in paths]
    else:
        try:
            rx = re.compile(query)
        except re.error as e:
            # A bad pattern is the caller's mistake, not a server fault: fail
            # with a code and copy, never a 500 (brief §3.1).
            return _err("invalid-regex", f"Invalid regular expression: {e}")
        hits, scanned, truncated = _search_content(target, rx, page, ctx)
    return {
        "ok": True,
        "mode": m,
        "hits": hits,
        "scanned": scanned,
        "truncated": truncated,
        "elapsed_ms": int((time.monotonic() - t0) * 1000),
    }