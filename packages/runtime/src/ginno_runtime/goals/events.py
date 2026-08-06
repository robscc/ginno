"""Goal change notification bridge (tools -> server) without circular imports.

Goal tools mutate the store mid-turn; the server (WS broadcast + continuation
driver) must react. The tools cannot import server.py (server imports graph,
graph imports tools), so they call :func:`notify_goal_changed` here and the
server registers a listener at startup.
"""

from __future__ import annotations

from typing import Any, Callable

# fn(slug, session_id, goal_or_none) — goal_or_none=None means "cleared".
GoalListener = Callable[[str, str, dict[str, Any] | None], None]

_listeners: list[GoalListener] = []


def register_goal_listener(fn: GoalListener) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def notify_goal_changed(slug: str, session_id: str, goal: dict[str, Any] | None) -> None:
    for fn in list(_listeners):
        try:
            fn(slug, session_id, goal)
        except Exception:
            # a listener failure must never break the mutating tool call
            import logging

            logging.getLogger("ginno").exception("goal_listener_failed")
