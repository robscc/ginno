"""MCP Apps API layer (docs/mcp-apps-design.md §2 ③).

/api/mcp/tools-with-ui、/api/mcp/ui（404/200/503）、/api/mcp/call（权限
deny/ask→403、allow/bypass→调用）。权限断言走 settings.json 真读写（policy
与 graph 的 permission node 同一套规则语义）。
"""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths, server_shared
from ginno_runtime.api import mcp_apps
from ginno_runtime.mcp import registry as reg_mod
from ginno_runtime.mcp.registry import MCPRegistry, MCPServerConfig

pytestmark = pytest.mark.api

UI = {"resourceUri": "ui://s1/show", "csp": "default-src 'none'", "permissions": ["microphone"]}


class _UiTool:
    def __init__(self, name: str, ui: dict | None = None) -> None:
        self.name = name
        self.description = f"desc {name}"
        self.inputSchema = {"type": "object", "properties": {}}
        if ui is not None:
            self.meta = {"ui": ui}


class _Result:
    def __init__(self, contents: list) -> None:
        self.contents = contents


class _FakeSession:
    def __init__(self) -> None:
        self.resources: dict[str, object] = {"ui://s1/show": "<html>qr</html>"}
        self.calls: list[tuple[str, dict]] = []

    async def read_resource(self, uri: str):
        from mcp.types import TextResourceContents

        return _Result(
            [
                TextResourceContents(
                    uri=uri, mimeType="text/html", text=str(self.resources[str(uri)])
                )
            ]
        )

    async def call_tool(self, name: str, args: dict):
        from mcp.types import CallToolResult, TextContent

        self.calls.append((name, dict(args or {})))
        return CallToolResult(
            content=[TextContent(type="text", text="ok")],
            structuredContent={"done": True},
        )


def _fake_registry(monkeypatch, *, connected: bool = True) -> tuple[MCPRegistry, _FakeSession | None]:
    reg = MCPRegistry()
    live = reg_mod._LiveServer(MCPServerConfig(name="s1", transport="http", url="http://x"))
    live.tools = [_UiTool("show", ui=UI), _UiTool("plain")]
    session = None
    if connected:
        session = _FakeSession()
        live.session = session
    reg._live["s1"] = live
    monkeypatch.setattr(server_shared, "_mcp", reg)
    return reg, session


def _write_permissions(home, perms: dict, bypass: bool = False) -> None:
    settings = {"bypass_permissions": bypass, "permissions": perms}
    (home / "settings.json").write_text(json.dumps(settings))


# --------------------------------------------------------------------------- #
# GET /api/mcp/tools-with-ui
# --------------------------------------------------------------------------- #
def test_tools_with_ui_endpoint_empty(client):
    r = client.get("/api/mcp/tools-with-ui")
    assert r.status_code == 200
    assert r.json() == {"tools": {}}


async def test_tools_with_ui_endpoint_lists(monkeypatch, isolated_home):
    _fake_registry(monkeypatch)
    out = await mcp_apps.mcp_tools_with_ui()
    assert out["tools"]["mcp_s1_show"]["resourceUri"] == "ui://s1/show"


def test_tools_with_ui_endpoint_via_client(client, monkeypatch):
    _fake_registry(monkeypatch)
    r = client.get("/api/mcp/tools-with-ui")
    assert r.status_code == 200
    assert r.json()["tools"]["mcp_s1_show"]["server"] == "s1"


# --------------------------------------------------------------------------- #
# GET /api/mcp/ui
# --------------------------------------------------------------------------- #
async def test_ui_endpoint_200(monkeypatch, isolated_home):
    _fake_registry(monkeypatch)
    out = await mcp_apps.mcp_tool_ui(server="s1", tool="mcp_s1_show")
    assert out["html"] == "<html>qr</html>"
    assert out["csp"] == "default-src 'none'"
    assert out["permissions"] == ["microphone"]
    assert out["server"] == "s1"
    assert out["tool"] == "mcp_s1_show"


async def test_ui_endpoint_404_without_ui(monkeypatch, isolated_home):
    from fastapi import HTTPException

    _fake_registry(monkeypatch)
    with pytest.raises(HTTPException) as ei:
        await mcp_apps.mcp_tool_ui(server="s1", tool="mcp_s1_plain")
    assert ei.value.status_code == 404


async def test_ui_endpoint_503_when_disconnected(monkeypatch, isolated_home):
    from fastapi import HTTPException

    _fake_registry(monkeypatch, connected=False)
    with pytest.raises(HTTPException) as ei:
        await mcp_apps.mcp_tool_ui(server="s1", tool="mcp_s1_show")
    assert ei.value.status_code == 503


# --------------------------------------------------------------------------- #
# POST /api/mcp/call：权限语义（P0 收敛：deny 与 ask 都 403）
# --------------------------------------------------------------------------- #
def _call_body(**over) -> dict:
    body = {"server": "s1", "tool": "show", "arguments": {"text": "hi"}}
    body.update(over)
    return body


async def test_call_400_on_bad_body(monkeypatch, isolated_home):
    from fastapi import HTTPException

    _fake_registry(monkeypatch)
    for bad in ({}, {"server": "s1"}, {"server": "s1", "tool": "show", "arguments": "str"}):
        with pytest.raises(HTTPException) as ei:
            await mcp_apps.mcp_app_call(bad)
        assert ei.value.status_code == 400


async def test_call_deny_rule_returns_403(monkeypatch, isolated_home):
    from fastapi import HTTPException

    _fake_registry(monkeypatch)
    _write_permissions(isolated_home, {"deny": ["mcp_s1_show(*)"]})
    with pytest.raises(HTTPException) as ei:
        await mcp_apps.mcp_app_call(_call_body())
    assert ei.value.status_code == 403
    detail = ei.value.detail
    assert detail["decision"] == "deny"
    # 错误体含放行指引（en locale：isolated_home pin 英文）
    assert "mcp_s1_show(*)" in detail["error"]
    assert "permissions.allow" in detail["error"]


async def test_call_default_ask_returns_403(monkeypatch, isolated_home):
    """无任何规则 → 默认 ask → P0 同样拒绝（ask 弹卡回环是 P1）。"""
    from fastapi import HTTPException

    _fake_registry(monkeypatch)
    _write_permissions(isolated_home, {})
    with pytest.raises(HTTPException) as ei:
        await mcp_apps.mcp_app_call(_call_body())
    assert ei.value.status_code == 403
    assert ei.value.detail["decision"] == "ask"


async def test_call_allow_rule_executes(monkeypatch, isolated_home):
    reg, session = _fake_registry(monkeypatch)
    _write_permissions(isolated_home, {"allow": ["mcp_s1_show(*)"]})
    out = await mcp_apps.mcp_app_call(_call_body(origin="mcp-app"))
    assert out["ok"] is True
    assert out["content"] == ["ok"]
    assert out["structured"] == {"done": True}
    assert session.calls == [("show", {"text": "hi"})]


async def test_call_allow_matches_by_arg_glob(monkeypatch, isolated_home):
    """规则 "mcp_s1_show(safe*)" 只放行特定参数——与 graph 侧同构。"""
    from fastapi import HTTPException

    _fake_registry(monkeypatch)
    _write_permissions(isolated_home, {"allow": ["mcp_s1_show(*)"], "deny": ["mcp_s1_show(*danger*)"]})
    with pytest.raises(HTTPException) as ei:
        await mcp_apps.mcp_app_call(_call_body(arguments={"text": "danger zone"}))
    assert ei.value.status_code == 403
    out = await mcp_apps.mcp_app_call(_call_body(arguments={"text": "safe"}))
    assert out["ok"] is True


async def test_call_bypass_permissions_allows(monkeypatch, isolated_home):
    """特权模式（生产默认）全放行——与 graph permission node 的 bypass 跳过一致。"""
    _fake_registry(monkeypatch)
    _write_permissions(isolated_home, {"deny": ["mcp_s1_show(*)"]}, bypass=True)
    out = await mcp_apps.mcp_app_call(_call_body())
    assert out["ok"] is True


async def test_call_503_when_disconnected(monkeypatch, isolated_home):
    from fastapi import HTTPException

    _fake_registry(monkeypatch, connected=False)
    _write_permissions(isolated_home, {"allow": ["mcp_s1_show(*)"]})
    with pytest.raises(HTTPException) as ei:
        await mcp_apps.mcp_app_call(_call_body())
    assert ei.value.status_code == 503
