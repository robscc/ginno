"""Code panel — shared base: constants, error shape, router, session context, fence.

Split from the former single-module ``api/code.py`` (重构批次 2-2 绞杀者拆分);
面板总述见 ``api/code/__init__.py``。本文件持有跨子模块共享的顶层常量、
``CodeAccessError``、``router`` 本体、wire 错误工具、session 上下文解析、
§4.6 fence 与共享解码;代码体从原文件逐字迁移,语义零变化。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from ... import context_folders as cf
from ... import paths
from ...server_shared import _SESSIONS
from ...session_meta import _find_meta
from ...tools.builtin import _path_denied, mount_access, resolve_mounts

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
# Content cap for the live-diff endpoint: beyond it the current content is not
# returned (the frontend falls back to a whole-file view). Matches the
# baseline's own MAX_FILE_BYTES so both sides of the diff share one bound.
MAX_DIFF_BYTES = 2 * 1024 * 1024
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



# ---- shared decode + wire errors -----------------------------------------
def _decode(data: bytes) -> tuple[str, str]:
    """Decode with the S1 probe order; returns ``(text, encoding_used)``."""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1", errors="replace"), "latin-1"


def _err(code: str, message: str) -> dict:
    return {"ok": False, "code": code, "message": message}


def _unknown_session(session_id: str) -> JSONResponse:
    """400 (no session given) / 404 (session unknown) in the JSON error shape.

    These endpoints used to answer 200 ``{"ok": false, "code": "unknown-root"}``,
    so clients could not tell a dead session from a readable empty file."""
    return JSONResponse(
        status_code=400 if not (session_id or "").strip() else 404,
        content=_err("unknown-root", f"Session not found or deleted: {session_id}"),
    )

