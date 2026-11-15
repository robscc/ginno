"""A2A (Agent2Agent) protocol support — connector-based external agent collaboration.

Design: ``docs/design/a2a-connector-design.md``.

Layering (P0):
  models  — protocol data model (TaskState / Message / Part / Artifact / Task / AgentCard)
  errors  — A2A error codes + JSON-RPC mapping (no invented codes)
  address — Capability URL parsing (address-as-credential) + fingerprint pinning
  card    — Agent Card discovery / fetch / validation + SSRF guard
  client  — JSON-RPC over HTTP client (SendMessage / GetTask / CancelTask)
  peers   — peer registry + context mapping (contexts.json)

The protocol source of truth is ``specification/a2a.proto`` upstream; the constants
below were verified against it on 2026-11-15 (see design §3).
"""

from .address import (
    CapabilityAddress,
    build_address,
    fingerprint_of,
    parse_address,
    well_known_url,
)
from .card import (
    ensure_fingerprint,
    fetch_card,
    guard_endpoint,
    validate_card,
    verify_endpoint_fingerprint,
)
from .client import A2AClient
from .errors import (
    A2AError,
    TaskNotFoundError,
    context_survives,
    error_from_rpc,
    is_not_found,
    retention_data,
    retention_reason,
)
from .models import (
    INTERRUPT_STATES,
    TERMINAL_STATES,
    AgentCard,
    AgentSkill,
    Artifact,
    Message,
    Part,
    Task,
    TaskState,
    TaskStatus,
)

# 包根只暴露**协议面**（models / errors / address / card / client）。
# 基础设施模块刻意不在此 re-export，按需直接 import：
#   a2a.identity  —— 拖 cryptography，而 connectors/a2a.py → a2a.peers 会触发
#                    本 __init__，把重依赖塞进连接器导入链（并行开发期实测过的坑）
#   a2a.auth      —— token 存储，属服务端基础设施
#   a2a.peers / a2a.contexts —— 存储层，不是协议
# 这样 `import ginno_runtime.a2a` 保持轻量，连接器导入链不受影响。

__all__ = [
    # models
    "TERMINAL_STATES",
    "INTERRUPT_STATES",
    "AgentCard",
    "AgentSkill",
    "Artifact",
    "Message",
    "Part",
    "Task",
    "TaskState",
    "TaskStatus",
    # errors
    "A2AError",
    "TaskNotFoundError",
    "error_from_rpc",
    "is_not_found",
    "retention_reason",
    "retention_data",
    "context_survives",
    # address
    "CapabilityAddress",
    "parse_address",
    "build_address",
    "fingerprint_of",
    "well_known_url",
    # card
    "fetch_card",
    "guard_endpoint",
    "validate_card",
    "ensure_fingerprint",
    "verify_endpoint_fingerprint",
    # client
    "A2AClient",
]
