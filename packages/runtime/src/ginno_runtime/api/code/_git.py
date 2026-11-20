"""Code panel — S3 git decoration: status/numstat endpoints + HEAD blob helper.

Split from the former single-module ``api/code.py``;面板总述见
``api/code/__init__.py``。git 永远只是装饰:任何失败(无 git/非仓库/超时)
都静默降级,绝不把请求挂死或报错(brief §1-3)。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

from ._common import (
    CodeAccessError,
    _decode,
    _err,
    _git_info,
    _session_ctx,
    _unknown_session,
    resolve_code_target,
    router,
)


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



# ---- git status (S3) -------------------------------------------------------

_EMPTY_GIT = {"ok": True, "is_repo": False, "toplevel": None, "branch": None, "entries": {}}


@router.get("/api/code/git")
async def code_git(
    project_slug: str = "default",
    session_id: str = "",
    root: str = "session",
    numstat: int = 0,
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
        return _unknown_session(session_id)
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
    resp = {
        "ok": True,
        "is_repo": True,
        "toplevel": str(toplevel),
        "branch": branch,
        "entries": entries,
    }
    if numstat:
        # P2 (2026-11-04): per-file add/del counts, best-effort. Tracked
        # changes only — untracked files have no HEAD counterpart; the flag
        # tells the frontend not to expect them here. Any git failure or
        # timeout simply omits the key (never an error — same contract as the
        # rest of this endpoint).
        ns = _git_numstat(toplevel, offset)
        if ns is not None:
            resp["numstat"] = {"tracked_only": True, "files": ns}
    return resp


def _git_numstat(toplevel: Path, offset: str | None) -> dict[str, dict] | None:
    """``{path: {add, del}}`` for ``git diff HEAD --numstat``, ROOT-relative.

    ``None`` on any git failure/timeout (caller omits the key). Binary files
    report ``add``/``del`` as ``-`` in porcelain — kept as 0 with
    ``binary: True`` so the key shape stays uniform for the frontend. Paths go
    through the same ``_relocate`` offset as status entries so both maps share
    one frame of reference.
    """
    r = _git_run(["diff", "HEAD", "--numstat"], str(toplevel))
    if r is None or r.returncode != 0:
        return None
    files: dict[str, dict] = {}
    text = r.stdout.decode("utf-8", "replace")
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_s, del_s, path = parts[0], parts[1], "\t".join(parts[2:])
        entry: dict = {"add": 0, "del": 0, "binary": False}
        try:
            entry["add"] = int(add_s)
            entry["del"] = int(del_s)
        except ValueError:
            entry["binary"] = True  # numstat prints "-" for binary files
        files[path] = entry
    return _relocate_numstat(files, offset) if offset else files


def _relocate_numstat(files: dict[str, dict], offset: str) -> dict[str, dict]:
    """Rewrite toplevel-relative keys as ROOT-relative (same rule as
    ``_relocate`` for status entries)."""
    pfx = offset + "/"
    return {p[len(pfx):]: e for p, e in files.items() if p.startswith(pfx)}

