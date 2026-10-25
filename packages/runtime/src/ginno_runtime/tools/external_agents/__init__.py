"""External-agent adapters（一个 CLI 一个文件）+ 接口。"""

from .base import (  # noqa: F401 — 装配层与测试的稳定导入面
    _TIMEOUT_DEFAULT_S,
    _TIMEOUT_MAX_S,
    _TIMEOUT_MIN_S,
    DelegationResult,
    ExternalBackend,
    _parse_json_lenient,
    _resolve_cli,
    _tail,
    clear_cli_cache,
    cli_version,
    cli_version_warning,
    invalidate_cli,
)
from .claude_code import ClaudeCodeBackend
from .codex import CodexBackend
from .dsh import DshBackend
from .pi import PiBackend
