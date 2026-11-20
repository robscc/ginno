"""Command protocol — the leaf shared by all command packages.

``BuiltinCommand`` and the shared command logger live here (not in
``registry.py``) so the per-domain packages can import the protocol without
a circular dependency: registry assembles BUILTINS *from* the packages, the
packages never import registry at module load time. Importers keep using
``ginno_runtime.commands.registry.BuiltinCommand`` — registry re-exports it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_log = logging.getLogger("ginno.commands")


@dataclass(frozen=True)
class BuiltinCommand:
    name: str
    # Bilingual (en, zh) — rendered through t() at reply time (locale is
    # request-scoped; module import time would freeze the wrong locale).
    description: tuple[str, str]
    # (project_slug, session, args) -> reply text. session/args may be None
    # for commands that need no session context.
    handler: Callable[..., str]
    # Optional async variant for commands that do real server work (LLM
    # calls, state rewrites). When set, the resolver routes the command to
    # the async path (TurnPlan.builtin_async) and the WS invoke handler
    # awaits it — busy-gated, because unlike a pure sync reply it can touch
    # thread state that must never race a live turn.
    async_handler: Callable[..., Awaitable[str]] | None = None
    # Channel-side (WeCom/DingTalk) visibility policy (docs/design/
    # channel-devswap-unattended-design.md §2):
    #   "all"   — anyone in the bound chat may trigger (default, compat);
    #   "admin" — only userids in the channel's admin_users config;
    #   "none"  — channel cannot trigger at all (desktop-only, reserved).
    # Enforced in channels/base.run_channel_turn; desktop WS never checks.
    channel_policy: str = "all"
