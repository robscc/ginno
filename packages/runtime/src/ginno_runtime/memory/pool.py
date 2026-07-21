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

# Patterns to strip from captured text (injection defense)
_INJECTION_PATTERNS = [
    re.compile(r"</?\s*injected_(wiki|memory)\s*>", re.IGNORECASE),
    re.compile(r"</?\s*system_prompt\s*>", re.IGNORECASE),
    re.compile(r"</?\s*instruction_hierarchy\s*>", re.IGNORECASE),
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


def append_to_pool(session_id: str, agent_id: str | None, text: str) -> None:
    """Append a sanitized assistant turn to the pool."""
    sanitized = sanitize_for_memory(text)
    if not sanitized:
        return
    entry = {
        "session_id": session_id,
        "agent_id": agent_id,
        "timestamp": time.time(),
        "content": sanitized,
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


def clear_pool() -> None:
    """Delete all pool files after summarization."""
    pool_dir = paths.memory_pool_dir()
    if not pool_dir.exists():
        return
    for f in pool_dir.glob("*.jsonl"):
        try:
            f.unlink()
        except OSError:
            continue


def pool_count() -> int:
    """Count pool entries (for UI display)."""
    return len(read_pool())
