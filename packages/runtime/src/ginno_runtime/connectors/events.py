"""Connector event bus (connector-module-design.md §5).

The push half the M1 polling fallback stood in for: a tiny fan-out that the
WS endpoint (/api/ws/connectors) subscribes to. Producers:

- ``registry`` status transitions        → connector_status_changed
- ``browser/relay`` tools/progress       → tool_progress
- ``tools/browser_tools`` B 轨 fallback  → browser_fallback_used
- ``browser_handoff`` start/release      → handoff_changed
- relay ``openInChat`` (popup 发送此页面) → page_pushed (+ latest kept here)

Sync listeners only — the WS endpoint marshals into its asyncio queue.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

Listener = Callable[[str, dict], None]


class ConnectorEvents:
    def __init__(self) -> None:
        self._listeners: list[Listener] = []
        self._lock = threading.Lock()
        # latest pushed browser page (popup「发送此页面」); None until first use
        self.latest_page: dict | None = None
        # latest browser tool progress row (for snapshot on WS connect)
        self.latest_progress: dict | None = None

    def subscribe(self, fn: Listener) -> None:
        with self._lock:
            if fn not in self._listeners:
                self._listeners.append(fn)

    def unsubscribe(self, fn: Listener) -> None:
        with self._lock:
            if fn in self._listeners:
                self._listeners.remove(fn)

    def emit(self, type_: str, data: dict | None = None) -> None:
        data = data or {}
        if type_ == "page_pushed":
            self.latest_page = data
        elif type_ == "tool_progress":
            self.latest_progress = data
        with self._lock:
            listeners = list(self._listeners)
        for fn in listeners:
            try:
                fn(type_, data)
            except Exception:  # noqa: BLE001 — a broken listener never blocks others
                pass


_events: ConnectorEvents | None = None


def connector_events() -> ConnectorEvents:
    global _events
    if _events is None:
        _events = ConnectorEvents()
    return _events


def wire_default_producers() -> None:
    """Idempotent: hook registry + relay progress into the bus."""
    ev = connector_events()
    from .registry import registry

    def _on_status(event: str, payload: dict) -> None:
        ev.emit(event, payload)

    if _on_status not in registry()._listeners:
        registry().subscribe(_on_status)
    from ..browser.relay import set_progress_listener

    def _on_progress(params: dict) -> None:
        ev.emit("tool_progress", params)

    set_progress_listener(_on_progress)
