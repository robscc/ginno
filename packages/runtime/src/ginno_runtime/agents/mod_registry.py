"""Mod-registered agents (claude-code-mods-design.md §5.4 ``agent.*``, P2).

A mod calls ``$.agent.register {name, description}`` — the broker forwards it
to the Python backend op (mods/ops.py), which lands here. ``$.agent.list``
returns the table so a mod can discover what (it or a sibling mod) declared.
Registration is purely declarative metadata for now: ``agent.spawn`` taps the
SUBAGENT spawn path (subagent_scheduler.create_subagent) by event, not by
these names — the registry is what mods see, the event is what gates runs.

In-memory by design, mirroring commands/mod_commands.py: entries rebuild from
the mods themselves on every runner (re)connect. Unlike commands there is no
teardown hook wired yet (ModChannel._teardown_connection clears commands only),
so a disabled mod's entries persist until the runtime restarts — bounded and
harmless, listed under a stale mod name.
"""

from __future__ import annotations

import re

# Same shape as mod / command names (design §15.3) — an agent name is a bare token.
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# name -> {"name", "mod", "description"}
_registry: dict[str, dict] = {}


def register(name: str, mod: str, description: str = "") -> dict:
    """Register (or re-register — last write wins) ``name`` for ``mod``.
    Raises ValueError on a malformed name / missing mod so the op surfaces a
    named failure to the mod instead of silently swallowing the entry."""
    name = (name or "").strip()
    mod = (mod or "").strip()
    if not _NAME_RE.match(name):
        raise ValueError(f"invalid agent name {name!r} (expected [A-Za-z0-9_-], 1-64 chars)")
    if not mod:
        raise ValueError("agent.register requires the calling mod on the frame")
    entry = {"name": name, "mod": mod, "description": description or ""}
    _registry[name] = entry
    return dict(entry)


def unregister_mod(mod: str) -> None:
    """Drop every agent owned by ``mod`` (reserved for a future teardown hook)."""
    for name in [n for n, e in _registry.items() if e.get("mod") == mod]:
        _registry.pop(name, None)


def get(name: str) -> dict | None:
    entry = _registry.get((name or "").strip())
    return dict(entry) if entry else None


def list_agents() -> list[dict]:
    return [dict(e) for _, e in sorted(_registry.items())]


def clear() -> None:
    _registry.clear()
