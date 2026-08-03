"""Shared helpers for agent-style nodes (moved out of ``compiler`` in round 3)."""

from __future__ import annotations

import json
import re

from ...graph import build_agent_system_prompt

# Marker the step system prompt asks the model to use for context write-back.
WRITE_OPEN = "WRITE_JSON"


def build_system(goal: str, context: dict, agent) -> str:
    base = build_agent_system_prompt(agent, "default", [], query="")
    return (
        f"{base}\n\n"
        "## Your step goal\n"
        f"{goal}\n\n"
        "## Current workflow context\n"
        f"{json.dumps(context, ensure_ascii=False)}\n\n"
        "Use your tools as needed to achieve the goal. When you are done, if this "
        "step must update the workflow context, end your reply with a single JSON "
        "object on its own line prefixed by WRITE_JSON containing ONLY the fields to "
        "write, e.g. `WRITE_JSON {\"drafts\": [\"...\"]}`. Do not wrap it in code fences."
    )


def extract_write_json(text: str) -> str | None:
    """Return the first brace-balanced JSON object following WRITE_OPEN (string-aware)."""
    i = text.find(WRITE_OPEN)
    if i < 0:
        return None
    j = text.find("{", i + len(WRITE_OPEN))
    if j < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for k in range(j, len(text)):
        c = text[k]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[j : k + 1]
    return None


def parse_writes(text: str) -> dict:
    if not text:
        return {}
    frag = extract_write_json(text)
    if not frag:
        return {}
    try:
        data = json.loads(frag)
        return data if isinstance(data, dict) else {}
    except json.JSONDecodeError:
        return {}
