"""Session Tool Policy — 会话级 MCP/Skill 按需禁用（P0，runtime 侧）。

设计：docs/design/session-tool-policy-design.md（2026-11-12 拍板）：
- 黑名单制：``session.meta.tool_policy = {mode, mcp_disabled[], skills_disabled[]}``
- 裁剪对象仅 MCP servers + **非内置** skills——builtin 工具与内置 skill
  （BUILTIN_SKILLS）一律不参与（用户拍板 ②）
- 生效点：build/refresh 时过滤 ``all_langchain_tools()``（MCP 工具名带
  ``mcp_<server-slug>_`` 前缀，砍 server = 砍整组）；world_state skills
  索引过滤；use_skill 调禁用项返回明确错误
- 下一 turn 生效；运行中 turn 不热插拔
- 继承：subagent/worker/delegation 复制父会话 policy（api/sessions.py
  建子会话时带入）
"""

from __future__ import annotations

from typing import Any, Iterable

from .mcp.naming import _slug, server_prefix

# 内置 skill（不参与裁剪——用户拍板 ②）。browser 是唯一随 Ginno 发行、
# 且与 browser_* 工具集联动的内置 skill；后续新增内置 skill 在此登记。
BUILTIN_SKILLS = frozenset({"browser"})

VALID_MODES = frozenset({"all", "minimal", "custom"})


def parse(session: dict | None) -> dict:
    """规范化 session（运行时 dict 或 meta）里的 tool_policy。

    坏数据一律降级 all（现状全量）——policy 永不阻塞工具构建。
    """
    raw = (session or {}).get("tool_policy")
    if not isinstance(raw, dict):
        return {
            "mode": "all",
            "mcp_disabled": set(),
            "mcp_tools_disabled": {},
            "skills_disabled": set(),
        }
    mode = raw.get("mode") if raw.get("mode") in VALID_MODES else "all"
    mcp_raw = raw.get("mcp_disabled")
    sk_raw = raw.get("skills_disabled")
    tools_raw = raw.get("mcp_tools_disabled")
    tools_disabled: dict[str, set[str]] = {}
    if isinstance(tools_raw, dict):
        for server, names in tools_raw.items():
            if isinstance(names, (list, tuple, set)):
                clean = {str(x) for x in names if str(x).startswith("mcp_")}
                if clean:
                    tools_disabled[str(server)] = clean
    return {
        "mode": mode,
        "mcp_disabled": (
            {str(x) for x in mcp_raw} if isinstance(mcp_raw, (list, tuple, set)) else set()
        ),
        "mcp_tools_disabled": tools_disabled,
        "skills_disabled": (
            {str(x) for x in sk_raw} if isinstance(sk_raw, (list, tuple, set)) else set()
        ),
    }


def _disabled_tool_names(policy: dict) -> set[str]:
    """Flatten tool-level blacklist (graph-facing full names only).

    Only ``mcp_*`` names are honoured: builtin tools must never be hideable
    through a session policy (2026-11-12 拍板 ②).
    """
    out: set[str] = set()
    for names in policy.get("mcp_tools_disabled", {}).values():
        out |= set(names)
    return {n for n in out if n.startswith("mcp_")}


def _mcp_disabled_prefixes(policy: dict) -> set[str]:
    """minimal = 全部 server 关；custom = 黑名单前缀集合。"""
    if policy["mode"] == "minimal":
        return {"*"}  # 通配：匹配任意 server 前缀
    return {server_prefix(s) for s in policy["mcp_disabled"]}


def filter_mcp_tools(tools: list, session: dict | None) -> list:
    """按 policy 过滤 langchain MCP 工具列表（保持入参顺序/排序）。

    两层黑名单叠加：server 级（``mcp_disabled``，砍整组前缀）与工具级
    （``mcp_tools_disabled``，按 graph 面向全名精确剔除）。mode=all 只跳过
    server 级，工具级仍生效。
    """
    policy = parse(session)
    disabled_tools = _disabled_tool_names(policy)
    prefixes = (
        _mcp_disabled_prefixes(policy) if policy["mode"] != "all" else set()
    )
    if not disabled_tools and not prefixes:
        return tools
    wildcard = "*" in prefixes
    # 通配（minimal）只砍 MCP 前缀工具；builtin 工具（read_file/bash/…）
    # 无 mcp_ 前缀，一律保留（用户拍板②：builtin 不参与裁剪）。
    if wildcard:
        return [
            t for t in tools if not t.name.startswith("mcp_") and t.name not in disabled_tools
        ]
    return [
        t
        for t in tools
        if t.name not in disabled_tools
        and not any(t.name.startswith(p) for p in prefixes)
    ]


def mcp_tool_allowed(tool_name: str, session: dict | None) -> bool:
    policy = parse(session)
    if tool_name in _disabled_tool_names(policy):
        return False
    if policy["mode"] == "all":
        return True
    prefixes = _mcp_disabled_prefixes(policy)
    if "*" in prefixes:
        return False
    return not any(tool_name.startswith(p) for p in prefixes)


def skill_visible(name: str, session: dict | None) -> bool:
    """world_state skills 索引过滤 + use_skill 门（内置 skill 永真）。"""
    if name in BUILTIN_SKILLS:
        return True
    policy = parse(session)
    if policy["mode"] == "minimal":
        return False
    return name not in policy["skills_disabled"]


def skill_denial(name: str, session: dict | None) -> str | None:
    """use_skill 调被禁 skill 时的明确错误文案；None=放行。"""
    if skill_visible(name, session):
        return None
    return (
        f"[error] skill '{name}' 已被本会话的工具策略禁用"
        "（输入框 → 上下文管理 可重新开启）。"
    )


def inherit(parent_session: dict | None) -> dict[str, Any]:
    """子会话（subagent/worker/delegation）继承父 policy 的 meta 片段。

    返回可直接存子 meta 的 dict；父无 policy 返回空 dict（=all）。
    """
    policy = parse(parent_session)
    if (
        policy["mode"] == "all"
        and not policy["mcp_disabled"]
        and not policy["mcp_tools_disabled"]
        and not policy["skills_disabled"]
    ):
        return {}
    tools_disabled = {
        server: sorted(names)
        for server, names in (policy.get("mcp_tools_disabled") or {}).items()
        if names
    }
    return {
        "tool_policy": {
            "mode": policy["mode"],
            "mcp_disabled": sorted(policy["mcp_disabled"]),
            "mcp_tools_disabled": tools_disabled,
            "skills_disabled": sorted(policy["skills_disabled"]),
        }
    }
