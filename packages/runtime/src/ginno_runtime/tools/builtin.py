"""Built-in tools: Read / Grep / Glob / Write / Edit / Bash.

P0 scaffold: thin wrappers around simple operations, scoped to the
active workspace cwd. P1 will harden with permission checks, sandbox
backends (matching AgentScope's workspace model), and richer arg
validation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from langchain_core.tools import tool


def _ws(workspace: str | None, p: str) -> Path:
    base = Path(workspace or Path.cwd()).expanduser()
    return (base / p).resolve() if not Path(p).is_absolute() else Path(p)


@tool
def read_file(path: str, workspace: str | None = None) -> str:
    """Read a UTF-8 text file. Returns contents or error string."""
    try:
        return _ws(workspace, path).read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return f"[error] file not found: {path}"


@tool
def write_file(path: str, content: str, workspace: str | None = None) -> str:
    """Write text content to a file (overwrite)."""
    p = _ws(workspace, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return f"wrote {len(content)} bytes to {p}"


@tool
def glob_files(pattern: str, workspace: str | None = None) -> str:
    """Glob files under workspace. Returns newline-joined list."""
    base = Path(workspace or Path.cwd()).expanduser()
    hits = sorted(str(p.relative_to(base)) for p in base.rglob(pattern) if p.is_file())
    return "\n".join(hits) or "(no matches)"


@tool
def grep_files(pattern: str, workspace: str | None = None, max_hits: int = 50) -> str:
    """Grep files under workspace (recursive). Returns file:line:match."""
    import re

    rx = re.compile(pattern)
    base = Path(workspace or Path.cwd()).expanduser()
    out: list[str] = []
    for f in base.rglob("*"):
        if not f.is_file():
            continue
        try:
            for i, line in enumerate(f.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f.relative_to(base)}:{i}:{line}")
                    if len(out) >= max_hits:
                        out.append("... (truncated)")
                        return "\n".join(out)
        except Exception:
            continue
    return "\n".join(out) or "(no matches)"


@tool
def edit_file(path: str, old: str, new: str, workspace: str | None = None) -> str:
    """Replace a unique occurrence of `old` with `new` in a file."""
    p = _ws(workspace, path)
    text = p.read_text(encoding="utf-8")
    if text.count(old) > 1:
        return "[error] multiple matches"
    if text.count(old) == 0:
        return "[error] not found"
    p.write_text(text.replace(old, new, 1), encoding="utf-8")
    return "ok"


@tool
def bash(command: str, workspace: str | None = None, timeout: int = 30) -> str:
    """Run a shell command in workspace cwd. Returns stdout+stderr."""
    try:
        r = subprocess.run(
            command,
            shell=True,
            cwd=workspace or None,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        return f"[exit {r.returncode}]\n{r.stdout}\n--- stderr ---\n{r.stderr}"
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"


def build_builtin_tools() -> list:
    return [read_file, write_file, glob_files, grep_files, edit_file, bash]
