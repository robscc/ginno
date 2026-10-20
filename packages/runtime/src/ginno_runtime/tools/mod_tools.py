"""Mod-registered tools as model-facing StructuredTools (claude-code-mods-design.md
§5.4 P1 / §15.8).

A mod calls ``$.tool.register {name, description?, inputSchema?}`` — the entry
lands in the registry below, and the NEXT turn's toolset carries it under the
DSH-compatible name ``mcp__<plugin>__<tool>``. The model's call routes through
``ModTool._arun`` → broker ``tool.call`` event → the owning mod's runner → its
``onPress``-style callback answers ``{output}``. Registry lifetime follows the
runner (channel ``mod.state.changed`` clears the mod's entries), mirroring
mod_commands' connection-scoped design.

The reverse direction (a mod calling a host tool) is ``build_host_tool`` — a
lookup against ``build_all_tools`` for one-shot invocation (op ``tool.call``),
never cached.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

log = logging.getLogger("ginno.mods")

# 注册名约束与 broker/DSH 的命名规则一致(§15.8):插件名 <mod>、工具名 <tool>
# 都取 [A-Za-z0-9_-],全名 mcp__<mod>__<tool>。
_MOD_TOOL_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# mod → {tool_name → entry}。连接生命周期所有(channel 断开/mod 停用即清)。
_REGISTRY: dict[str, dict[str, dict]] = {}

# 一次 tool.call 事件等 owning runner 回答的上限——mod 的 handler 挂死不能
# 拖住模型的 turn(DEFAULT_DEADLINE 是 10s,这里给足余量到 60s)。
_TOOL_CALL_TIMEOUT_S = 60.0


def full_name(mod: str, tool: str) -> str:
    return f"mcp__{mod}__{tool}"


def register_tool(mod: str, name: str, description: str = "", schema: dict | None = None) -> dict:
    """``$.tool.register`` backend. Raises ValueError on a bad name (the op
    layer maps it to invalid-argument). Re-registering the same name replaces
    (hot-reload friendly), same name under a different mod is refused."""
    mod = (mod or "").strip()
    name = (name or "").strip()
    if not mod or not _MOD_TOOL_RE.match(name):
        raise ValueError(f"invalid tool name: {name!r}")
    for other, tools in _REGISTRY.items():
        if other != mod and name in tools:
            raise ValueError(f"tool name {name!r} already registered by mod {other!r}")
    entry = {
        "name": name,
        "fullName": full_name(mod, name),
        "mod": mod,
        "description": description or "",
        "inputSchema": schema or {"type": "object", "properties": {}},
    }
    _REGISTRY.setdefault(mod, {})[name] = entry
    log.info("mod_tool_registered mod=%s tool=%s", mod, name)
    return entry


def unregister_mod(mod: str) -> None:
    """Drop every tool owned by ``mod`` (runner went away / disabled)."""
    if _REGISTRY.pop(mod, None) is not None:
        log.info("mod_tools_cleared mod=%s", mod)


def owns_tool(name: str) -> bool:
    return lookup_tool(name) is not None


def lookup_tool(full: str) -> dict | None:
    """``mcp__<mod>__<tool>`` → entry (None for anything else)."""
    parts = (full or "").split("__")
    if len(parts) != 3 or parts[0] != "mcp":
        return None
    return _REGISTRY.get(parts[1], {}).get(parts[2])


def list_tools() -> list[dict]:
    out: list[dict] = []
    for tools in _REGISTRY.values():
        out.extend(dict(e) for e in tools.values())
    return out


async def call_mod_tool(full: str, args: dict, session_id: str) -> str:
    """Invoke a mod-owned tool: the broker routes a ``tool.call`` event to the
    owning mod's runner and the handler's ``{output}`` settles it."""
    from ..mods.events import maybe_dispatch

    entry = lookup_tool(full)
    if entry is None:
        return f"[error] unknown tool: {full}"
    payload = {
        "sessionId": session_id,
        "tool": entry["name"],
        "toolName": entry["fullName"],
        "args": args or {},
    }
    try:
        result = await asyncio.wait_for(
            maybe_dispatch(session_id, "tool.call", payload, deadline_ms=55_000),
            timeout=_TOOL_CALL_TIMEOUT_S,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return "[error] mod tool call timed out"
    settle = result if isinstance(result, dict) else {}
    for key in ("output", "result", "text"):
        v = settle.get(key)
        if isinstance(v, str) and v:
            return v
    return "[error] mod tool produced no output"


# ---- model-side wiring -----------------------------------------------------------


def build_mod_tools(session_id: str | None) -> list:
    """One StructuredTool per registered mod tool (graph.py build_all_tools
    consumer). ``[]`` without a session — same convention as the skill/goal
    tools: no socket to answer over."""
    if not session_id or not _REGISTRY:
        return []

    out: list = []
    for entry in list_tools():
        out.append(_wrap(entry, session_id))
    return out


def _wrap(entry: dict, session_id: str):
    import json

    from pydantic import BaseModel, create_model

    schema = entry.get("inputSchema") or {}
    props = schema.get("properties") if isinstance(schema, dict) else None
    fields: dict[str, Any] = {}
    if isinstance(props, dict):
        required = set(schema.get("required") or [])
        for pname, pdef in props.items():
            if not isinstance(pdef, dict):
                continue
            py = (pdef.get("type") or "string") if isinstance(pdef, dict) else "string"
            tmap = {
                "string": (str, ...),
                "number": (float, ...),
                "integer": (int, ...),
                "boolean": (bool, ...),
            }
            ann = tmap.get(py, (str, ...))
            default = pdef.get("default")
            optional = pname not in required and default is not None
            fields[str(pname)] = (*ann, default) if optional else ann
    ArgsModel: type[BaseModel] = (
        create_model(f"{entry['fullName']}_args", **fields)
        if fields
        else create_model(f"{entry['fullName']}_args")
    )

    async def _arun(**kwargs: Any) -> str:
        # 输出是 mod 的回答——UNTRUSTED DATA,与 MCP/委托工具同一纪律:
        # 模型侧的描述已声明,这里不加过滤(过滤会破坏结构化输出)。
        payload = {k: v for k, v in kwargs.items() if v is not None}
        try:
            return await call_mod_tool(entry["fullName"], payload, session_id)
        except Exception as e:  # noqa: BLE001 — never kill the turn
            return f"[error] {type(e).__name__}: {e}"

    from langchain_core.tools import StructuredTool

    return StructuredTool(
        name=entry["fullName"],
        description=(entry.get("description") or f"Tool provided by the {entry['mod']} mod.")[
            :1024
        ],
        args_schema=ArgsModel,
        coroutine=_arun,
        tags=["mod"],
        metadata={"json": json.dumps({"mod": entry["mod"]})},
    )


def build_host_tool(name: str) -> Any | None:
    """One-shot lookup for op ``tool.call``: build the session-less host
    toolset and find ``name`` in it. Never cached (registry may change turn to
    turn); returns None when the name is unknown."""
    from .. import graph

    try:
        tools = graph.build_all_tools()
    except Exception:  # noqa: BLE001 — sessionless build must not raise here
        log.exception("mod_host_toolset_build_failed")
        return None
    return next((t for t in tools if getattr(t, "name", "") == name), None)
