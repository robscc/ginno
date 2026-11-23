"""Visible Responses summaries grouped by model request in an active turn.

A delta advances the last emitted revision. Snapshots can recover missed
deltas when a client reconnects. Completed history comes from AI messages.
"""

from __future__ import annotations

from itertools import count

_revisions = count(1)


class RequestReasoningSummary:
    def __init__(self, request_id: str):
        self.request_id = request_id
        self.parts: dict[str, list[str]] = {}
        self._pending: dict[str, list[str]] = {}
        self.revision = 0
        self.receiving = False

    def add(self, parts: list[dict[str, str]]) -> str:
        delta = []
        for part in parts:
            key, text = part["id"], part["text"]
            if (self.parts or self._pending) and key not in self.parts and key not in self._pending:
                delta.append("\n\n")
            self._pending.setdefault(key, []).append(text)
            delta.append(text)
        return "".join(delta)

    def take_delta(self, *, receiving: bool) -> dict:
        base_revision = self.revision
        parts = []
        for key, fragments in self._pending.items():
            text = "".join(fragments)
            self.parts.setdefault(key, []).append(text)
            parts.append({"id": key, "text": text})
        self._pending.clear()
        self.receiving = receiving
        self.revision = next(_revisions)
        return {
            "requestId": self.request_id,
            "baseRevision": base_revision,
            "revision": self.revision,
            "receiving": self.receiving,
            "parts": parts,
        }

    def snapshot(self) -> dict:
        return {
            "requestId": self.request_id,
            "revision": self.revision,
            "receiving": self.receiving,
            "parts": [{"id": key, "text": "".join(parts)} for key, parts in self.parts.items()],
        }


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
        return [request.snapshot() for request in self._requests.values() if request.revision]

    def snapshot_for(self, request_id: str) -> dict | None:
        request = self._requests.get(request_id)
        return request.snapshot() if request and request.revision else None


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

def active_summary(session_id: str, turn_id: str, request_id: str) -> dict | None:
    summary = _active.get(session_id)
    return summary.snapshot_for(request_id) if summary and summary.turn_id == turn_id else None
