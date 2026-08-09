"""Built-in tools: Read / Grep / Glob / Write / Edit / Bash.

The session workspace is BOUND into the tools at construction time
(``build_builtin_tools(workspace)`` — plan F1, unfrozen 2026-08 after the
skill-install incident: the model had no way to know the workspace, omitted
the param, and the tools fell back to the sidecar cwd ``/``). The model
never sees a ``workspace`` parameter: relative paths resolve against the
session workspace, absolute paths pass through unchanged.

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


def _path_denied(p: Path, base_dir: Path | None) -> bool:
    """True when ``p`` sits in a protected region (master-plan §2.3 baseline).

    Two layers:
    * ``_always_deny_roots`` (ssh / keychains) are denied unconditionally.
    * the sidecar home (``paths.home()``, e.g. ~/.ginno) is denied, EXCEPT the
      active session workspace ``base_dir`` — session workspaces live *inside*
      the home dir (``projects/<slug>/sessions/<id>/``), so a blanket deny would
      break legitimate file tools. The workspace exemption keeps them working
      while still blocking other home subdirs (checkpoints, settings, memory…).
    """
    try:
        real = os.path.realpath(p)
    except OSError:
        real = str(p)
    for denied in _always_deny_roots():
        if real == denied or real.startswith(denied + os.sep):
            return True
    home = os.path.realpath(paths.home())
    if real == home or real.startswith(home + os.sep):
        if base_dir is not None:
            base_real = os.path.realpath(base_dir)
            # Exempt only a workspace that is a PROPER subdir of home (a real
            # session workspace). base_dir == home is the workflow cwd fallback
            # and must stay protected, else a cwd=home run reads all of ~/.ginno.
            if base_real != home and base_real.startswith(home + os.sep):
                if real == base_real or real.startswith(base_real + os.sep):
                    return False  # inside the active session workspace → allowed
        return True
    return False


def _deny_msg(p: str) -> str:
    return f"[error] 路径 {p!r} 在拒绝访问区域内（运行时数据/凭据目录受保护）"


def _base(workspace: str | None) -> Path:
    return Path(workspace).expanduser() if workspace else Path.cwd()


def _ws(base: Path, p: str) -> Path:
    return (base / p).resolve() if not Path(p).is_absolute() else Path(p)


def build_builtin_tools(workspace: str | None = None) -> list:
    """Build the six file/shell tools with ``workspace`` bound in.

    Called per session (create_session / _ensure_session) so each session's
    graph carries tools scoped to its own files dir. Callers without a
    workspace (workflow engine, unit tests) get the process cwd fallback —
    the pre-F1 behaviour.
    """
    base_dir = _base(workspace)

    @tool
    def read_file(path: str) -> str:
        """Read a UTF-8 text file. Relative paths resolve against the session
        workspace. Returns contents or an ``[error]`` string."""
        try:
            p = _ws(base_dir, path)
            if _path_denied(p, base_dir):
                return _deny_msg(path)
            return p.read_text(encoding="utf-8", errors="replace")
        except FileNotFoundError:
            return f"[error] file not found: {path}"
        except OSError as e:
            return f"[error] cannot read {path}: {type(e).__name__}: {e}"

    @tool
    def write_file(path: str, content: str) -> str:
        """Write text content to a file (overwrite). Relative paths resolve
        against the session workspace.

        Never raises into the graph: an unwritable path (e.g. the knowledge raw dir
        resolving outside the workspace, or a read-only parent) returns an ``[error]``
        tool result so the agent can react, instead of crashing the whole turn.
        """
        p = _ws(base_dir, path)
        if _path_denied(p, base_dir):
            return _deny_msg(path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        except OSError as e:
            return f"[error] cannot write {p}: {type(e).__name__}: {e}"
        return f"wrote {len(content)} bytes to {p}"

    @tool
    def glob_files(pattern: str) -> str:
        """Glob files under the session workspace (recursive). Returns a
        newline-joined list of workspace-relative paths, capped at 500
        matches. Never raises — a bad base dir or pattern yields an
        ``[error]`` string. Hard-denied dirs (runtime data / credentials) are
        pruned and never scanned (master-plan §2.3)."""
        import fnmatch

        try:
            if not base_dir.is_dir():
                return f"[error] workspace is not a directory: {base_dir}"
            hits: list[str] = []
            truncated = False
            try:
                for root, dirs, files in os.walk(base_dir):
                    # Prune hard-denied directories so they are never traversed.
                    dirs[:] = [d for d in dirs if not _path_denied(Path(root) / d, base_dir)]
                    for name in files:
                        fp = Path(root) / name
                        if _path_denied(fp, base_dir):
                            continue
                        rel = str(fp.relative_to(base_dir))
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
            out = "\n".join(hits)
            if truncated:
                out += f"\n... (truncated at {GLOB_MAX_HITS} matches)"
            return out or "(no matches)"
        except Exception as e:  # noqa: BLE001 — last-resort guard: never raise
            return f"[error] glob failed: {type(e).__name__}: {e}"

    @tool
    def grep_files(pattern: str, max_hits: int = 50) -> str:
        """Grep files under the session workspace (recursive). Returns
        file:line:match. Never raises — a bad pattern or base dir yields an
        ``[error]`` string."""
        import re

        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"[error] invalid regex {pattern!r}: {e}"
        try:
            if not base_dir.is_dir():
                return f"[error] workspace is not a directory: {base_dir}"
            out: list[str] = []
            for root, dirs, files in os.walk(base_dir):
                # Prune hard-denied directories (master-plan §2.3).
                dirs[:] = [d for d in dirs if not _path_denied(Path(root) / d, base_dir)]
                for name in files:
                    f = Path(root) / name
                    if _path_denied(f, base_dir):
                        continue
                    try:
                        for i, line in enumerate(
                            f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
                        ):
                            if rx.search(line):
                                out.append(f"{f.relative_to(base_dir)}:{i}:{line}")
                                if len(out) >= max_hits:
                                    out.append("... (truncated)")
                                    return "\n".join(out)
                    except Exception:
                        continue
            return "\n".join(out) or "(no matches)"
        except Exception as e:  # noqa: BLE001 — last-resort guard: never raise
            return f"[error] grep failed: {type(e).__name__}: {e}"

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace a unique occurrence of `old` with `new` in a file."""
        p = _ws(base_dir, path)
        if _path_denied(p, base_dir):
            return _deny_msg(path)
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
        """Run a shell command with the session workspace as cwd. Returns
        stdout+stderr. Never raises."""
        # Weak hard-deny guard (master-plan §2.3 decision-6 interim): scan the
        # command's path-like tokens through the same _path_denied check (which
        # exempts the active workspace). Not a sandbox — dynamically built paths
        # bypass this; full bash policy is TBD.
        from pathlib import Path as _P

        for tok in command.split():
            tok = tok.strip("'\"`;|&")
            if not tok or not (tok.startswith("/") or tok.startswith("~")):
                continue
            if _path_denied(_P(tok).expanduser(), base_dir):
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
