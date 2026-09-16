"""Automatic memory refinery: capture → pool → LLM draft → human review → MEMORY.md."""

from .pool import append_to_pool, read_pool, clear_pool, sanitize_for_memory, pool_count
from .summarize import (
    apply_draft,
    create_draft,
    discard_draft,
    draft_payload,
    has_draft,
    read_draft,
    remove_memory_lines,
)

__all__ = [
    "append_to_pool",
    "read_pool",
    "clear_pool",
    "sanitize_for_memory",
    "pool_count",
    "apply_draft",
    "create_draft",
    "discard_draft",
    "draft_payload",
    "has_draft",
    "read_draft",
    "remove_memory_lines",
]
