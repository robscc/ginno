"""Session Tool Policy 单测：过滤/黑名单/minimal/内置豁免/继承/坏数据降级。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ginno_runtime import tool_policy as tp


def _tool(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


ALL = [_tool("mcp_srv-zhishui_get_user"), _tool("mcp_srv-docs_read"),
       _tool("read_file"), _tool("bash")]


def test_parse_defaults_and_bad_data():
    assert tp.parse(None)["mode"] == "all"
    assert tp.parse({})["mode"] == "all"
    assert tp.parse({"tool_policy": {"mode": "bogus"}})["mode"] == "all"
    assert tp.parse({"tool_policy": {"mode": "custom",
                                     "mcp_disabled": "not-a-list"}})["mcp_disabled"] == set()


def test_all_mode_no_filter():
    out = tp.filter_mcp_tools(ALL, {"tool_policy": {"mode": "all"}})
    assert out == ALL


def test_custom_blacklist_filters_by_server_prefix():
    sess = {"tool_policy": {"mode": "custom", "mcp_disabled": ["srv-zhishui"]}}
    out = tp.filter_mcp_tools(ALL, sess)
    assert [t.name for t in out] == ["mcp_srv-docs_read", "read_file", "bash"]


def test_minimal_disables_all_mcp_but_keeps_builtin_tools():
    out = tp.filter_mcp_tools(ALL, {"tool_policy": {"mode": "minimal"}})
    assert [t.name for t in out] == ["read_file", "bash"]


def test_mcp_tool_allowed():
    sess = {"tool_policy": {"mode": "custom", "mcp_disabled": ["srv-docs"]}}
    assert tp.mcp_tool_allowed("read_file", sess) is True
    assert tp.mcp_tool_allowed("mcp_srv-docs_read", sess) is False
    assert tp.mcp_tool_allowed("mcp_srv-docs_write", sess) is False
    assert tp.mcp_tool_allowed("mcp_srv-docsx_read", sess) is True  # 前缀精确（hyphen 边界）


def test_skill_visible_builtin_exempt():
    sess = {"tool_policy": {"mode": "custom", "skills_disabled": ["todo", "browser"]}}
    assert tp.skill_visible("browser", sess) is True   # 内置豁免（拍板②）
    assert tp.skill_visible("todo", sess) is False
    assert tp.skill_denial("todo", sess) is not None
    assert tp.skill_denial("browser", sess) is None


def test_minimal_hides_non_builtin_skills_only():
    sess = {"tool_policy": {"mode": "minimal"}}
    assert tp.skill_visible("todo", sess) is False
    assert tp.skill_visible("browser", sess) is True


def test_inherit():
    parent = {"tool_policy": {"mode": "custom",
                              "mcp_disabled": ["a"], "skills_disabled": ["b"]}}
    inh = tp.inherit(parent)
    assert inh["tool_policy"]["mode"] == "custom"
    assert inh["tool_policy"]["mcp_disabled"] == ["a"]
    assert tp.inherit({"tool_policy": {"mode": "all"}}) == {}
    assert tp.inherit({}) == {}
