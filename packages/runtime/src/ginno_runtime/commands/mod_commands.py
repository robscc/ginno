"""Mod-registered slash commands (claude-code-mods-design.md §5.3 ``command.*``).

A mod calls ``$.command.register {name, description?}`` — the broker forwards
it to the Python backend op (mods/ops.py), which lands here. When the user
types ``/name`` and neither the builtin registry nor a user-invocable skill
matches, the resolver consults this registry and the WS layer raises a
``command.run`` event the broker routes to the owning mod's runner.

In-memory by design: the registry is rebuilt from the mods themselves on every
channel (re)connect — the runner re-runs its register() after a reload, and
ModChannel._teardown_connection clears the table so a dead connection can't
leave ghost commands behind. Builtins and skills always keep priority (P1 不
拦截内置命令): a mod command can never shadow ``/help`` or a skill.
"""

from __future__ import annotations

import re

# Same shape as mod names (design §15.3) — a command name is a bare token.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# name -> {"name", "mod", "description"}
_registry: dict[str, dict] = {}


def register(name: str, mod: str, description: str = "") -> dict:
    """Register (or re-register — last write wins) ``name`` for ``mod``.
    Raises ValueError on a malformed name so the op surfaces a named failure
    to the mod instead of silently swallowing the command."""
    name = (name or "").strip()
    mod = (mod or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid command name {name!r} (expected [A-Za-z0-9_-], 1-64 chars)")
    if not mod:
        raise ValueError("command.register requires the calling mod on the frame")
    entry = {"name": name, "mod": mod, "description": description or ""}
    _registry[name] = entry
    return dict(entry)


def unregister_mod(mod: str) -> None:
    """Drop every command owned by ``mod`` (runner went away / disabled)."""
    for name in [n for n, e in _registry.items() if e.get("mod") == mod]:
        _registry.pop(name, None)


def lookup(name: str) -> dict | None:
    entry = _registry.get((name or "").strip())
    return dict(entry) if entry else None


def list_commands() -> list[dict]:
    return [dict(e) for _, e in sorted(_registry.items())]


def clear() -> None:
    _registry.clear()
