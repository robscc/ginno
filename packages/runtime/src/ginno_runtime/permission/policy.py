"""Permission policy matcher.

Settings format:

    ~/.ginno/settings.json
    {
      "permissions": {
        "allow": ["Read(*)", "Bash(git status)", "MCP.obsidian.search(*)"],
        "deny":  ["Bash(rm -rf *)", "Write(~/.ssh/**)"],
        "ask":   ["Bash(*)", "Write(~/workspace/**)"]
      }
    }

Patterns are "<ToolName>(<arg-glob>)". First match wins, in order:
deny → ask → allow. Default is "ask".
"""

from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from ginno_runtime import jsonx as json

from .. import paths
from .. import settings_store

log = logging.getLogger(__name__)

PermissionDecision = Literal["allow", "deny", "ask"]


@dataclass
class PermissionRule:
    tool: str
    arg_pattern: str  # glob

    def matches(self, tool_name: str, args_repr: str) -> bool:
        if not fnmatch.fnmatch(tool_name.lower(), self.tool.lower()):
            return False
        return fnmatch.fnmatch(args_repr.lower(), self.arg_pattern.lower())


def _parse_rule(spec: str) -> PermissionRule:
    spec = spec.strip()
    if "(" not in spec:
        return PermissionRule(tool=spec, arg_pattern="*")
    head, rest = spec.split("(", 1)
    arg = rest.rstrip().rstrip(")")
    return PermissionRule(tool=head.strip(), arg_pattern=arg.strip() or "*")


@dataclass
class PermissionPolicy:
    allow: list[PermissionRule]
    deny: list[PermissionRule]
    ask: list[PermissionRule]

    @classmethod
    def from_settings(cls, settings: dict | None = None) -> "PermissionPolicy":
        if settings is None:
            # 三态读取：损坏 → 空权限（decide 默认 ask，fail-closed），
            # 绝不 raise——graph.py 构建 session 时无 try 包裹，损坏的
            # settings.json 不能把整个应用炸到不可用（settings-integrity）。
            doc = settings_store.load_doc()
            settings = doc.data
        perms = settings.get("permissions", {}) if isinstance(settings, dict) else {}
        if not isinstance(perms, dict):
            perms = {}
        return cls(
            allow=[_parse_rule(s) for s in perms.get("allow", [])],
            deny=[_parse_rule(s) for s in perms.get("deny", [])],
            ask=[_parse_rule(s) for s in perms.get("ask", [])],
        )

    def decide(self, tool_name: str, args_repr: str = "") -> PermissionDecision:
        for r in self.deny:
            if r.matches(tool_name, args_repr):
                return "deny"
        for r in self.ask:
            if r.matches(tool_name, args_repr):
                return "ask"
        for r in self.allow:
            if r.matches(tool_name, args_repr):
                return "allow"
        return "ask"


def is_bypass_permissions(settings: dict | None = None) -> bool:
    """Privileged / "yolo" mode: when on, the permission node lets every tool
    call through without any tools_allow / hook / policy check (i.e. "allow all
    commands"). Defaults to ON. Read live from settings so a UI toggle applies to
    the next tool call without rebuilding sessions.

    损坏 fail-closed（settings-integrity）：文件损坏读不出 bypass 位时返回
    False（最严）。默认 ON 只适用于「文件不存在/键缺失」的可信缺省态；
    损坏文档不是可信缺省，退成宽松可执行状态等于放大事故。
    """
    if settings is None:
        doc = settings_store.load_doc()
        if doc.corrupt:
            return False
        settings = doc.data
    return bool(settings.get("bypass_permissions", True))


def _append_allow(tools: tuple[str, ...]) -> None:
    """幂等迁移（锁内读-改-写）：tools 中未被任何 bucket 提及的名字加进
    permissions.allow。无 permissions 块 / 已提及 → no-op；损坏 → 静默
    放弃本次迁移（与旧 JSONDecodeError 分支同一语义）——迁移是尽力而为
    的加法，损坏文件必须留给用户修复，存储层的拒写保证原字节不动。"""
    def _mut(settings: dict) -> dict | None:
        perms = settings.get("permissions")
        if not isinstance(perms, dict):
            return None  # no permissions block: defaults apply, nothing to migrate
        existing = " ".join(
            str(x) for bucket in ("allow", "deny", "ask") for x in perms.get(bucket, []) or []
        ).lower()
        allow = list(perms.get("allow") or [])
        added = [n for n in tools if n not in existing and n not in allow]
        if not added:
            return None
        perms["allow"] = allow + added
        settings["permissions"] = perms
        return settings

    try:
        settings_store.update(_mut)
    except settings_store.SettingsCorruptError:
        return
    except settings_store.SettingsWriteError:
        log.warning("permission migration write failed", exc_info=True)


_WEB_TOOL_NAMES = ("web_search", "web_fetch")


def ensure_web_permissions() -> None:
    """Idempotent migration (sibling of agents.ensure_web_tools).

    ``paths._DEFAULT_SETTINGS`` seeds the web tools into ``permissions.allow``
    only for FRESH installs (ensure_layout writes defaults when settings.json is
    missing). Upgraded installs never get them, so with ``bypass_permissions``
    OFF every web_search/web_fetch falls through to the default ``ask`` and
    interrupts the turn — worst on headless goal-continuation turns, which park
    at the prompt indefinitely. Add the two names to ``allow`` unless the user
    already mentions them in any bucket (a deliberate deny/ask is respected).
    """
    _append_allow(_WEB_TOOL_NAMES)

# Browser tools (browser-companion-extension-design.md §6): read tools and
# visible action tools land in allow; browser_js (arbitrary code execution)
# and browser_file_upload (files leaving through the browser) deliberately
# stay OUT — they fall through to the default ask, which surfaces the
# permission prompt. browser_handoff/browser_stop are side-effect-free waits.
_BROWSER_ALLOW_TOOLS = (
    "browser_computer", "browser_read_page", "browser_find",
    "browser_form_input", "browser_navigate", "browser_page_text",
    "browser_console", "browser_network", "browser_resize_window",
    "browser_tabs_context", "browser_tabs_create", "browser_tabs_close",
    "browser_handoff", "browser_stop",
)


def ensure_browser_permissions() -> None:
    """Idempotent migration, same contract as ensure_web_permissions."""
    _append_allow(_BROWSER_ALLOW_TOOLS)

# relay_answer (worker ask_user 中断透传, 2026-11-21): the tool the SUBSCRIBER's
# agent calls to hand the user's answer back to a parked worker. It is gated by
# its own qid credential chain (one-shot qid + tombstone + choice hard-match)
# and carries the user's explicit answer, so an "ask" prompt on top of it adds
# a second confirmation of a decision the user just made — user decision
# 2026-11-21: default allow. A deliberate deny/ask rule still wins.
_RELAY_ALLOW_TOOLS = ("relay_answer",)


def ensure_relay_permissions() -> None:
    """Idempotent migration, same contract as ensure_web_permissions."""
    _append_allow(_RELAY_ALLOW_TOOLS)
