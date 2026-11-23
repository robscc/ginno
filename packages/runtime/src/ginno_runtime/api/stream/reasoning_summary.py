"""Latest visible Responses summary for an active turn segment.

Snapshots carry an increasing revision so stream delivery and turn-state
probes can arrive in either order. Completed history comes from AI messages.
"""

from __future__ import annotations

from itertools import count

_revisions = count(1)


class TurnReasoningSummary:
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.parts: dict[str, str] = {}
        self.revision = 0
        self.receiving = False

    def add(self, parts: list[dict[str, str]]) -> str:
        delta = []
        for part in parts:
            key, text = part["id"], part["text"]
            if self.parts and key not in self.parts:
                delta.append("\n\n")
            self.parts[key] = self.parts.get(key, "") + text
            delta.append(text)
        self.receiving = True
        self.revision = next(_revisions)
        return "".join(delta)

    def snapshot(self) -> dict | None:
        if not self.parts:
            return None
        return {
            "revision": self.revision,
            "receiving": self.receiving,
            "parts": [{"id": key, "text": text} for key, text in self.parts.items()],
        }

    def finish_receiving(self) -> None:
        self.receiving = False
        self.revision = next(_revisions)


_active: dict[str, TurnReasoningSummary] = {}


def begin_summary(session_id: str, turn_id: str) -> TurnReasoningSummary:
    summary = TurnReasoningSummary(turn_id)
    _active[session_id] = summary
    return summary


def end_summary(session_id: str, summary: TurnReasoningSummary) -> None:
    if _active.get(session_id) is summary:
        del _active[session_id]


def active_summary(session_id: str, turn_id: str) -> dict | None:
    summary = _active.get(session_id)
    return summary.snapshot() if summary and summary.turn_id == turn_id else None
