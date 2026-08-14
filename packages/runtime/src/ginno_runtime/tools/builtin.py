"""Built-in tools: Read / Grep / Glob / Write / Edit / Bash.

The session workspace is BOUND into the tools at construction time
(``build_builtin_tools(workspace)`` — plan F1, unfrozen 2026-08 after the
skill-install incident: the model had no way to know the workspace, omitted
the param, and the tools fell back to the sidecar cwd ``/``). The model
never sees a ``workspace`` parameter: relative paths resolve against the
session workspace, absolute paths pass through unchanged.

Context folders (docs/context-folders-design.md, M0): a session may mount
additional directories (``context_dirs``). Effects:

* ``primary_path`` — one mounted dir may be the PRIMARY working dir: it
  becomes the base for relative paths and the bash cwd. Falls back to the
  session files dir when unset.
* ``ro`` mounts are a HARD write constraint (write_file / edit_file refuse),
  independent of ``bypass_permissions``. Reads/greps work everywhere mounted.
* Paths OUTSIDE the mounted set keep the pre-feature behaviour (M0 decision:
  status quo — the only fences are the hard-deny roots below).

Every tool returns an ``[error] ...`` string instead of raising, so a bad
path degrades to a tool result the agent can react to — never a dead turn
(the 2026-08 incident ended in ``OSError: [Errno 22]`` from a whole-disk
glob and killed the whole turn).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from langchain_core.tools import tool

from .. import paths

# A runaway glob (e.g. ``**`` under a huge tree) used to stream the entire
# filesystem listing into the context; cap it like grep_files does.
GLOB_MAX_HITS = 500


def _always_deny_roots() -> list[str]:
    """Directories NO file/shell tool may ever touch, regardless of workspace
    (credentials — never a legitimate work location)."""
    return [os.path.expanduser("~/.ssh"), os.path.expanduser("~/Library/Keychains")]


def _inside(child_real: str, parent_real: str) -> bool:
    return child_real == parent_real or child_real.startswith(parent_real + os.sep)


def _path_denied(p: Path, base_dir: Path | None, extra_roots: list[Path] | None = None) -> bool:
    """True when ``p`` sits in a protected region (master-plan §2.3 baseline).

    Layers:
    * ``_always_deny_roots`` (ssh / keychains) are denied unconditionally.
    * EXEMPT: the active session workspace ``base_dir`` AND any mounted
      context dir (``extra_roots``) — user-chosen work locations win over the
      blanket home-dir deny below.
    * the sidecar home (``paths.home()``, e.g. ~/.ginno) is denied otherwise:
      session workspaces live *inside* the home dir
      (``projects/<slug>/sessions/<id>/``), so without the exemption a
      blanket deny would break legitimate file tools, while checkpoints,
      settings and memory stay protected.
    """
    try:
        real = os.path.realpath(p)
    except OSError:
        real = str(p)
    for denied in _always_deny_roots():
        if _inside(real, denied):
            return True
    for root in extra_roots or []:
        try:
            root_real = os.path.realpath(root)
        except OSError:
            continue
        if _inside(real, root_real):
            return False  # explicitly mounted → reachable
    home = os.path.realpath(paths.home())
    if real == home or real.startswith(home + os.sep):
        if base_dir is not None:
            base_real = os.path.realpath(base_dir)
            # Exempt only a workspace that is a PROPER subdir of home (a real
            # session workspace). base_dir == home is the workflow cwd fallback
            # and must stay protected, else a cwd=home run reads all of ~/.ginno.
            if base_real != home and base_real.startswith(home + os.sep):
                if _inside(real, base_real):
                    return False  # inside the active session workspace → allowed
        return True
    return False


def _deny_msg(p: str) -> str:
    return f"[error] 路径 {p!r} 在拒绝访问区域内（运行时数据/凭据目录受保护）"


def _base(workspace: str | None) -> Path:
    return Path(workspace).expanduser() if workspace else Path.cwd()


def _ws(base: Path, p: str) -> Path:
    return (base / p).resolve() if not Path(p).is_absolute() else Path(p)


def build_builtin_tools(
    workspace: str | None = None,
    context_dirs: list[dict] | None = None,
    primary_path: str | None = None,
) -> list:
    """Build the six file/shell tools with ``workspace`` (and optionally the
    session's mounted context dirs) bound in.

    Called per session (create_session / _ensure_session / mount changes) so
    each session's graph carries tools scoped to its own dirs. Callers
    without a workspace (workflow engine, unit tests) get the process cwd
    fallback — the pre-F1 behaviour.

    ``context_dirs`` items are the serializable dicts from
    ``context_folders.resolve_session_dirs`` (path/access/missing); entries
    marked ``missing`` are ignored. ``primary_path`` (a mounted dir's path)
    switches the relative-path base + bash cwd to that dir.
    """
    ws_root = _base(workspace)

    # Mounts → [(resolved_path, access)] — most-specific match wins for
    # nested mounts (an rw dir inside an ro mount keeps its own tier).
    mounts: list[tuple[Path, str]] = []
    for d in context_dirs or []:
        if not isinstance(d, dict) or d.get("missing"):
            continue
        raw = (d.get("path") or "").strip()
        if not raw:
            continue
        try:
            rp = Path(raw).expanduser().resolve()
        except OSError:
            continue
        mounts.append((rp, "rw" if d.get("access") == "rw" else "ro"))

    base_dir = ws_root
    if primary_path:
        cand = Path(primary_path).expanduser()
        if cand.is_dir():
            base_dir = cand

    read_roots: list[Path] = [ws_root] + [p for p, _ in mounts]
    write_roots: list[Path] = [ws_root] + [p for p, a in mounts if a == "rw"]

    def _mount_access(p: Path) -> str | None:
        """Access tier of the MOST SPECIFIC mount containing ``p`` (or None)."""
        try:
            real = os.path.realpath(p)
        except OSError:
            return None
        best: str | None = None
        best_len = -1
        for mp, acc in mounts:
            r = str(mp)
            if _inside(real, r) and len(r) > best_len:
                best, best_len = acc, len(r)
        return best

    def _ro_write_msg(p: str) -> str:
        return (
            f"[error] 路径 {p!r} 位于只读挂载目录内，不可写入。"
            "只读目录仅供 read_file / grep_files / glob_files 参考；"
            "如需修改请让用户将其改为读写挂载。"
        )

    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file. Relative paths resolve against the session
        working directory. Returns contents or an ``[error]`` string."""
        try:
            p = _ws(base_dir, path)
            if _path_denied(p, base_dir, read_roots):
                return _deny_msg(path)
            return p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return f"[error] file not found: {path}"
        except OSError as e:
            return f"[error] cannot read {path}: {type(e).__name__}: {e}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write text content to a file (overwrite). Relative paths resolve
        against the session working directory.

        Never raises into the graph: an unwritable path (e.g. the knowledge raw dir
        resolving outside the workspace, a read-only mount, or a read-only
        parent) returns an ``[error]`` tool result so the agent can react,
        instead of crashing the whole turn.
        """
        p = _ws(base_dir, path)
        if _path_denied(p, base_dir, read_roots):
            return _deny_msg(path)
        if _mount_access(p) == "ro":
            return _ro_write_msg(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as e:
            return f"[error] cannot write {p}: {type(e).__name__}: {e}"
        return f"wrote {len(content)} bytes to {p}"

    def _resolve_search_root(root: str) -> Path | str:
        """Validate an explicit glob/grep root: it must sit inside the session
        workspace or a mounted dir. Returns the resolved Path or an error."""
        rp = _ws(base_dir, (root or "").strip())
        if not rp.is_dir():
            return f"[error] root 不是目录: {root}"
        try:
            real = os.path.realpath(rp)
        except OSError:
            real = str(rp)
        for rr in read_roots:
            try:
                rr_real = os.path.realpath(rr)
            except OSError:
                continue
            if _inside(real, rr_real):
                return rp
        avail = ", ".join(str(rr) for rr in read_roots)
        return f"[error] root {root!r} 不在本会话的可达目录内（工作目录/挂载目录：{avail}）"

    @tool
    def glob_files(pattern: str, root: str = "") -> str:
        """Glob files under the session working directory (recursive), or
        under an explicit ``root`` (must be the workspace or a mounted
        context dir — use ``root`` to search a mounted folder). Returns a
        newline-joined list of paths relative to the searched root, capped at
        500 matches. Never raises — a bad base dir or pattern yields an
        ``[error]`` string. Hard-denied dirs (runtime data / credentials) are
        pruned and never scanned (master-plan §2.3)."""
        import fnmatch

        try:
            search_root = base_dir
            if (root or "").strip():
                resolved = _resolve_search_root(root)
                if isinstance(resolved, str):
                    return resolved
                search_root = resolved
            if not search_root.is_dir():
                return f"[error] workspace is not a directory: {search_root}"
            hits: list[str] = []
            truncated = False
            try:
                for r, dirs, files in os.walk(search_root):
                    # Prune hard-denied directories so they are never traversed.
                    dirs[:] = [d for d in dirs if not _path_denied(Path(r) / d, base_dir, read_roots)]
                    for name in files:
                        fp = Path(r) / name
                        if _path_denied(fp, base_dir, read_roots):
                            continue
                        rel = str(fp.relative_to(search_root))
                        try:
                            if not fnmatch.fnmatch(rel, pattern) and not fnmatch.fnmatch(name, pattern):
                                continue
                        except Exception:
                            continue
                        hits.append(rel)
                        if len(hits) >= GLOB_MAX_HITS:
                            truncated = True
                            break
                    if truncated:
                        break
            except (OSError, ValueError) as e:
                if not hits:
                    return f"[error] glob failed: {type(e).__name__}: {e}"
            hits.sort()
            header = f"(root: {search_root})\n" if str(search_root) != str(base_dir) else ""
            out = header + "\n".join(hits)
            if truncated:
                out += f"\n... (truncated at {GLOB_MAX_HITS} matches)"
            return out or "(no matches)"
        except Exception as e:  # noqa: BLE001 — last-resort guard: never raise
            return f"[error] glob failed: {type(e).__name__}: {e}"

    @tool
    def grep_files(pattern: str, max_hits: int = 50, root: str = "") -> str:
        """Grep files under the session working directory (recursive), or
        under an explicit ``root`` (must be the workspace or a mounted
        context dir — use ``root`` to search a mounted folder). Returns
        file:line:match. Never raises — a bad pattern or base dir yields an
        ``[error]`` string."""
        import re

        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"[error] invalid regex {pattern!r}: {e}"
        try:
            search_root = base_dir
            if (root or "").strip():
                resolved = _resolve_search_root(root)
                if isinstance(resolved, str):
                    return resolved
                search_root = resolved
            if not search_root.is_dir():
                return f"[error] workspace is not a directory: {search_root}"
            out: list[str] = []
            header = f"(root: {search_root})" if str(search_root) != str(base_dir) else ""
            for r, dirs, files in os.walk(search_root):
                # Prune hard-denied directories (master-plan §2.3).
                dirs[:] = [d for d in dirs if not _path_denied(Path(r) / d, base_dir, read_roots)]
                for name in files:
                    f = Path(r) / name
                    if _path_denied(f, base_dir, read_roots):
                        continue
                    try:
                        for i, line in enumerate(
                            f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
                        ):
                            if rx.search(line):
                                out.append(f"{f.relative_to(search_root)}:{i}:{line}")
                                if len(out) >= max_hits:
                                    out.append("... (truncated)")
                                    return (header + "\n" if header else "") + "\n".join(out)
                    except Exception:
                        continue
            body = "\n".join(out) or "(no matches)"
            return (header + "\n" + body) if header else body
        except Exception as e:  # noqa: BLE001 — last-resort guard: never raise
            return f"[error] grep failed: {type(e).__name__}: {e}"

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace a unique occurrence of `old` with `new` in a file."""
        p = _ws(base_dir, path)
        if _path_denied(p, base_dir, read_roots):
            return _deny_msg(path)
        if _mount_access(p) == "ro":
            return _ro_write_msg(path)
        try:
            text = p.read_text(encoding="utf-8")
        except FileNotFoundError:
            return f"[error] file not found: {path}"
        except OSError as e:
            return f"[error] cannot read {path}: {type(e).__name__}: {e}"
        if text.count(old) > 1:
            return "[error] multiple matches"
        if text.count(old) == 0:
            return "[error] not found"
        try:
            p.write_text(text.replace(old, new, 1), encoding="utf-8")
        except OSError as e:
            return f"[error] cannot write {path}: {type(e).__name__}: {e}"
        return "ok"

    @tool
    def bash(command: str, timeout: int = 30) -> str:
        """Run a shell command with the session working directory (the primary
        context folder when one is set) as cwd. Returns stdout+stderr. Never
        raises."""
        # Weak hard-deny guard (master-plan §2.3 decision-6 interim): scan the
        # command's path-like tokens through the same _path_denied check
        # (which exempts the workspace + mounted dirs). Not a sandbox —
        # dynamically built paths bypass this; full bash policy is TBD.
        from pathlib import Path as _P

        for tok in command.split():
            tok = tok.strip("'\"`;|&")
            if not tok or not (tok.startswith("/") or tok.startswith("~")):
                continue
            if _path_denied(_P(tok).expanduser(), base_dir, read_roots):
                return _deny_msg(tok)
        cwd = str(base_dir) if base_dir.is_dir() else None
        try:
            r = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                timeout=timeout,
                capture_output=True,
                text=True,
            )
            return f"[exit {r.returncode}]\n{r.stdout}\n--- stderr ---\n{r.stderr}"
        except subprocess.TimeoutExpired:
            return f"[timeout after {timeout}s]"
        except OSError as e:
            return f"[error] cannot execute: {type(e).__name__}: {e}"

    return [read_file, write_file, glob_files, grep_files, edit_file, bash]
