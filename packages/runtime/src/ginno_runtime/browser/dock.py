"""Best-effort dock of the headed Chrome window into the Ginno tile.

M1 is still a separate OS window (design §7.3). We position it over the
rectangle the frontend reports. Failure is non-fatal — activate() still
raises the window so the human can reach the real page.
"""

from __future__ import annotations

import logging
import subprocess
from typing import Any

log = logging.getLogger(__name__)


def set_chrome_bounds(x: int, y: int, width: int, height: int) -> dict[str, Any]:
    """Move/resize the frontmost Google Chrome window (macOS AppleScript)."""
    if width < 80 or height < 80:
        return {"ok": False, "error": "tile too small"}
    script = f"""
    tell application "System Events"
      if not (exists process "Google Chrome") then return "missing"
      tell process "Google Chrome"
        set frontmost to true
        if (count of windows) is 0 then return "no-window"
        set position of window 1 to {{{int(x)}, {int(y)}}}
        set size of window 1 to {{{int(width)}, {int(height)}}}
      end tell
    end tell
    return "ok"
    """
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except Exception as e:
        log.debug("dock osascript failed: %s", e)
        return {"ok": False, "error": str(e)}
    out = (r.stdout or "").strip() or (r.stderr or "").strip()
    if r.returncode != 0:
        return {"ok": False, "error": out or f"osascript exit {r.returncode}"}
    if out in {"missing", "no-window"}:
        return {"ok": False, "error": out}
    return {"ok": True, "bounds": {"x": int(x), "y": int(y), "width": int(width), "height": int(height)}}
