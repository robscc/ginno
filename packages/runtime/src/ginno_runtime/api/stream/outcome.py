"""Result of a chat turn segment, shared by the local and remote runners."""

from enum import StrEnum


class TurnOutcome(StrEnum):
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    AWAITING_INPUT = "awaiting_input"
