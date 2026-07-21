"""Automatic memory summarization: capture → pool → LLM distill → MEMORY.md."""

from .pool import append_to_pool, read_pool, clear_pool, sanitize_for_memory, pool_count
from .summarize import summarize_pool

__all__ = [
    "append_to_pool",
    "read_pool",
    "clear_pool",
    "sanitize_for_memory",
    "pool_count",
    "summarize_pool",
]
