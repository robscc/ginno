"""Memory pool: append-only buffer of sanitized assistant turns.

Each turn's assistant text (after sanitization) is appended as a JSON line to
`~/.ginno/memory/pool/<timestamp>.jsonl`. The summarizer reads all pool files,
merges with existing MEMORY.md via LLM, writes back, then clears the pool.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from .. import paths
from ..knowledge.injection import _INJECTION_PATTERNS as _CANONICAL_PATTERNS

# Patterns to strip from captured text (injection defense). Start from the
# canonical wrapper list (shared with knowledge/injection.py so the retrieval
# and capture sanitize paths never drift), then add the prose phrasing that
# only matters on the capture path.
_INJECTION_PATTERNS = list(_CANONICAL_PATTERNS) + [
    re.compile(r"ignore\s+(previous|all)\s+instructions", re.IGNORECASE),
]


def sanitize_for_memory(text: str) -> str:
    """Strip injection-like patterns before storing in pool."""
    out = text
    for rx in _INJECTION_PATTERNS:
        out = rx.sub("", out)
    return out.strip()


def _pool_dir() -> Path:
    d = paths.memory_pool_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def append_to_pool(
    session_id: str, agent_id: str | None, text: str, cited: bool = False
) -> None:
    """Append a sanitized assistant turn to the pool.

    ``cited`` marks turns whose citations were verified against registered
    sources — a quality signal the distiller weights when extracting knowledge
    (evidence over hearsay).
    """
    sanitized = sanitize_for_memory(text)
    if not sanitized:
        return
    entry = {
        "session_id": session_id,
        "agent_id": agent_id,
        "timestamp": time.time(),
        "content": sanitized,
        "cited": cited,
    }
    pool_file = _pool_dir() / f"{int(time.time() * 1000)}.jsonl"
    with open(pool_file, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_pool() -> list[dict[str, Any]]:
    """Read all pool entries (sorted by timestamp)."""
    entries: list[dict[str, Any]] = []
    pool_dir = paths.memory_pool_dir()
    if not pool_dir.exists():
        return entries
    for f in sorted(pool_dir.glob("*.jsonl")):
        try:
            for line in f.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    entries.append(json.loads(line))
        except (json.JSONDecodeError, OSError):
            continue
    return entries


def clear_pool(before_ts: float | None = None) -> None:
    """Delete pool files after summarization.

    With ``before_ts`` (the draft's pool cutoff), only entries captured at or
    before that timestamp are removed — turns that arrived while the draft was
    pending review survive and distill next round.
    """
    pool_dir = paths.memory_pool_dir()
    if not pool_dir.exists():
        return
    for f in pool_dir.glob("*.jsonl"):
        try:
            if before_ts is None:
                f.unlink()
                continue
            kept: list[str] = []
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    kept.append(line)  # keep undecodable lines untouched
                    continue
                if float(entry.get("timestamp") or 0) > before_ts:
                    kept.append(line)
            if kept:
                f.write_text("\n".join(kept) + "\n", encoding="utf-8")
            else:
                f.unlink()
        except OSError:
            continue


def pool_count() -> int:
    """Count pool entries (for UI display)."""
    return len(read_pool())
