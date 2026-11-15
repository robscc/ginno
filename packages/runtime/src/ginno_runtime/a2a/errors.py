"""A2A error codes and JSON-RPC mapping (design §3, §9.4).

**No invented codes.** The A2A-specific range is ``-32001``..``-32099``; the names
and values below were verified against upstream ``docs/specification.md`` §5.4
(Error Code Mappings) on 2026-11-15.

TTL expiry is expressed as ``TaskNotFoundError`` — the spec's own error table
already folds "invalid, expired, or already completed and purged" into it. A
distinguishing ``reason`` rides in the optional JSON-RPC ``error.data`` field so
smart clients can self-heal precisely while standard clients ignore it and still
work (design §9.4).
"""

from __future__ import annotations

from typing import Any

# JSON-RPC 2.0 standard codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# A2A-specific codes (-32001 .. -32099)
TASK_NOT_FOUND = -32001
TASK_NOT_CANCELABLE = -32002
PUSH_NOTIFICATION_NOT_SUPPORTED = -32003
UNSUPPORTED_OPERATION = -32004
CONTENT_TYPE_NOT_SUPPORTED = -32005
INVALID_AGENT_RESPONSE = -32006
EXTENDED_CARD_NOT_CONFIGURED = -32007
EXTENSION_SUPPORT_REQUIRED = -32008
VERSION_NOT_SUPPORTED = -32009

_NAMES = {
    TASK_NOT_FOUND: "TaskNotFoundError",
    TASK_NOT_CANCELABLE: "TaskNotCancelableError",
    PUSH_NOTIFICATION_NOT_SUPPORTED: "PushNotificationNotSupportedError",
    UNSUPPORTED_OPERATION: "UnsupportedOperationError",
    CONTENT_TYPE_NOT_SUPPORTED: "ContentTypeNotSupportedError",
    INVALID_AGENT_RESPONSE: "InvalidAgentResponseError",
    EXTENDED_CARD_NOT_CONFIGURED: "ExtendedAgentCardNotConfiguredError",
    EXTENSION_SUPPORT_REQUIRED: "ExtensionSupportRequiredError",
    VERSION_NOT_SUPPORTED: "VersionNotSupportedError",
}

#: Retention reasons carried in ``error.data["ginno:retention"].reason``.
#: ``task_purged`` means the CONTEXT survives — reuse it, open a new task.
#: Anything else (or a missing reason) means start a fresh context.
REASON_CONTEXT_EXPIRED = "context_expired"
REASON_CONTEXT_PURGED = "context_purged"
REASON_TASK_PURGED = "task_purged"
REASON_UNKNOWN = "unknown"


class A2AError(Exception):
    """Base for A2A protocol errors. Carries the JSON-RPC code and optional data.

    Tools must NEVER raise this across the tool boundary — the tool layer flattens
    it to an ``[error]``-prefixed string (delegate_agent seam principle 3).
    """

    def __init__(self, message: str, *, code: int = INTERNAL_ERROR, data: Any = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.data = data

    @property
    def name(self) -> str:
        return _NAMES.get(self.code, "A2AError")

    def to_rpc(self) -> dict:
        out: dict = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out

    def __str__(self) -> str:
        return f"{self.name}({self.code}): {self.message}"


class TaskNotFoundError(A2AError):
    """Task/context does not exist or is not accessible — invalid, expired, or purged."""

    def __init__(self, message: str = "Task not found", *, data: Any = None):
        super().__init__(message, code=TASK_NOT_FOUND, data=data)


class TaskNotCancelableError(A2AError):
    def __init__(self, message: str = "Task cannot be canceled", *, data: Any = None):
        super().__init__(message, code=TASK_NOT_CANCELABLE, data=data)


class UnsupportedOperationError(A2AError):
    def __init__(self, message: str = "Unsupported operation", *, data: Any = None):
        super().__init__(message, code=UNSUPPORTED_OPERATION, data=data)


class ExtensionSupportRequiredError(A2AError):
    def __init__(self, message: str = "Required extension not supported", *, data: Any = None):
        super().__init__(message, code=EXTENSION_SUPPORT_REQUIRED, data=data)


class VersionNotSupportedError(A2AError):
    def __init__(self, message: str = "Protocol version not supported", *, data: Any = None):
        super().__init__(message, code=VERSION_NOT_SUPPORTED, data=data)


def error_from_rpc(raw: Any) -> A2AError:
    """Build an A2AError from a JSON-RPC error object (tolerant of malformed input)."""
    if not isinstance(raw, dict):
        return A2AError(str(raw or "unknown A2A error"))
    code = raw.get("code")
    code = code if isinstance(code, int) else INTERNAL_ERROR
    message = str(raw.get("message") or "A2A error")
    data = raw.get("data")
    if code == TASK_NOT_FOUND:
        return TaskNotFoundError(message, data=data)
    if code == TASK_NOT_CANCELABLE:
        return TaskNotCancelableError(message, data=data)
    if code == UNSUPPORTED_OPERATION:
        return UnsupportedOperationError(message, data=data)
    if code == EXTENSION_SUPPORT_REQUIRED:
        return ExtensionSupportRequiredError(message, data=data)
    if code == VERSION_NOT_SUPPORTED:
        return VersionNotSupportedError(message, data=data)
    return A2AError(message, code=code, data=data)


def is_not_found(err: BaseException | Any) -> bool:
    """True if the error means 'gone' (invalid/expired/purged).

    Used by the self-heal path AND by cancel: per spec CancelTask is idempotent and
    MAY return TaskNotFoundError once purged — the client MUST treat that as
    cancel SUCCESS (design §9.6).
    """
    if isinstance(err, TaskNotFoundError):
        return True
    if isinstance(err, A2AError):
        return err.code == TASK_NOT_FOUND
    return False


def retention_reason(err: BaseException | Any) -> str:
    """Extract the retention reason from ``error.data``, defaulting to unknown.

    Accepts both the Ginno-namespaced shape and a bare ``{"reason": ...}`` so a
    peer that implements the idea without our exact namespace still interoperates.
    """
    data = getattr(err, "data", None)
    if not isinstance(data, dict):
        return REASON_UNKNOWN
    block = data.get("ginno:retention")
    if not isinstance(block, dict):
        block = data
    reason = block.get("reason")
    if isinstance(reason, str) and reason in (
        REASON_CONTEXT_EXPIRED, REASON_CONTEXT_PURGED, REASON_TASK_PURGED,
    ):
        return reason
    return REASON_UNKNOWN


def context_survives(err: BaseException | Any) -> bool:
    """True when the context is still usable and only the task is gone.

    Drives the self-heal branch: reuse contextId + open a new task, versus
    omitting contextId entirely to get a fresh one (design §9.6).
    """
    return is_not_found(err) and retention_reason(err) == REASON_TASK_PURGED


def retention_data(reason: str, *, obj: str = "context", expired_at: str = "",
                   policy_uri: str = "", recoverable: bool = False) -> dict:
    """Build the ``error.data`` payload a Ginno SERVER attaches on expiry."""
    return {"ginno:retention": {
        "reason": reason,
        "object": obj,
        "expired_at": expired_at,
        "policy_uri": policy_uri,
        "recoverable": recoverable,
    }}
