"""MCP Apps registry layer (docs/mcp-apps-design.md §2 ①②).

_meta.ui 透传、tools_with_ui 清单、find_tool_ui、read_resource（Text/Blob
拼接）、call_tool（content/structured 提取）——API 层（tests/api/
test_mcp_apps_api.py）只测路由/权限/错误码，协议细节都在这里钉。
"""

from __future__ import annotations

import base64

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.mcp import registry as reg_mod
from ginno_runtime.mcp.registry import (
    MCPNotConnectedError,
    MCPRegistry,
    MCPServerConfig,
    _content_texts,
    _full_tool_name,
)

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# Fakes：带 _meta.ui 的工具 + 可编程的 FakeSession（挂 live.session）
# --------------------------------------------------------------------------- #
class _UiTool:
    """模拟 SDK mcp.types.Tool：meta 属性即线上 _meta（alias）。"""

    def __init__(self, name: str, ui: dict | None = None) -> None:
        self.name = name
        self.description = f"desc {name}"
        self.inputSchema = {"type": "object", "properties": {}}
        if ui is not None:
            self.meta = {"ui": ui}


class _FakeSession:
    """可编程的 ClientSession 替身：resources / call_tool 结果预置。"""

    def __init__(self, resources: dict | None = None, call_results: dict | None = None) -> None:
        self.resources = resources or {}
        self.call_results = call_results or {}
        self.read_uris: list[str] = []
        self.calls: list[tuple[str, dict]] = []

    async def read_resource(self, uri: str):
        self.read_uris.append(str(uri))
        if str(uri) not in self.resources:
            raise KeyError(uri)
        v = self.resources[str(uri)]
        if isinstance(v, Exception):
            raise v
        return v

    async def call_tool(self, name: str, args: dict):
        self.calls.append((name, dict(args or {})))
        if name not in self.call_results:
            raise KeyError(name)
        v = self.call_results[name]
        if isinstance(v, Exception):
            raise v
        return v


def _live_with_tools(name: str, tools: list, session=None) -> reg_mod._LiveServer:
    live = reg_mod._LiveServer(MCPServerConfig(name=name, transport="http", url="http://x"))
    live.tools = tools
    live.session = session
    return live


# --------------------------------------------------------------------------- #
# ① _wrap_tool 透传 _meta
# --------------------------------------------------------------------------- #
def test_wrap_tool_carries_meta_ui():
    ui = {"resourceUri": "ui://app/main", "csp": "default-src 'none'", "permissions": ["microphone"]}
    live = _live_with_tools("s1", [_UiTool("show", ui=ui), _UiTool("plain")])
    wrapped = {t.name: t for t in live.to_langchain_tools()}
    with_ui = wrapped["mcp_s1_show"]
    assert with_ui.metadata["mcp_ui"] == ui
    assert with_ui.metadata["mcp_server"] == "s1"
    # 无 ui 块的工具：metadata 只带 server 名，消费方按键缺失降级
    assert "mcp_ui" not in wrapped["mcp_s1_plain"].metadata
    assert wrapped["mcp_s1_plain"].metadata["mcp_server"] == "s1"


def test_wrap_tool_ignores_malformed_meta():
    class _Weird:
        name = "w"
        description = "d"
        inputSchema = {"type": "object", "properties": {}}
        meta = "not a dict"  # 畸形 _meta：容忍而非炸包装

    live = _live_with_tools("s1", [_Weird()])
    [tool] = live.to_langchain_tools()
    assert "mcp_ui" not in tool.metadata


def test_all_langchain_tools_order_unchanged_with_meta():
    """metadata 注入不改变排序稳定性（provider cache prefix 依赖）。"""
    reg = MCPRegistry()
    reg._live = {
        "b": _live_with_tools("b", [_UiTool("z"), _UiTool("a", ui={"resourceUri": "ui://b/a"})]),
        "a": _live_with_tools("a", [_UiTool("m")]),
    }
    names = [t.name for t in reg.all_langchain_tools()]
    assert names == sorted(names)


# --------------------------------------------------------------------------- #
# ② tools_with_ui / find_tool_ui
# --------------------------------------------------------------------------- #
def test_tools_with_ui_lists_active_tools_only(isolated_home):
    reg = MCPRegistry()
    cfg = reg_mod.MCPServerConfig(name="s1", transport="http", url="http://x")
    cfg.disabled_tools = ["hidden"]
    live = reg_mod._LiveServer(cfg)
    live.tools = [
        _UiTool("show", ui={"resourceUri": "ui://s1/show"}),
        _UiTool("plain"),
        _UiTool("hidden", ui={"resourceUri": "ui://s1/hidden"}),  # 被禁：不进清单
        _UiTool("noref", ui={"csp": "x"}),  # 缺 resourceUri：没有卡
    ]
    reg._live["s1"] = live
    reg._live["s2"] = _live_with_tools("s2", [])
    out = reg.tools_with_ui()
    assert list(out) == ["mcp_s1_show"]  # 键排序稳定
    assert out["mcp_s1_show"]["server"] == "s1"
    assert out["mcp_s1_show"]["tool"] == "mcp_s1_show"
    assert out["mcp_s1_show"]["resourceUri"] == "ui://s1/show"
    assert out["mcp_s1_show"]["csp"] is None
    assert out["mcp_s1_show"]["permissions"] is None


def test_find_tool_ui_by_graph_name(isolated_home):
    ui = {"resourceUri": "ui://s1/show", "csp": "default-src 'none'"}
    reg = MCPRegistry()
    reg._live["s1"] = _live_with_tools("s1", [_UiTool("show", ui=ui), _UiTool("plain")])
    assert reg.find_tool_ui("s1", "mcp_s1_show") == ui
    assert reg.find_tool_ui("s1", "mcp_s1_plain") is None  # 工具在但无 ui
    assert reg.find_tool_ui("s1", "mcp_s1_missing") is None  # 工具不存在
    assert reg.find_tool_ui("nobody", "mcp_nobody_x") is None  # server 未连


def test_live_for_prefers_shared_registry(isolated_home, monkeypatch):
    """重连/reload 语义：shared 指向新 registry 时，旧实例的方法路由到新连接。"""
    from ginno_runtime import server_shared

    old = MCPRegistry()
    old._live["s1"] = _live_with_tools("s1", [_UiTool("stale", ui={"resourceUri": "ui://old"})])
    fresh = MCPRegistry()
    fresh._live["s1"] = _live_with_tools("s1", [_UiTool("fresh", ui={"resourceUri": "ui://new"})])
    monkeypatch.setattr(server_shared, "_mcp", fresh)
    assert old.find_tool_ui("s1", "mcp_s1_fresh") is not None
    assert old.find_tool_ui("s1", "mcp_s1_stale") is None


# --------------------------------------------------------------------------- #
# ③ read_resource：Text/Blob 拼接
# --------------------------------------------------------------------------- #
def _text_content(text: str):
    from mcp.types import TextResourceContents

    return TextResourceContents(
        uri="ui://app/main", mimeType="text/html", text=text
    )


def _blob_content(payload: bytes):
    from mcp.types import BlobResourceContents

    return BlobResourceContents(
        uri="ui://app/blob",
        mimeType="text/html",
        blob=base64.b64encode(payload).decode(),
    )


class _Result:
    def __init__(self, contents: list) -> None:
        self.contents = contents


async def test_read_resource_joins_text_contents(isolated_home):
    reg = MCPRegistry()
    session = _FakeSession(resources={"ui://app/main": _Result([_text_content("<html>"), _text_content("<!--tail-->")])})
    reg._live["s1"] = _live_with_tools("s1", [_UiTool("t")], session=session)
    html = await reg.read_resource("s1", "ui://app/main")
    assert html == "<html>\n<!--tail-->"
    assert session.read_uris == ["ui://app/main"]


async def test_read_resource_decodes_blob(isolated_home):
    reg = MCPRegistry()
    payload = "<html>blob-ui</html>".encode()
    session = _FakeSession(resources={"ui://app/blob": _Result([_blob_content(payload)])})
    reg._live["s1"] = _live_with_tools("s1", [_UiTool("t")], session=session)
    assert await reg.read_resource("s1", "ui://app/blob") == "<html>blob-ui</html>"


async def test_read_resource_errors(isolated_home):
    reg = MCPRegistry()
    # 未连接
    with pytest.raises(MCPNotConnectedError):
        await reg.read_resource("gone", "ui://x")
    # 空 contents
    session = _FakeSession(resources={"ui://empty": _Result([])})
    reg._live["s1"] = _live_with_tools("s1", [_UiTool("t")], session=session)
    with pytest.raises(ValueError, match="no contents"):
        await reg.read_resource("s1", "ui://empty")


# --------------------------------------------------------------------------- #
# ④ call_tool：content/structured 提取（与 _wrap_tool 同源）
# --------------------------------------------------------------------------- #
def _call_result():
    from mcp.types import CallToolResult, TextContent

    return CallToolResult(
        content=[TextContent(type="text", text="line1"), TextContent(type="text", text="line2")],
        structuredContent={"rows": [1, 2]},
    )


async def test_call_tool_extracts_text_and_structured(isolated_home):
    reg = MCPRegistry()
    session = _FakeSession(call_results={"show": _call_result()})
    reg._live["s1"] = _live_with_tools("s1", [_UiTool("show")], session=session)
    out = await reg.call_tool("s1", "show", {"q": "x"})
    assert out["content"] == ["line1", "line2"]  # app 视角不 join
    assert out["structured"] == {"rows": [1, 2]}
    assert session.calls == [("show", {"q": "x"})]


async def test_call_tool_not_connected(isolated_home):
    reg = MCPRegistry()
    with pytest.raises(MCPNotConnectedError):
        await reg.call_tool("gone", "show", {})


def test_content_texts_same_source_as_wrap_tool(isolated_home):
    """graph 包装器（join）与 app 路径（逐块）对同一结果产出一致内容——
    _content_texts 是唯一提取点，两条路径永不漂移。"""
    result = _call_result()
    texts = _content_texts(result.content)
    assert texts == ["line1", "line2"]
    assert "\n".join(texts) == "line1\nline2"
