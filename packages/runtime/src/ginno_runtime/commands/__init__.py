"""Chat commands — slash commands (builtins + skills) and @mentions.

- ``registry``: extensible built-in command registry (V1 ships ``/help``).
- ``resolver``: turns one raw invoke message into a ``TurnPlan`` — builtin
  short-circuit, skill substitution, and @mention resolution.

See docs/commands-and-mentions-design.md.
"""

from .registry import BUILTINS, BuiltinCommand
from .resolver import (
    TurnPlan,
    parse_mention_tokens,
    parse_slash,
    resolve_mentions,
    resolve_turn,
    substitute_skill,
)

__all__ = [
    "BUILTINS",
    "BuiltinCommand",
    "TurnPlan",
    "parse_slash",
    "parse_mention_tokens",
    "resolve_mentions",
    "resolve_turn",
    "substitute_skill",
]
