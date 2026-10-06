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
import os
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
    "SessionEnd",
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
        # Plugin hooks (claude-code-mods-design.md §10): classic-shape
        # ``hooks/hooks.json`` entries from installed mods, flattened to the
        # same {matcher, command} shape as settings hooks. ``plugin_root``
        # rides into the child env as CLAUDE_PLUGIN_ROOT. The "modules" JS
        # shape never lands here — those hooks go to the mods broker.
        self._plugin_hooks: dict[HookEventName, list[dict[str, Any]]] = {}

    @classmethod
    def from_settings(cls) -> "HookDispatcher":
        p = paths.settings_path()
        if not p.exists():
            return cls(settings={})
        return cls(settings=json.loads(p.read_text() or "{}"))

    def _hooks_for(self, event: HookEventName, matcher: str | None) -> list[dict[str, Any]]:
        hooks = self.settings.get("hooks", {}).get(event, [])
        if matcher is None:
            matched = [h for h in hooks if not h.get("matcher")]
            matched += [h for h in self._plugin_hooks.get(event, []) if not h.get("matcher")]
            return matched
        matched = [h for h in hooks if not h.get("matcher") or h.get("matcher") == matcher]
        matched += [
            h
            for h in self._plugin_hooks.get(event, [])
            if not h.get("matcher") or h.get("matcher") == matcher
        ]
        return matched

    def register_plugin(self, name: str, hooks_doc: dict[str, Any], plugin_root: str) -> int:
        """Register one mod's classic-shape hooks.json::

            {"hooks": {"SessionStart": [{"hooks": [
                {"type": "command", "command": "${CLAUDE_PLUGIN_ROOT}/hooks/x.sh"}
            ]}]}}

        Returns the number of commands registered. Idempotent per plugin: a
        re-registration (settings-page reload / reinstall) replaces the
        previous entries of the same plugin name. ${CLAUDE_PLUGIN_ROOT} in the
        command is substituted literally; the env var is injected too so
        scripts the hook spawns can resolve it."""
        groups = hooks_doc.get("hooks") if isinstance(hooks_doc, dict) else None
        if not isinstance(groups, dict):
            return 0
        # Drop this plugin's previous entries (re-registration = replace).
        for evt in list(self._plugin_hooks):
            self._plugin_hooks[evt] = [
                h for h in self._plugin_hooks[evt] if h.get("plugin") != name
            ]
        count = 0
        for event, entries in groups.items():
            if event not in HookEventName.__args__ or not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                matcher = entry.get("matcher") or None
                for hook in entry.get("hooks") or []:
                    if not isinstance(hook, dict) or hook.get("type") not in (None, "command"):
                        continue
                    cmd = hook.get("command")
                    if not cmd:
                        continue
                    self._plugin_hooks.setdefault(event, []).append(
                        {
                            "matcher": matcher if isinstance(matcher, str) else None,
                            "command": str(cmd).replace("${CLAUDE_PLUGIN_ROOT}", plugin_root),
                            "plugin": name,
                            "plugin_root": plugin_root,
                        }
                    )
                    count += 1
        return count

    def plugin_hook_count(self) -> int:
        return sum(len(v) for v in self._plugin_hooks.values())

    async def dispatch(self, event: HookEvent, matcher: str | None = None) -> list[HookResult]:
        results: list[HookResult] = []
        for h in self._hooks_for(event.name, matcher):
            cmd = h.get("command")
            if not cmd:
                continue
            # Plugin hooks run with CLAUDE_PLUGIN_ROOT=<mod dir> (§10 bridging).
            env: dict[str, str] | None = None
            root = h.get("plugin_root")
            if root:
                env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(root)}
            try:
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
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
