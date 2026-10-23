"""MCP 图面向工具名的唯一实现。

服务器名允许中文 / 空格 / 符号（设置页按用户习惯显示），但发给模型的
工具名必须满足 provider 的 ``^[a-zA-Z0-9_-]+$``（2026-10-23：中文服务器名
「钉钉文档」直接拼进 ``mcp_{server}_{tool}``，OpenAI 兼容端点 400 拒绝
整个 turn）。命名同时是系统级契约——权限 pattern、``disabled_tools``、
per-turn 图指纹都按它匹配——所以只能有一个实现。

规则：
- ASCII 名字原样保留（存量配置零迁移）。
- 含非法字符时：非法段替换为 ``_``，并追加原名的稳定短哈希，避免
  「钉钉文档」/「钉钉待办」都被压成同一个 slug。
"""

from __future__ import annotations

import hashlib
import re

_INVALID = re.compile(r"[^A-Za-z0-9_-]+")

# Provider（OpenAI / Anthropic）对函数名的长度上限是 64；MCP 工具名可能很长，
# 超限时截断并保留原名的短哈希，避免两个长名截断后撞车。
_MAX_TOOL_NAME = 64


def _slug(value: str) -> str:
    raw = str(value or "")
    slug = _INVALID.sub("_", raw).strip("_")
    if slug != raw:
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]
        slug = f"{slug}_{digest}" if slug else f"srv_{digest}"
    return slug or "srv"


def server_slug(server_name: str) -> str:
    """用户的服务器名 → 图面向 ASCII 段。"""
    return _slug(server_name)


def tool_slug(tool_name: str) -> str:
    """服务器给的原始工具名 → 图面向 ASCII 段（原始名可能含点号）。"""
    return _slug(tool_name)


def server_prefix(server_name: str) -> str:
    """判断「某工具属于哪个服务器」时用的前缀。"""
    return f"mcp_{_slug(server_name)}_"


def full_tool_name(server_name: str, tool_name: str) -> str:
    name = f"{server_prefix(server_name)}{_slug(tool_name)}"
    if len(name) <= _MAX_TOOL_NAME:
        return name
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    return f"{name[: _MAX_TOOL_NAME - len(digest) - 1]}_{digest}"