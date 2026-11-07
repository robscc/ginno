"""MCP Apps endpoints (docs/mcp-apps-design.md §2 P0).

MCP Apps = server 在 tool ``_meta.ui.resourceUri`` 声明交互 UI（ui:// HTML
resource），host 取该 resource 以沙箱 iframe 渲染进对话流，app ⇄ host 走
postMessage JSON-RPC。Ginno 前端不直连 MCP server（stdio 根本无法直连），
所有 resource 拉取与工具回调都经这里的三个端点代理：

- ``GET  /api/mcp/tools-with-ui``：{graphName: {server, tool, resourceUri,
  csp, permissions}} 轻量清单——前端「工具块要不要渲染 app 卡」的判定源
  （启动拉一次 + 按需刷新，避免动 WS 帧结构）。
- ``GET  /api/mcp/ui?server=&tool=``：查该工具的 ``_meta.ui`` → read_resource
  → {html, csp, permissions, server, tool}。无 ui（404）时前端回退普通渲染。
- ``POST /api/mcp/call``：app 发起的工具回调。与 LLM 发起同权：走
  permission/policy 的 deny→ask→allow（P0 收敛：deny 与 ask 都拒绝 403，
  ask 弹卡回环属 P1；特权模式 bypass_permissions 全放行，与 graph 的
  permission node 同语义）。allow 才经 registry 当前活动会话 call_tool。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException

from .. import server_shared as shared
from ..lang import t
from ..mcp import registry as mcp_reg
from ..permission import policy as policy_mod

router = APIRouter()

log = logging.getLogger(__name__)


def _registry() -> mcp_reg.MCPRegistry | None:
    reg = shared._mcp
    return reg if isinstance(reg, mcp_reg.MCPRegistry) else None


@router.get("/api/mcp/tools-with-ui")
async def mcp_tools_with_ui() -> dict:
    reg = _registry()
    return {"tools": reg.tools_with_ui() if reg else {}}


@router.get("/api/mcp/ui")
async def mcp_tool_ui(server: str, tool: str) -> dict:
    """Fetch one tool's ui:// resource (HTML). 404 = no UI declared → the
    frontend falls back to the plain tool bubble."""
    reg = _registry()
    ui = reg.find_tool_ui(server, tool) if reg else None
    if not ui or not ui.get("resourceUri"):
        raise HTTPException(
            status_code=404,
            detail={"ok": False, "error": f"tool '{tool}' declares no ui resource"},
        )
    try:
        html = await reg.read_resource(server, str(ui["resourceUri"]))
    except mcp_reg.MCPNotConnectedError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "error": t(
                    f"MCP server '{server}' is not connected ({e}). Retry later or "
                    "check Settings → MCP.",
                    f"MCP 服务「{server}」未连接（{e}）。请稍后重试，或在 设置 → MCP 检查。",
                ),
            },
        ) from e
    except Exception as e:  # noqa: BLE001 — resource 读取的任何失败都是结果而非事故
        raise HTTPException(
            status_code=502,
            detail={
                "ok": False,
                "error": t(
                    f"Failed to read ui resource for '{tool}': {e}",
                    f"读取「{tool}」的 ui resource 失败：{e}",
                ),
            },
        ) from e
    return {
        "html": html,
        "csp": ui.get("csp"),
        "permissions": ui.get("permissions"),
        "server": server,
        "tool": tool,
    }


@router.post("/api/mcp/call")
async def mcp_app_call(body: dict) -> dict:
    """App-initiated tool call (postMessage ``tools/call`` → host → here).

    body: ``{server, tool, arguments, origin?}`` —— ``tool`` 是**服务器原始
    工具名**（app 就是 server 自己，它只认识自己的原名；server 名由持有
    McpAppCard 的 host 侧注入）。policy 按图内全名 ``mcp_<server>_<tool>``
    匹配，参数 repr 与 graph 的 permission node 同构（``repr(dict)``）。
    """
    server = str((body or {}).get("server") or "").strip()
    tool = str((body or {}).get("tool") or "").strip()
    arguments = (body or {}).get("arguments") or {}
    origin = (body or {}).get("origin") or "mcp-app"
    if not server or not tool:
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": "body must carry {server, tool, arguments}"},
        )
    if not isinstance(arguments, dict):
        raise HTTPException(
            status_code=400,
            detail={"ok": False, "error": "arguments must be an object"},
        )
    full_name = mcp_reg._full_tool_name(server, tool)

    # 权限：与 LLM 发起同权（deny→ask→allow 首个匹配，默认 ask）。特权模式
    # bypass 全放行——与 graph.py permission node 的语义一致（bypass 跳过
    # policy）。P0 收敛：deny 与 ask 都 403（ask 弹卡回环是 P1），错误体带
    # 放行指引。
    bypass = policy_mod.is_bypass_permissions()
    decision: str = "allow"
    if not bypass:
        decision = policy_mod.PermissionPolicy.from_settings().decide(full_name, repr(arguments))
    if decision != "allow":
        log.info(
            "mcp_app_call denied server=%s tool=%s decision=%s origin=%s",
            server,
            full_name,
            decision,
            origin,
        )
        allow_hint = f"{full_name}(*)"
        raise HTTPException(
            status_code=403,
            detail={
                "ok": False,
                "decision": decision,
                "error": t(
                    f"Tool call '{full_name}' from a sandboxed MCP App was rejected "
                    f"by the permission policy (decision: {decision}). To allow it, "
                    f"add \"{allow_hint}\" to permissions.allow in settings.json.",
                    f"沙箱 MCP App 发起的工具调用「{full_name}」被权限策略拒绝"
                    f"（decision: {decision}）。如需放行，请在 settings.json 的 "
                    f"permissions.allow 中加入 \"{allow_hint}\"。",
                ),
            },
        )

    reg = _registry()
    if reg is None:
        raise HTTPException(
            status_code=503,
            detail={"ok": False, "error": "mcp registry is not running"},
        )
    log.info("mcp_app_call server=%s tool=%s origin=%s", server, full_name, origin)
    try:
        result = await reg.call_tool(server, tool, arguments)
    except mcp_reg.MCPNotConnectedError as e:
        raise HTTPException(
            status_code=503,
            detail={
                "ok": False,
                "error": t(
                    f"MCP server '{server}' is not connected ({e}).",
                    f"MCP 服务「{server}」未连接（{e}）。",
                ),
            },
        ) from e
    except Exception as e:  # noqa: BLE001 — app 侧要看到完整错误文本
        raise HTTPException(
            status_code=502,
            detail={
                "ok": False,
                "error": t(
                    f"Tool call '{full_name}' failed: {type(e).__name__}: {e}",
                    f"工具调用「{full_name}」失败：{type(e).__name__}: {e}",
                ),
            },
        ) from e
    return {"ok": True, "content": result["content"], "structured": result["structured"]}
