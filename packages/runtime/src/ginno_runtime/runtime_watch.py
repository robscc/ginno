"""Runtime-update detection (P0-2).

The desktop app ships a frozen sidecar inside ``Ginno.app``. ``make app``
replaces that bundle while an old process may still be running — the new file
is on disk, the old code is in memory, and PyInstaller's per-extract archive
reads then fail with ``zlib.error`` (CLAUDE.md 已知故障模式 #1). The user sees
"every turn suddenly broken" with no hint.

This module answers one question, cheaply and safely: was the running binary
replaced after this process started? ``/api/health`` exposes the verdict so
the UI can tell the user to restart. Detection is advisory — it never blocks a
request and never tries to reload anything.
"""

from __future__ import annotations

import logging
import os
import sys
import time

_log = logging.getLogger("ginno.runtime_watch")

# Captured at import time, which happens before the server starts serving.
PROCESS_START = time.time()

_CACHE_TTL_S = 30.0
_cache: dict = {"at": 0.0, "stale": False, "mtime": None}


def _bundle_path() -> str | None:
    """The executable PyInstaller extracts from (None outside a frozen app)."""
    if not getattr(sys, "frozen", False):
        return None
    exe = sys.executable or ""
    return exe if exe and os.path.isfile(exe) else None


def _probe() -> tuple[bool, float | None]:
    path = _bundle_path()
    if not path:
        return False, None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return False, None
    return mtime > PROCESS_START + 1.0, mtime


def status() -> dict:
    """``{"runtime_stale": bool, "bundle_mtime": ts|None, "process_started": ts}``."""
    now = time.time()
    if now - _cache["at"] >= _CACHE_TTL_S:
        stale, mtime = _probe()
        if stale and not _cache["stale"]:
            _log.warning(
                "runtime bundle replaced after startup (mtime=%s > start=%s) — "
                "restart the app to load the new runtime",
                mtime,
                PROCESS_START,
            )
        _cache.update({"at": now, "stale": stale, "mtime": mtime})
    return {
        "runtime_stale": bool(_cache["stale"]),
        "bundle_mtime": _cache["mtime"],
        "process_started": PROCESS_START,
    }
