"""A2A protocol data model (design §3, verified against upstream a2a.proto).

Wire format is JSON (JSON-RPC binding). Enums use the lowercase/hyphenated string
forms the JSON binding emits. Parsing is deliberately tolerant: a remote agent is
an untrusted peer and may omit optional fields — we never raise on a missing
optional, only on a structurally unusable payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class TaskState(str, Enum):
    """The 9 states from ``a2a.proto``. **There is no ``expired`` state** —
    TTL expiry is expressed via ``TaskNotFoundError``, never a new state
    (design §9.1)."""

    UNSPECIFIED = "unspecified"
    SUBMITTED = "submitted"
    WORKING = "working"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    INPUT_REQUIRED = "input-required"
    REJECTED = "rejected"
    AUTH_REQUIRED = "auth-required"

    @classmethod
    def parse(cls, raw: Any) -> "TaskState":
        """Tolerant parse. Unknown/missing → UNSPECIFIED (never raise)."""
        if isinstance(raw, cls):
            return raw
        if not isinstance(raw, str):
            return cls.UNSPECIFIED
        key = raw.strip().lower().replace("_", "-")
        # proto enum names arrive as TASK_STATE_INPUT_REQUIRED in some bindings
        if key.startswith("task-state-"):
            key = key[len("task-state-"):]
        for member in cls:
            if member.value == key:
                return member
        return cls.UNSPECIFIED


#: Terminal states are immutable — a terminal task can never be restarted;
#: refinement must open a NEW task in the same contextId (design §8.1).
TERMINAL_STATES = frozenset({
    TaskState.COMPLETED,
    TaskState.FAILED,
    TaskState.CANCELED,
    TaskState.REJECTED,
})

#: Interrupt states — the remote is waiting on the caller. These map onto
#: Ginno's ask_user / browser_handoff (design §7.2).
INTERRUPT_STATES = frozenset({
    TaskState.INPUT_REQUIRED,
    TaskState.AUTH_REQUIRED,
})


@dataclass
class Part:
    """A message/artifact payload unit: text, file, or structured data."""

    kind: str = "text"  # text | file | data
    text: str = ""
    data: dict = field(default_factory=dict)
    file: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "Part":
        if not isinstance(raw, dict):
            return cls(kind="text", text=str(raw or ""))
        kind = raw.get("kind") or raw.get("type") or "text"
        return cls(
            kind=str(kind),
            text=str(raw.get("text") or ""),
            data=raw.get("data") if isinstance(raw.get("data"), dict) else {},
            file=raw.get("file") if isinstance(raw.get("file"), dict) else {},
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        )

    def to_dict(self) -> dict:
        out: dict = {"kind": self.kind}
        if self.kind == "text":
            out["text"] = self.text
        elif self.kind == "data":
            out["data"] = self.data
        elif self.kind == "file":
            out["file"] = self.file
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    def as_text(self) -> str:
        """Best-effort text projection (for tool results / untrusted labeling)."""
        if self.kind == "text":
            return self.text
        if self.kind == "file":
            name = self.file.get("name") or self.file.get("filename") or ""
            uri = self.file.get("uri") or ""
            return f"[file:{name or uri}]"
        if self.kind == "data":
            from ginno_runtime import jsonx as json
            try:
                return json.dumps(self.data, ensure_ascii=False)
            except (TypeError, ValueError):
                return str(self.data)
        return ""


@dataclass
class Message:
    """A single exchange unit. ``role`` is ``user`` (caller) or ``agent`` (peer)."""

    role: str = "agent"
    parts: list[Part] = field(default_factory=list)
    message_id: str = ""
    context_id: str = ""
    task_id: str = ""
    metadata: dict = field(default_factory=dict)
    # A2A 标准 referenceTaskIds：追问/续跑时指向被引用的任务。
    # input-required 任务的远程批准闭环靠它定位要 resume 的那条 task。
    reference_task_ids: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any) -> "Message":
        if not isinstance(raw, dict):
            return cls(parts=[Part(text=str(raw or ""))])
        parts_raw = raw.get("parts")
        parts = [Part.from_dict(p) for p in parts_raw] if isinstance(parts_raw, list) else []
        return cls(
            role=str(raw.get("role") or "agent"),
            parts=parts,
            message_id=str(raw.get("messageId") or raw.get("message_id") or ""),
            context_id=str(raw.get("contextId") or raw.get("context_id") or ""),
            task_id=str(raw.get("taskId") or raw.get("task_id") or ""),
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
            reference_task_ids=[
                str(x)
                for x in (raw.get("referenceTaskIds") or raw.get("reference_task_ids") or [])
                if str(x)
            ]
            if isinstance(raw.get("referenceTaskIds") or raw.get("reference_task_ids"), list)
            else [],
        )

    def to_dict(self) -> dict:
        out: dict = {"role": self.role, "parts": [p.to_dict() for p in self.parts]}
        if self.message_id:
            out["messageId"] = self.message_id
        if self.context_id:
            out["contextId"] = self.context_id
        if self.task_id:
            out["taskId"] = self.task_id
        if self.reference_task_ids:
            out["referenceTaskIds"] = list(self.reference_task_ids)
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    def text(self) -> str:
        return "\n".join(t for t in (p.as_text() for p in self.parts) if t)


@dataclass
class Artifact:
    """Task output. Version lineage is the CLIENT's responsibility — the protocol
    does not track it (design §3, §8.1)."""

    artifact_id: str = ""
    name: str = ""
    description: str = ""
    parts: list[Part] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "Artifact":
        if not isinstance(raw, dict):
            return cls()
        parts_raw = raw.get("parts")
        return cls(
            artifact_id=str(raw.get("artifactId") or raw.get("artifact_id") or ""),
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            parts=[Part.from_dict(p) for p in parts_raw] if isinstance(parts_raw, list) else [],
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        )

    def to_dict(self) -> dict:
        out: dict = {"parts": [p.to_dict() for p in self.parts]}
        if self.artifact_id:
            out["artifactId"] = self.artifact_id
        if self.name:
            out["name"] = self.name
        if self.description:
            out["description"] = self.description
        if self.metadata:
            out["metadata"] = self.metadata
        return out

    def text(self) -> str:
        return "\n".join(t for t in (p.as_text() for p in self.parts) if t)


@dataclass
class TaskStatus:
    state: TaskState = TaskState.UNSPECIFIED
    message: Message | None = None
    timestamp: str = ""

    @classmethod
    def from_dict(cls, raw: Any) -> "TaskStatus":
        if not isinstance(raw, dict):
            return cls(state=TaskState.parse(raw))
        msg = raw.get("message")
        return cls(
            state=TaskState.parse(raw.get("state")),
            message=Message.from_dict(msg) if isinstance(msg, dict) else None,
            timestamp=str(raw.get("timestamp") or ""),
        )

    def to_dict(self) -> dict:
        out: dict = {"state": self.state.value}
        if self.message is not None:
            out["message"] = self.message.to_dict()
        if self.timestamp:
            out["timestamp"] = self.timestamp
        return out


@dataclass
class Task:
    """A stateful unit of work. ``id`` and ``context_id`` are server-generated and
    MUST be treated as opaque by the client (design §3)."""

    id: str = ""
    context_id: str = ""
    status: TaskStatus = field(default_factory=TaskStatus)
    artifacts: list[Artifact] = field(default_factory=list)
    history: list[Message] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "Task":
        if not isinstance(raw, dict):
            return cls()
        arts = raw.get("artifacts")
        hist = raw.get("history")
        return cls(
            id=str(raw.get("id") or ""),
            context_id=str(raw.get("contextId") or raw.get("context_id") or ""),
            status=TaskStatus.from_dict(raw.get("status")),
            artifacts=[Artifact.from_dict(a) for a in arts] if isinstance(arts, list) else [],
            history=[Message.from_dict(m) for m in hist] if isinstance(hist, list) else [],
            metadata=raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {},
        )

    @property
    def state(self) -> TaskState:
        return self.status.state

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def needs_input(self) -> bool:
        return self.state in INTERRUPT_STATES

    def result_text(self) -> str:
        """Artifacts first, then the status message, then last history entry."""
        chunks = [a.text() for a in self.artifacts if a.text()]
        if not chunks and self.status.message is not None:
            chunks = [self.status.message.text()]
        if not chunks and self.history:
            chunks = [self.history[-1].text()]
        return "\n".join(c for c in chunks if c)


@dataclass
class AgentSkill:
    """Descriptive capability — **NOT a callable endpoint**. Skills go into the
    delegate tool's description, never one-tool-per-skill (design §7.4)."""

    id: str = ""
    name: str = ""
    description: str = ""
    tags: list[str] = field(default_factory=list)
    examples: list[str] = field(default_factory=list)
    input_modes: list[str] = field(default_factory=list)
    output_modes: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Any) -> "AgentSkill":
        if not isinstance(raw, dict):
            return cls()

        def _strs(key: str) -> list[str]:
            v = raw.get(key)
            return [str(x) for x in v] if isinstance(v, list) else []

        return cls(
            id=str(raw.get("id") or ""),
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            tags=_strs("tags"),
            examples=_strs("examples"),
            input_modes=_strs("inputModes") or _strs("input_modes"),
            output_modes=_strs("outputModes") or _strs("output_modes"),
        )


@dataclass
class AgentCard:
    """Public identity + capabilities of a remote agent.

    **Everything here is UNTRUSTED INPUT** — name/description/skill text all land
    in the model context and are prompt-injection carriers (design §10.4).
    """

    name: str = ""
    description: str = ""
    url: str = ""
    version: str = ""
    protocol_version: str = ""
    provider: dict = field(default_factory=dict)
    capabilities: dict = field(default_factory=dict)
    skills: list[AgentSkill] = field(default_factory=list)
    security_schemes: dict = field(default_factory=dict)
    security: list = field(default_factory=list)
    extensions: list = field(default_factory=list)
    default_input_modes: list[str] = field(default_factory=list)
    default_output_modes: list[str] = field(default_factory=list)
    documentation_url: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> "AgentCard":
        if not isinstance(raw, dict):
            return cls()

        def _strs(key: str) -> list[str]:
            v = raw.get(key)
            return [str(x) for x in v] if isinstance(v, list) else []

        skills_raw = raw.get("skills")
        return cls(
            name=str(raw.get("name") or ""),
            description=str(raw.get("description") or ""),
            url=str(raw.get("url") or ""),
            version=str(raw.get("version") or ""),
            protocol_version=str(raw.get("protocolVersion") or raw.get("protocol_version") or ""),
            provider=raw.get("provider") if isinstance(raw.get("provider"), dict) else {},
            capabilities=raw.get("capabilities") if isinstance(raw.get("capabilities"), dict) else {},
            skills=[AgentSkill.from_dict(s) for s in skills_raw] if isinstance(skills_raw, list) else [],
            security_schemes=(
                raw.get("securitySchemes") if isinstance(raw.get("securitySchemes"), dict) else {}
            ),
            security=raw.get("security") if isinstance(raw.get("security"), list) else [],
            extensions=raw.get("extensions") if isinstance(raw.get("extensions"), list) else [],
            default_input_modes=_strs("defaultInputModes"),
            default_output_modes=_strs("defaultOutputModes"),
            documentation_url=str(raw.get("documentationUrl") or ""),
            raw=raw,
        )

    @property
    def supports_streaming(self) -> bool:
        return bool(self.capabilities.get("streaming"))

    @property
    def supports_push(self) -> bool:
        return bool(self.capabilities.get("pushNotifications"))

    def auth_schemes(self) -> list[str]:
        """Declared auth scheme names (OpenAPI-style ``security`` list)."""
        names: list[str] = []
        for entry in self.security:
            if isinstance(entry, dict):
                names.extend(str(k) for k in entry)
        return names

    def retention_policy(self) -> dict | None:
        """The Ginno retention extension params, if the peer publishes them.

        Looked up by URI suffix so a peer may host the schema anywhere; matched
        loosely because the extension is advisory (design §9.5).
        """
        for ext in self.extensions:
            if not isinstance(ext, dict):
                continue
            uri = str(ext.get("uri") or "")
            if "retention" in uri:
                params = ext.get("params")
                return params if isinstance(params, dict) else {}
        return None

    def summary(self) -> str:
        """Short human-readable summary for the connector overview tab."""
        bits = [self.name or "(unnamed agent)"]
        if self.version:
            bits.append(f"v{self.version}")
        org = self.provider.get("organization") if isinstance(self.provider, dict) else None
        if org:
            bits.append(str(org))
        caps = []
        if self.supports_streaming:
            caps.append("streaming")
        if self.supports_push:
            caps.append("push")
        if caps:
            bits.append("+".join(caps))
        return " · ".join(bits)
