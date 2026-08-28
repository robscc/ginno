"""Code-generated image surfacing (inline-images design).

When the agent runs code via the ``bash`` tool that writes image files into
the session workspace (e.g. ``matplotlib.savefig``), we want those pictures to
appear inline in the chat bubble. This module holds the two shared primitives:

- **snapshot / diff** — ``bash`` snapshots the workspace's image files before
  running and diffs after, to find newly created/updated images without
  relying on the model to self-report.
- **marker** — a compact machine-readable trailer appended to the bash tool
  output (and thus persisted in the ToolMessage). The WS layer parses it to
  register + broadcast the images; the agent node lifts the paths into the
  next AIMessage's ``additional_kwargs["ginno_images"]`` as a durable anchor
  that survives microcompact (which clears old ToolMessage bodies); the
  history builder reads the anchor to re-emit ``image`` blocks on replay.

The marker is never shown to the user (stripped from tool bubbles) and never
sent to the model (stripped from the send-only LLM copy) — images are
display-only.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from .extractors import IMAGE_EXTS

#: Marker wrapper. Absolute paths are stored as a JSON array so unusual
#: filenames (spaces / CJK) round-trip safely.
_MARKER_RE = re.compile(r"<!--ginno-images:(\[.*?\])-->", re.S)

#: Max files scanned per snapshot so a pathological workspace can't stall bash.
_SCAN_CAP = 20000


def snapshot_images(base_dir: Path) -> dict[str, int]:
    """Map ``{absolute_path: mtime_ns}`` for every image file under base_dir.

    Skips hidden directories. Returns an empty dict on any OS error or if the
    scan exceeds ``_SCAN_CAP`` files (defensive cap, not an error).
    """
    out: dict[str, int] = {}
    if not base_dir.is_dir():
        return out
    count = 0
    try:
        for root, dirs, files in os.walk(base_dir):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for f in files:
                if Path(f).suffix.lower() not in IMAGE_EXTS:
                    continue
                count += 1
                if count > _SCAN_CAP:
                    return {}
                p = Path(root) / f
                try:
                    out[str(p)] = p.stat().st_mtime_ns
                except OSError:
                    pass
    except OSError:
        return {}
    return out


def diff_images(before: dict[str, int], after: dict[str, int]) -> list[str]:
    """Absolute paths of images created or modified between two snapshots."""
    return sorted(p for p, mt in after.items() if before.get(p) != mt)


def encode_images_marker(paths: list[str]) -> str:
    """Build the marker trailer for a list of absolute image paths."""
    return "<!--ginno-images:" + json.dumps(list(paths), ensure_ascii=False) + "-->"


def parse_images_marker(text: str) -> list[str]:
    """Extract absolute image paths from a marker, or ``[]`` if none/invalid."""
    if not isinstance(text, str) or "<!--ginno-images:" not in text:
        return []
    m = _MARKER_RE.search(text)
    if not m:
        return []
    try:
        paths = json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(paths, list):
        return []
    return [p for p in paths if isinstance(p, str) and p]


def strip_images_marker(text: str) -> str:
    """Remove any marker (and a preceding trailing newline) from tool text."""
    if not isinstance(text, str) or "<!--ginno-images:" not in text:
        return text
    return _MARKER_RE.sub("", text).rstrip()
