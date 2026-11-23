"""Visible Responses summaries grouped by model request in an active turn.

Each snapshot carries a revision so stream delivery and turn-state probes
can arrive in either order. Completed history comes from AI messages.
"""

from __future__ import annotations

from itertools import count

_revisions = count(1)


class RequestReasoningSummary:
    def __init__(self, request_id: str):
        self.request_id = request_id
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

    def snapshot(self) -> dict:
        return {
            "requestId": self.request_id,
            "revision": self.revision,
            "receiving": self.receiving,
            "parts": [{"id": key, "text": text} for key, text in self.parts.items()],
        }

    def finish_receiving(self) -> None:
        self.receiving = False
        self.revision = next(_revisions)


class TurnReasoningSummary:
    def __init__(self, turn_id: str):
        self.turn_id = turn_id
        self.current: RequestReasoningSummary | None = None
        self._requests: dict[str, RequestReasoningSummary] = {}

    def select_request(self, request_id: str | None) -> None:
        self.current = None
        if request_id is not None:
            if request_id not in self._requests:
                self._requests[request_id] = RequestReasoningSummary(request_id)
            self.current = self._requests[request_id]

    def snapshots(self) -> list[dict]:
        return [request.snapshot() for request in self._requests.values()]


_active: dict[str, TurnReasoningSummary] = {}


def begin_summary(session_id: str, turn_id: str) -> TurnReasoningSummary:
    summary = TurnReasoningSummary(turn_id)
    _active[session_id] = summary
    return summary


def end_summary(session_id: str, summary: TurnReasoningSummary) -> None:
    if _active.get(session_id) is summary:
        del _active[session_id]


def active_summaries(session_id: str, turn_id: str) -> list[dict]:
    summary = _active.get(session_id)
    return summary.snapshots() if summary and summary.turn_id == turn_id else []
