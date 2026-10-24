"""MCP 工具名 ASCII 化（mcp/naming.py）。

2026-10-23：中文服务器名（「钉钉文档」）直接拼进 ``mcp_{server}_{tool}``，
OpenAI 兼容端点以 ``Invalid 'tools[N].function.name'`` 400 拒绝整个 turn。
命名是权限 pattern / disabled_tools / 图指纹的共同契约，这里把规则钉死。
"""

from __future__ import annotations

import re

from ginno_runtime.mcp import naming

VALID = re.compile(r"^[a-zA-Z0-9_-]+$")


def test_ascii_names_unchanged() -> None:
    # 存量 ASCII 配置零迁移。
    assert naming.server_slug("vault") == "vault"
    assert naming.server_prefix("vault") == "mcp_vault_"
    assert naming.full_tool_name("vault", "read_file") == "mcp_vault_read_file"


def test_chinese_server_name_is_valid_stable_and_distinct() -> None:
    a = naming.full_tool_name("钉钉文档", "search_documents")
    assert VALID.match(a), a
    assert a == naming.full_tool_name("钉钉文档", "search_documents")  # 稳定
    assert a.startswith(naming.server_prefix("钉钉文档"))
    # 不同原名不能压成同一个 slug（都含中文时尤其容易）。
    b = naming.full_tool_name("钉钉待办", "search_documents")
    assert VALID.match(b), b
    assert a != b


def test_tool_name_with_invalid_chars_is_sanitized() -> None:
    full = naming.full_tool_name("vault", "files.search")
    assert VALID.match(full), full
    tail = full[len(naming.server_prefix("vault")):]
    assert tail == naming.tool_slug("files.search")
    cn = naming.full_tool_name("vault", "搜索")
    assert VALID.match(cn), cn


def test_long_names_are_capped_at_provider_limit() -> None:
    # provider 函数名上限 64；截断后用原名哈希防碰撞。
    a = naming.full_tool_name("server", "x" * 200)
    assert len(a) <= 64, a
    assert VALID.match(a)
    assert a == naming.full_tool_name("server", "x" * 200)  # 稳定
    b = naming.full_tool_name("server", "x" * 199 + "y")
    assert len(b) <= 64 and a != b  # 只差末字符也不能撞车


def test_empty_names_degrade_safely() -> None:
    assert VALID.match(naming.full_tool_name("", ""))
    assert VALID.match(naming.server_prefix(""))


def test_real_mcp_config_prefixes_are_valid() -> None:
    """用户真实配置里的每个服务器名都要产出合法前缀。"""
    import os

    from ginno_runtime import jsonx as json

    path = os.path.expanduser("~/.ginno/mcp/mcp.json")
    try:
        data = json.load(open(path))
    except (OSError, json.JSONDecodeError):
        return  # 测试机没有真实配置时跳过
    servers = data.get("mcpServers") or data.get("servers") or {}
    for name in servers:
        assert VALID.match(naming.server_prefix(name)), name