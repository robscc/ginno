"""Embedded browser: Space / ownership / eval / handoff (docs/browser-embed-design.md).

Supervisor + engine abstraction. Tests use ``FakeEngine``
(``GINNO_BROWSER_ENGINE=fake``). Production prefers a packaged CEF tile
when present, otherwise headless system Chrome via CDP (screencast into
the pane). The fake engine still tracks ownership so the UI and HITL path
work when neither runtime is available.
"""

from __future__ import annotations

from typing import Any

from .supervisor import BrowserSupervisor

_SUPERVISOR: BrowserSupervisor | None = None


def get_supervisor() -> BrowserSupervisor:
    """Process-wide singleton. Created on first use; reset in tests."""
    global _SUPERVISOR
    if _SUPERVISOR is None:
        _SUPERVISOR = BrowserSupervisor()
    return _SUPERVISOR


def reset_supervisor() -> None:
    """Drop the singleton (test isolation / shutdown)."""
    global _SUPERVISOR
    if _SUPERVISOR is not None:
        try:
            _SUPERVISOR.close_sync()
        except Exception:
            pass
    _SUPERVISOR = None


def waiting_human(session_id: str | None = None) -> bool:
    """True when any (optionally session-bound) Space is in handoff."""
    sup = _SUPERVISOR
    if sup is None:
        return False
    return sup.waiting_human(session_id)


__all__ = [
    "BrowserSupervisor",
    "get_supervisor",
    "reset_supervisor",
    "waiting_human",
]
