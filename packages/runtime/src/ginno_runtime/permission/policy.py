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
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from .. import paths

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
            p = paths.settings_path()
            settings = json.loads(p.read_text() or "{}") if p.exists() else {}
        perms = settings.get("permissions", {})
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
    the next tool call without rebuilding sessions."""
    if settings is None:
        p = paths.settings_path()
        settings = json.loads(p.read_text() or "{}") if p.exists() else {}
    return bool(settings.get("bypass_permissions", True))
