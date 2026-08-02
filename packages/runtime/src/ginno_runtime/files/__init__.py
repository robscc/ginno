"""File handling subpackage — uploads, identity registry, content extraction.

Three layers:

- ``extractors`` — format → markdown/metadata (lazy deps, graceful degrade).
- ``registry``   — id ↔ path identity ledger per project (persisted JSON),
  the reactive touch-point for preview auto-refresh.
- ``preview``    — paginated preview payloads for the UI (spreadsheets) or
  full markdown (documents).
"""

from .registry import (
    FileEntry,
    get_by_id,
    get_registry,
    norm_path,
    reset_registries,
    subscribe,
    touch,
)

__all__ = [
    "FileEntry",
    "get_by_id",
    "get_registry",
    "norm_path",
    "reset_registries",
    "subscribe",
    "touch",
]
