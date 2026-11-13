"""Context breakdown for the 上下文管理 panel (2026-11-13 P0).

Answers "where did this session's prompt go?" from local data only:

* ``tools``  — per MCP server: enabled/disabled tool counts + token cost of
  the langchain schema actually sent to the model (name + description + args).
* ``skills`` — the exact one-line skill index WorldState injects at this
  session's policy, measured with the same token counter.
* ``system`` — stable system prompt (persona + WorldState + memory) minus the
  skills index (reported separately to avoid double counting).
* ``history`` — ``last prompt_tokens - fixed parts`` (>= 0). The provider's
  recorded prompt_tokens is the source of truth for "current context size".
* ``window`` / ``threshold`` — same resolution chain as the TopBar ring and
  compaction (never forks its own logic).

All figures are approximations (``≈``): tiktoken is not the provider's
tokenizer. The frontend shows the counter name and keeps the ``≈``.
"""

from __future__ import annotations

import json
from typing import Any

from .mcp.naming import server_prefix
from .tool_policy import filter_mcp_tools, parse
from .token_count import count_tokens, counter_name


def _tool_text(tool: Any) -> str:
    """The schema text a provider actually receives for one tool."""
    description = getattr(tool, "description", "") or ""
    try:
        schema = json.dumps(
            getattr(tool, "args", {}) or {},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
    except Exception:  # noqa: BLE001 — a weird schema must not hide the panel
        schema = ""
    return f"{tool.name}\n{description}\n{schema}"


def _server_of(tool_name: str, server_names: list[str]) -> str | None:
    """Longest matching server prefix wins (servers may prefix each other)."""
    best: str | None = None
    best_len = -1
    for name in server_names:
        prefix = server_prefix(name)
        if tool_name.startswith(prefix) and len(prefix) > best_len:
            best, best_len = name, len(prefix)
    return best


def build(session_id: str, session: dict | None) -> dict:
    """Return one panel payload; never raises."""
    from . import server_shared as shared

    info = dict(session or {})
    # meta uses provider/model; live sessions use model_provider/model_name.
    if not info.get("model_provider"):
        info["model_provider"] = info.get("provider") or ""
    if not info.get("model_name"):
        info["model_name"] = info.get("model") or ""
    policy = parse(info)
    registry = getattr(shared, "_mcp", None)

    all_tools = []
    status: list[dict] = []
    if registry is not None:
        try:
            all_tools = list(registry.all_langchain_tools())
        except Exception:  # noqa: BLE001
            all_tools = []
        try:
            status = list(registry.status())
        except Exception:  # noqa: BLE001
            status = []

    try:
        enabled_tools = filter_mcp_tools(all_tools, info)
    except Exception:  # noqa: BLE001
        enabled_tools = all_tools
    enabled_names = {t.name for t in enabled_tools}
    # The real system prompt is measured against the same builtin+MCP toolset
    # the graph passes to build_stable_system (permissions/tool guidance
    # sections depend on it). Failures fall back to the MCP tools only.
    built_tools = enabled_tools
    try:
        from .graph import build_all_tools

        built_tools = build_all_tools(
            enabled_tools,
            workspace=str(info.get("workspace") or ""),
            project_slug=str(info.get("project_slug") or "default"),
            session_id=session_id,
            context_dirs=list(info.get("context_dirs") or []),
            primary_path=str(info.get("primary_path") or ""),
            subagent_depth=info.get("depth") if info.get("type") == "subagent" else None,
            restrict_tools=list(info.get("restrict_tools") or []),
        )
    except Exception:  # noqa: BLE001
        built_tools = enabled_tools

    server_names = [str(s.get("name") or "") for s in status if s.get("name")]

    by_server: dict[str, list[Any]] = {n: [] for n in server_names if n}
    for t in all_tools:
        owner = _server_of(t.name, server_names)
        if owner is not None:
            by_server.setdefault(owner, []).append(t)

    tools_parts: list[dict] = []
    for s in status:
        name = str(s.get("name") or "")
        if not name:
            continue
        server_tools = by_server.get(name, [])
        measured = [(t, count_tokens(_tool_text(t))) for t in server_tools]
        live = [(t, n) for t, n in measured if t.name in enabled_names]
        off = [(t, n) for t, n in measured if t.name not in enabled_names]
        tools_parts.append(
            {
                "server": name,
                "connected": bool(s.get("connected")),
                "enabled": s.get("enabled") is not False,
                "error": s.get("error") or "",
                "tools": int(s.get("tools") or len(server_tools)),
                "enabledTools": len(live),
                "tokens": sum(n for _, n in live),
                "disabledTokens": sum(n for _, n in off),
                # Per-tool costs let the panel preview tool-level switches
                # without a second measurement round-trip.
                "byTool": [
                    {"name": t.name, "tokens": n, "enabled": t.name in enabled_names}
                    for t, n in measured
                ],
            }
        )

    tools_total = sum(p["tokens"] for p in tools_parts)

    # Skills index — use the production section so filtering/budget match the
    # prompt byte-for-byte.
    skills_tokens = 0
    skills_count = 0
    skills: dict[str, Any] = {"count": 0, "disabled": len(policy["skills_disabled"]), "tokens": 0}
    try:
        from .world_state import SessionCtx, SkillsSection, _agent_by_id

        ctx = SessionCtx(
            session_id=session_id,
            project_slug=str(info.get("project_slug") or "default"),
            agent_id=info.get("agent_id"),
            mcp_tool_names=[t.name for t in enabled_tools],
            all_tool_names=[t.name for t in built_tools],
            agent=_agent_by_id(info.get("agent_id")),
            workspace=str(info.get("workspace") or ""),
            context_dirs=list(info.get("context_dirs") or []),
            primary_path=str(info.get("primary_path") or ""),
            tool_policy=info,
        )
        snap = SkillsSection().snapshot(ctx) or {}
        index = str(snap.get("index") or "")
        names = list(snap.get("names") or [])
        skills_tokens = count_tokens(index)
        skills = {
            "count": len(names),
            "disabled": len(policy["skills_disabled"]),
            "tokens": skills_tokens,
        }
    except Exception:  # noqa: BLE001
        pass

    # Stable system prompt. Building it is the most expensive step; failures
    # degrade to 0 rather than hiding the panel.
    system_tokens = 0
    try:
        from .graph_prompts import build_stable_system
        from .world_state import _agent_by_id

        stable = build_stable_system(
            _agent_by_id(info.get("agent_id")),
            str(info.get("project_slug") or "default"),
            built_tools,
            agent_id=info.get("agent_id"),
            mcp_tool_names=[t.name for t in enabled_tools],
            session_id=session_id,
            workspace=str(info.get("workspace") or ""),
            context_dirs=list(info.get("context_dirs") or []),
            primary_path=str(info.get("primary_path") or ""),
            tool_policy=info,
        )
        system_tokens = max(0, count_tokens(stable) - skills_tokens)
    except Exception:  # noqa: BLE001
        pass

    # Current context size + window/threshold — one chain with TopBar/compaction.
    try:
        from .usage import context_state, last_chat_input_tokens

        used = last_chat_input_tokens(session_id)
    except Exception:  # noqa: BLE001
        used = None
    fixed = tools_total + skills_tokens + system_tokens
    measured = used is not None
    if used is None:
        used = fixed

    provider = str(info.get("model_provider") or info.get("provider") or "")
    model = str(info.get("model_name") or info.get("model") or "")
    if not provider or not model:
        try:
            from .session_meta import resolve_session_model

            p, m = resolve_session_model(session_id)
            provider = provider or p
            model = model or m
        except Exception:  # noqa: BLE001
            pass
    try:
        from .usage import context_state

        window = int(context_state(used, provider, model).get("window") or 0)
    except Exception:  # noqa: BLE001
        window = 200_000
    try:
        from .compaction import _effective_compact_threshold
        from .world_state import context_settings

        configured = int(_effective_compact_threshold(context_settings(), info))
        # Same intent as compaction (min(setting, window×0.75)); clamp here too
        # so a provider with an unknown window (0) cannot render a threshold
        # ABOVE the window the ring shows.
        threshold = min(configured, int(window * 0.75)) if window else configured
    except Exception:  # noqa: BLE001
        threshold = min(500_000, int(window * 0.75)) if window else 500_000

    history = max(0, used - fixed)
    return {
        "ok": True,
        "used": used,
        "measured": measured,
        "window": window,
        "threshold": threshold,
        "counter": counter_name(),
        "tools": tools_parts,
        "toolsTotal": tools_total,
        "skills": skills,
        "system": system_tokens,
        "history": history,
        "fixed": fixed,
        "policy": {
            "mode": policy["mode"],
            "mcp_disabled": sorted(policy["mcp_disabled"]),
            "mcp_tools_disabled": {
                k: sorted(v) for k, v in policy["mcp_tools_disabled"].items()
            },
            "skills_disabled": sorted(policy["skills_disabled"]),
        },
    }