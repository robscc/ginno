"""Auto-discovered local project roots.

The 2026-09-17 incident: the user asked an agent to scaffold a repo, the agent
wrote it with ABSOLUTE paths to ``~/workspace/dev/claude-agent-team`` (never
mounted as a context folder), and the next turn asked to "导入 skill". Ginno's
only known skill locations were its own two dirs, so the model installed into
``~/.ginno/skills`` — while the user meant the repo's ``.claude/skills``. The
model could not see that a project even existed.

This module makes a project *visible* the moment a file/shell tool touches it:
resolve the directory the tool acted on, walk up to the nearest project root,
and remember it for the session. The registry is a sibling of the world-state
baseline (``<session>.projects.json``), so it dies with the session's other
state.

Deliberately NOT a loader: discovering a repo does NOT pull its ``.claude/``
contents into Ginno. Mounted folders grant file access only (see
``context_folders`` module docstring) and the same boundary holds here — the
model may *see* that ``<repo>/.claude/skills`` exists and install into it when
the user asks, but Ginno never executes or indexes what is in there.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from . import paths

# Markers that make a directory a project root. Nearest hit wins.
MARKERS = (
    ".git",
    ".claude",
    "CLAUDE.md",
    "AGENTS.md",
    "GINNO.md",
    "package.json",
    "pyproject.toml",
    "Cargo.toml",
    "go.mod",
    "pnpm-workspace.yaml",
)

# Subset meaning "a Claude-style project" — those get the same-turn note in the
# tool result, because that is exactly the ambiguity that burned us.
CLAUDE_MARKERS = (".claude", "CLAUDE.md")

MAX_DEPTH = 6
MAX_ROOTS = 5

# Directories that are never project roots, however they are reached. Walking
# up from a file under these would otherwise land on "/" or on Ginno's own
# state tree and record the whole home dir as a "project".
_SKIP_ROOTS = ("/", "/tmp", "/var", "/private", "/etc", "/usr", "/opt")


def registry_path(slug: str, session_id: str) -> Path:
    return paths.project_sessions_dir(slug) / f"{session_id}.projects.json"


def claude_skills_dir(root: Path | str) -> Path:
    """Where a repo's Claude-Code skills live (created on demand by install)."""
    return Path(root) / ".claude" / "skills"


def _is_under(p: Path, root: Path) -> bool:
    return p == root or root in p.parents


def _count_claude_skills(root: Path) -> int:
    d = claude_skills_dir(root)
    if not d.is_dir():
        return 0
    try:
        return sum(1 for c in d.iterdir() if c.is_dir() and (c / "SKILL.md").is_file())
    except OSError:
        return 0


def detect_root(raw: str | Path) -> tuple[Path, list[str]] | None:
    """Nearest ancestor of ``raw`` that carries any marker, or None.

    Returns ``(root, markers)``. Refuses anything that cannot be a user
    project: relative paths, an empty path, Ginno's own home tree, and the
    system roots in ``_SKIP_ROOTS``.

    The path itself need not exist yet: bash is scanned for path-like tokens
    BEFORE it runs, so ``mkdir -p ~/work/new-repo`` is exactly the moment a
    project is being born. A not-yet-created dir simply has no markers, and
    the walk continues up — which is the right answer either way.
    """
    try:
        p = Path(raw).expanduser()
    except (TypeError, ValueError):
        return None
    if not str(raw).strip() or not p.is_absolute():
        return None
    try:
        p = p.resolve()
    except (OSError, RuntimeError):
        return None
    try:
        if p.is_file():
            p = p.parent
    except OSError:
        return None

    home = paths.home().resolve()
    for cand, depth in zip([p, *p.parents][: MAX_DEPTH + 1], range(MAX_DEPTH + 1), strict=False):
        if str(cand) in _SKIP_ROOTS or cand == cand.parent:
            return None
        # Ginno's own tree (skills dir, session workspaces, project metadata)
        # is never "the user's project".
        if cand == home or _is_under(cand, home):
            return None
        if depth > MAX_DEPTH:
            return None
        hits = [m for m in MARKERS if (cand / m).exists()]
        if hits:
            return cand, hits
    return None


def _load(slug: str, session_id: str) -> list[dict]:
    p = registry_path(slug, session_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return []
    roots = data.get("roots")
    return roots if isinstance(roots, list) else []


def _save(slug: str, session_id: str, roots: list[dict]) -> None:
    p = registry_path(slug, session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + f".tmp{secrets.token_hex(3)}")
    tmp.write_text(
        json.dumps({"roots": roots}, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    os.replace(tmp, p)


def known(slug: str, session_id: str) -> list[dict]:
    """Recorded project roots, newest first."""
    if not (slug and session_id):
        return []
    return list(reversed(_load(slug, session_id)))


def record(slug: str, session_id: str, raw: str | Path) -> dict | None:
    """Remember the project root containing ``raw``. Entry only when NEW.

    The return value is the once-only signal the caller uses to add a note to
    the tool result — an already-known root must not re-announce itself on
    every single file read.
    """
    if not (slug and session_id):
        return None
    hit = detect_root(raw)
    if not hit:
        return None
    root, markers = hit
    key = str(root)

    roots = _load(slug, session_id)
    for e in roots:
        if e.get("path") == key:
            return None

    entry = {
        "path": key,
        "name": root.name or key,
        "markers": markers,
        "has_claude": any(m in CLAUDE_MARKERS for m in markers),
        "claude_skills": _count_claude_skills(root),
    }
    roots.append(entry)
    # Oldest out — a long session touching many repos must not grow the
    # per-turn context without bound.
    if len(roots) > MAX_ROOTS:
        roots = roots[-MAX_ROOTS:]
    try:
        _save(slug, session_id, roots)
    except OSError:
        return None
    return entry


def forget(slug: str, session_id: str) -> None:
    """Drop the registry (session deletion / test cleanup)."""
    try:
        registry_path(slug, session_id).unlink(missing_ok=True)
    except OSError:
        pass