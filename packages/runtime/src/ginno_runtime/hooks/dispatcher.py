"""Hook event dispatcher.

Settings format (Claude-Code-inspired):

    ~/.ginno/settings.json
    {
      "hooks": {
        "PreToolUse":     [{"matcher": "Bash", "command": "python ~/.ginno/hooks/pre_bash.py"}],
        "PostToolUse":    [{"matcher": "Write", "command": "..."}],
        "UserPromptSubmit":[{"command": "..."}],
        "Stop":           [{"command": "..."}],
        "SessionStart":   [{"command": "..."}]
      }
    }

Dispatcher pipes a JSON context to the hook process stdin and reads a JSON
response on stdout. Response fields:
  - {"block": true, "reason": "..."}   → block the action
  - {"inject": "..."}                  → add context to state
  - {"rewrite": "..."}                 → rewrite the user prompt
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .. import paths

HookEventName = Literal[
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "Stop",
]


@dataclass
class HookEvent:
    name: HookEventName
    context: dict[str, Any]


@dataclass
class HookResult:
    block: bool = False
    reason: str = ""
    inject: str | None = None
    rewrite: str | None = None


class HookDispatcher:
    def __init__(self, settings: dict[str, Any] | None = None) -> None:
        self.settings = settings or {}

    @classmethod
    def from_settings(cls) -> "HookDispatcher":
        p = paths.settings_path()
        if not p.exists():
            return cls(settings={})
        return cls(settings=json.loads(p.read_text() or "{}"))

    def _hooks_for(self, event: HookEventName, matcher: str | None) -> list[dict[str, Any]]:
        hooks = self.settings.get("hooks", {}).get(event, [])
        if matcher is None:
            return [h for h in hooks if not h.get("matcher")]
        return [h for h in hooks if not h.get("matcher") or h.get("matcher") == matcher]

    async def dispatch(self, event: HookEvent, matcher: str | None = None) -> list[HookResult]:
        results: list[HookResult] = []
        for h in self._hooks_for(event.name, matcher):
            cmd = h.get("command")
            if not cmd:
                continue
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                payload = json.dumps({"event": event.name, **event.context})
                stdout, _ = await proc.communicate(payload.encode())
                if stdout:
                    data = json.loads(stdout)
                    results.append(
                        HookResult(
                            block=bool(data.get("block", False)),
                            reason=data.get("reason", ""),
                            inject=data.get("inject"),
                            rewrite=data.get("rewrite"),
                        )
                    )
            except Exception:
                continue
        return results
