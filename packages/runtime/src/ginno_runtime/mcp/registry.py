"""MCP server registry.

Reads ~/.ginno/mcp/mcp.json, spawns stdio servers via the MCP Python SDK,
connects to SSE/HTTP servers, lists tools, and wraps each MCP tool as a
LangChain BaseTool so the main graph's ToolNode can call it.

mcp.json format:

    {
      "mcpServers": {
        "filesystem": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/vault"]
        },
        "obsidian": {
          "transport": "stdio",
          "command": "npx",
          "args": ["-y", "obsidian-mcp", "/path/to/vault"]
        },
        "remote": {
          "transport": "streamable-http",
          "url": "https://example.com/mcp",
          "enabled": false,
          "disabled_tools": ["dangerous_tool"]
        }
      }
    }

每个 server 还支持两个可选开关字段（设置页 UI 化用）：
- ``enabled``（bool，默认 true）：false 时不建立连接、不注册任何工具，
  但 GET /api/mcp 的 status 里仍会出现（connected=false + enabled 标记）。
- ``disabled_tools``（str 数组，默认 []）：元素是服务器 tools/list 上报的
  原始工具名；命中者不包装进 graph，但 status 的 toolDetails 里仍可见，
  前端结合 disabledTools 数组渲染禁用标记。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from .. import paths
from .. import server_shared
from ..lang import t

log = logging.getLogger(__name__)


def _full_tool_name(server_name: str, tool_name: str) -> str:
    """Graph-facing name of one MCP tool. Single source of truth — used by
    the langchain wrapper AND by cheap name-only comparisons, so the two can
    never drift (2026-08-10: comparing raw vs wrapped names would have made
    the per-turn graph-refresh fingerprint mismatch on EVERY turn)."""
    return f"mcp_{server_name}_{tool_name}"


def _compact_connect_error(e: BaseException) -> str:
    """连接失败根因压成单行（设置页状态展示用）。

    MCP 客户端经 anyio TaskGroup + httpx/httpcore 多层包装，异常组外层
    只会说 "unhandled errors in a TaskGroup"，真正的原因（如
    httpx.ConnectError: DNS 解析失败）在最内层——逐层剥到叶子再取。
    """
    while isinstance(e, BaseExceptionGroup):
        e = e.exceptions[0]
    text = f"{type(e).__name__}: {e}".strip()
    return text[:300]


def _resolve_live(server_name: str) -> "_LiveServer | None":
    """调用时解析 server_name 当前的 _LiveServer（不闭包构建时的对象）。

    会话图会把工具包装器缓存很久：重连创建新的 _LiveServer、
    /api/mcp/reload 整体重建 registry——闭包旧对象就会永久失效
    （AttributeError: NoneType.call_tool 的根因）。每次调用都经
    server_shared._mcp 解析，重连/重载后旧包装器自动路由到新连接，
    无需重建图，工具集也不会因连接抖动被剥掉。shared 为空（单测、
    启动早期）时由调用方回退到构建时的 self。
    """
    reg = server_shared._mcp
    if isinstance(reg, MCPRegistry):
        return reg._live.get(server_name)
    return None


def _unpack_rw(ctx_res: tuple) -> tuple[Any, Any]:
    """MCP clients return (read, write) or (read, write, extra) — be tolerant."""
    if len(ctx_res) == 2:
        return ctx_res[0], ctx_res[1]
    return ctx_res[0], ctx_res[1]


@dataclass
class MCPServerConfig:
    name: str
    transport: str  # stdio | sse | streamable-http
    command: str | None = None
    args: list[str] | None = None
    env: dict[str, str] | None = None
    url: str | None = None
    connect_timeout: float = 15.0
    # 服务器级开关：False 时不建立连接、不注册任何工具（status 里仍可见）。
    enabled: bool = True
    # 工具级黑名单：元素为服务器 tools/list 上报的原始工具名，命中者不包装
    # 进 graph（status 的 toolDetails 里仍完整可见，由前端打禁用标记）。
    disabled_tools: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, cfg: dict[str, Any]) -> "MCPServerConfig":
        # Accept both "transport" (our schema) and "type" (the Claude-style
        # mcpServers schema many configs in the wild use) — a streamable-http
        # server declared with "type" would otherwise fall back to stdio and
        # fail startup with "stdio requires command".
        #
        # Normalize the spelling too: configs in the wild spell the HTTP
        # transport as "streamable-http", "streamable_http", or
        # "Streamable_HTTP". Underscores/case variants used to raise
        # "unknown transport" at connect time, silently dropping the server's
        # tools (the 2026-08 web-search server never registered because its
        # config said "streamable_http"). Valid transports carry no
        # underscores, so fold them to hyphens + lowercase.
        transport = str(cfg.get("transport") or cfg.get("type") or "stdio")
        transport = transport.strip().lower().replace("_", "-")
        # disabled_tools 容忍 null / 非数组（历史手编配置常见），逐元素转
        # str 防御数字型工具名；enabled 只认显式 false 为关闭（缺省即开启）。
        raw_disabled = cfg.get("disabled_tools")
        disabled_tools = [str(x) for x in raw_disabled] if isinstance(raw_disabled, list) else []
        return cls(
            name=name,
            transport=transport,
            command=cfg.get("command"),
            args=cfg.get("args", []),
            env=cfg.get("env"),
            url=cfg.get("url"),
            connect_timeout=float(cfg.get("connect_timeout", 15.0)),
            enabled=bool(cfg.get("enabled", True)),
            disabled_tools=disabled_tools,
        )


class _LiveServer:
    """A connected MCP server: session + the tools it exposes.

    The MCP stdio client uses anyio task groups internally, which require the
    context to be entered AND exited from the same task. We spawn a dedicated
    background task to hold the connection for the lifetime of the app, and
    communicate with it via events.
    """

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self.session = None
        self.tools: list[Any] = []
        # 连接成功时刻（epoch 秒），GET /api/mcp 的 connectedAt / 前端 uptime 用
        self.connected_at: float | None = None
        self._task: asyncio.Task | None = None
        self._shutdown = asyncio.Event()
        self._ready = asyncio.Event()
        self._connect_error: BaseException | None = None

    async def connect(self) -> None:
        self._task = asyncio.create_task(self._run())
        await self._ready.wait()
        if self._connect_error:
            raise self._connect_error

    async def _run(self) -> None:
        try:
            await self._run_inner()
        except BaseException as e:
            self._connect_error = e
            self._ready.set()
            raise

    async def _run_inner(self) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        try:
            from mcp.client.sse import sse_client
        except ImportError:
            sse_client = None
        try:
            from mcp.client.streamable_http import streamablehttp_client
        except ImportError:
            streamablehttp_client = None

        async with AsyncExitStack() as stack:
            if self.config.transport == "stdio":
                if not self.config.command:
                    raise ValueError(f"mcp server {self.config.name}: stdio requires command")
                # Inherit parent env (esp. PATH) so bundled binaries can find node/npx.
                env = dict(os.environ)
                if self.config.env:
                    env.update(self.config.env)
                params = StdioServerParameters(
                    command=self.config.command,
                    args=self.config.args or [],
                    env=env,
                )
                ctx_res = await stack.enter_async_context(stdio_client(params))
                read, write = _unpack_rw(ctx_res)
            elif self.config.transport == "sse":
                if not sse_client:
                    raise RuntimeError("mcp SSE client not installed")
                ctx_res = await stack.enter_async_context(sse_client(self.config.url))
                read, write = _unpack_rw(ctx_res)
            elif self.config.transport in ("streamable-http", "streamablehttp", "http"):
                if not streamablehttp_client:
                    raise RuntimeError("mcp streamable_http client not installed")
                ctx_res = await stack.enter_async_context(
                    streamablehttp_client(self.config.url)
                )
                read, write = _unpack_rw(ctx_res)
            else:
                raise ValueError(f"unknown transport: {self.config.transport}")

            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            tools_result = await session.list_tools()
            self.session = session
            self.tools = list(tools_result.tools)
            self.connected_at = time.time()
            log.info("mcp[%s] connected, %d tools", self.config.name, len(self.tools))
            self._ready.set()

            await self._shutdown.wait()

    async def close(self) -> None:
        self._shutdown.set()
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=10)
            except (asyncio.TimeoutError, Exception):
                self._task.cancel()
        self.session = None

    def to_langchain_tools(self) -> list[StructuredTool]:
        out: list[StructuredTool] = []
        for tool in self._active_tools():
            out.append(self._wrap_tool(tool))
        return out

    def _active_tools(self) -> list[Any]:
        """会包装进 graph 的工具 = 服务器上报的全集去掉 disabled_tools。

        「graph 视角」一律走这里（to_langchain_tools / list_wrapped_tools）；
        ``self.tools`` 保留服务器原始上报（list_tools / status 的 toolDetails
        仍用它，禁用的工具对设置页保持可见）。
        """
        disabled = set(self.config.disabled_tools or ())
        return [t for t in self.tools if t.name not in disabled]

    def _wrap_tool(self, mcp_tool: Any) -> StructuredTool:
        server_name = self.config.name
        tool_name = mcp_tool.name
        full_name = _full_tool_name(server_name, tool_name)
        description = mcp_tool.description or f"MCP tool {full_name}"
        schema = mcp_tool.inputSchema or {"type": "object", "properties": {}}

        async def _arun(**kwargs: Any) -> str:
            payload = {k: v for k, v in kwargs.items() if v is not None}
            # LLMs serialize array/object MCP args inconsistently (some drop the
            # space in '["a","b"]'), and certain gateways string-split on ", " —
            # so a space-less string OR a real array both come back empty.
            # Canonicalize any JSON-looking arg to json.dumps form (', ' joined)
            # so every spelling reaches the server identically.
            for k, v in list(payload.items()):
                s = v.strip() if isinstance(v, str) else None
                if s and s[:1] in ("[", "{"):
                    try:
                        parsed = json.loads(s)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, (list, dict)):
                        payload[k] = json.dumps(parsed, ensure_ascii=False)
                elif isinstance(v, (list, dict)):
                    payload[k] = json.dumps(v, ensure_ascii=False)
            # 调用时解析当前连接：重连/重载后旧包装器自动路由到新 _LiveServer；
            # 解析不到（shared 未就绪）回退到构建时的 self。
            live = _resolve_live(server_name) or self
            session = live.session
            if session is None:
                # 连接断开/重连中：给模型一个可恢复的双语错误信息而不是裸异常
                # （原来直接 self.session.call_tool 会炸 AttributeError:
                # 'NoneType' object has no attribute 'call_tool'）。
                return t(
                    f"MCP server '{server_name}' is not connected (it was "
                    "disconnected or reconfigured). Try the call again; if it "
                    "keeps failing, ask the user to check Settings → MCP.",
                    f"MCP 服务「{server_name}」未连接（已断开或被重新配置）。"
                    "请重试；若持续失败，请让用户在 设置 → MCP 中检查。",
                )
            result = await session.call_tool(tool_name, payload)
            # MCP returns CallToolResult with .content list
            contents = getattr(result, "content", None) or []
            texts: list[str] = []
            for c in contents:
                # 注意不能叫 t：会遮蔽模块级 i18n 函数 t，让上面 session 为
                # None 的分支 return t(...) 变成 UnboundLocalError（F823）。
                text = getattr(c, "text", None)
                if text:
                    texts.append(text)
                else:
                    texts.append(json.dumps(c.model_dump() if hasattr(c, "model_dump") else str(c)))
            return "\n".join(texts) or "(empty tool result)"

        def _run(**kwargs: Any) -> str:
            return asyncio.run(_arun(**kwargs))

        # Build a pydantic schema model for StructuredTool
        model = _schema_to_model(full_name, schema)

        return StructuredTool(
            name=full_name,
            description=description,
            func=_run,
            coroutine=_arun,
            args_schema=model,
        )


def _tool_detail(mcp_tool: Any) -> dict[str, Any]:
    """单个工具的设置页详情（GET /api/mcp status[].toolDetails 元素）。

    annotations 的 readOnlyHint/destructiveHint 是 MCP 的可选 hint：旧版 SDK
    或未声明 annotations 的工具按「未知 → False」处理（不凭空宣称只读/危险），
    前端只做展示，不依赖它做权限决策。
    """
    ann = getattr(mcp_tool, "annotations", None)
    return {
        "name": mcp_tool.name,
        "description": getattr(mcp_tool, "description", None),
        "readOnly": bool(getattr(ann, "readOnlyHint", False)) if ann is not None else False,
        "destructive": bool(getattr(ann, "destructiveHint", False)) if ann is not None else False,
    }


def _schema_to_model(name: str, schema: dict) -> type[BaseModel]:
    """Convert a JSON schema dict into a pydantic model for LangChain tool args."""
    props = (schema or {}).get("properties", {}) or {}
    required = set((schema or {}).get("required", []) or [])

    fields: dict[str, Any] = {}
    for k, v in props.items():
        fields[k] = (Any, ... if k in required else Field(default=None))

    return create_model(f"{name}_Args", **fields)


class MCPRegistry:
    def __init__(self, config_path: Path | None = None) -> None:
        self.config_path = config_path or paths.mcp_config_path()
        self.servers: dict[str, MCPServerConfig] = {}
        self._live: dict[str, _LiveServer] = {}
        self._stack: AsyncExitStack | None = None
        self._loaded = False
        # Servers whose last connect attempt failed: name -> monotonic time
        # of that attempt. retry_failed() re-attempts them on a cooldown so a
        # dead DNS/network window isn't hammered on every turn/UI poll.
        self._failed: dict[str, float] = {}
        # 最近一次连接失败的根因（单行字符串），供 /api/mcp status 展示。
        self._last_error: dict[str, str] = {}
        self._retry_busy = False
        # In-flight connect 守卫：connect_all 可被并发触发（5s 轮询的惰性
        # retry_failed 后台任务 + 设置页 reconnect?server= / reload 同时到达），
        # pending 快照在注册 _live 之前算好——没有守卫时两边各自 spawn
        # _LiveServer，后完成者覆盖 self._live[name]，先者的 stdio 子进程
        # 永久泄漏。标记在 connect_all 的同步段完成，asyncio 任务不会插进
        # 「算 pending」与「标记」之间。
        self._connecting: set[str] = set()
        # load() 遇到损坏 JSON 时记录根因（不 raise——status/turn 路径不能
        # 因为一个手编坏文件 500），GET /api/mcp/config 用它向前端报警。
        self.load_error: str | None = None

    def load(self) -> dict[str, MCPServerConfig]:
        if not self.config_path.exists():
            self.servers = {}
            self._loaded = True
            return self.servers
        try:
            raw = json.loads(self.config_path.read_text() or "{}")
        except json.JSONDecodeError as e:
            # 容忍损坏文件：servers 置空 + 记录根因。后续 PUT /api/mcp 是
            # 全文覆盖，本身就是修复路径；raise 会让 GET /api/mcp（5s 轮询）
            # 和 turn 路径整体 500。
            log.exception("mcp config is not valid JSON, acting as empty")
            self.load_error = str(e)
            self.servers = {}
            self._loaded = True
            return self.servers
        self.load_error = None
        servers = raw.get("mcpServers") or raw.get("servers") or {}
        self.servers = {name: MCPServerConfig.from_dict(name, c) for name, c in servers.items()}
        self._loaded = True
        return self.servers

    def ensure_loaded(self) -> dict[str, MCPServerConfig]:
        if not self._loaded:
            self.load()
        return self.servers

    async def connect_all(self, only: set[str] | None = None) -> dict[str, _LiveServer]:
        """Spawn/connect all configured servers concurrently. Idempotent.

        Each server is given a connect timeout so a hung spawn (e.g. `npx`
        stalling on first-run install or a missing binary) cannot block the
        caller — the HTTP/WS server must come up regardless so the UI can
        connect and chat even when an MCP server is misbehaving. Connections
        are started concurrently so N servers cost ~max(latency), not sum.

        ``only`` 限定本次只尝试这些服务器（POST /api/mcp/reconnect?server=
        用它做单服务器重试）；None 表示全部。enabled=False 的服务器永远
        不连接（不建 _LiveServer、也不进 _failed —— 禁用不是失败）。

        并发安全：同名 in-flight（``_connecting``）直接跳过，防止轮询惰性
        retry 与设置页手动 reconnect 撞车时 spawn 出第二条连接（泄漏 stdio
        子进程）。连接完成时还会复核配置仍在且未被禁用——中途被删/被禁的
        server 不注册进 _live，后台关闭。
        """
        self.ensure_loaded()
        pending = {
            n: c
            for n, c in self.servers.items()
            if n not in self._live and n not in self._connecting and c.enabled and (only is None or n in only)
        }
        self._connecting.update(pending.keys())

        async def _connect_one(name: str, cfg: MCPServerConfig) -> None:
            live = _LiveServer(cfg)
            try:
                await asyncio.wait_for(live.connect(), timeout=cfg.connect_timeout)
                cur = self.servers.get(name)
                if cur is None or not cur.enabled:
                    # 连接期间被删除（sync_configs/重命名）或被禁用：不注册，
                    # 后台关闭，防止孤儿连接/子进程。
                    self._spawn_close(live)
                    return
                self._live[name] = live
                self._failed.pop(name, None)
                self._last_error.pop(name, None)
            except Exception as e:
                self._failed[name] = time.monotonic()
                self._last_error[name] = _compact_connect_error(e)
                log.exception("mcp[%s] failed to connect (skipped)", name)
                try:
                    await live.close()
                except Exception:
                    pass
            finally:
                self._connecting.discard(name)

        if pending:
            await asyncio.gather(*(_connect_one(n, c) for n, c in pending.items()))
        return self._live

    async def close_all(self) -> None:
        for live in list(self._live.values()):
            try:
                await live.close()
            except Exception:
                pass
        self._live.clear()

    def _spawn_close(self, live: "_LiveServer") -> None:
        """后台收尾一条已从 _live 逐出的连接。

        PUT /api/mcp 是设置页的高频轻操作，不能为 hung 进程的 close（最长
        10s 超时）阻塞——_live 簿记已在 sync_configs 里同步完成（状态立即
        一致），真正的连接关闭丢进事件循环 fire-and-forget。_shutdown 事件
        触发后 _run 退出 AsyncExitStack；期间在途调用经包装器的
        _resolve_live 回退语义自然收尾。无事件循环（同步单测上下文）时跳过
        ——close 由进程退出兜底。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _close() -> None:
            try:
                await live.close()
            except Exception:
                pass

        loop.create_task(_close())

    def sync_configs(self, raw: dict[str, Any]) -> list[str]:
        """PUT /api/mcp 落盘后热更新内存 configs（2026-10-09 开关无响应修复）。

        修复的断点：PUT 只写文件，self.servers 停留在 load() 时刻——
        status() 永远报旧 enabled（5s 轮询把前端乐观翻转的开关翻回去），
        connect_all 也读旧配置而无法逐出已禁用的服务器。

        语义（对应 docs/mcp-server-ui-design.md §3.3/§4.2/§4.3）：
        - **开关字段热同步**：enabled / disabled_tools 在既有 MCPServerConfig
          对象上原位改写——对象身份保留，_LiveServer.config 与
          self.servers[name] 是同一对象，已连接服务器的 _active_tools()
          立即按新黑名单过滤；unconfigured_wrapped_names 下一 turn 据此
          触发会话图收缩重建（「下一 turn 生效」语义不变）。
        - **disable 逐出**：enabled 翻 False 的已连接服务器当场弹出 _live
          （关闭放 _spawn_close 后台），status 立即 connected=false；
          _failed/_last_error 一并清理——禁用不是失败。
        - **删除**：条目从 servers/_live/_failed/_last_error 全部清掉。
        - **新增**：解析进内存（未连接，由 reconnect / retry_failed 补连，
          status 里立即可见）。
        - **连接参数刻意不热同步**（url/command/args/env/transport/
          connect_timeout）：已建立连接仍按旧参数通信，内存必须与连接保持
          一致——改参数走 Save & Reload 的全量重建（api/mcp/reload）。

        返回被逐出（disable / 删除）的服务器名列表。逐条 try/except：单条
        坏配置只跳过该条，不影响其余（文件已落盘，由调用方决定是否 reload）。
        """
        self.ensure_loaded()
        raw_servers = raw.get("mcpServers") or raw.get("servers") or {}
        if not isinstance(raw_servers, dict):
            return []
        evicted: list[str] = []

        for name in list(self.servers.keys()):
            old = self.servers[name]
            new_raw = raw_servers.get(name)
            if new_raw is None:
                # 服务器被删：连人带簿记清掉
                live = self._live.pop(name, None)
                if live is not None:
                    evicted.append(name)
                    self._spawn_close(live)
                self._failed.pop(name, None)
                self._last_error.pop(name, None)
                del self.servers[name]
                continue
            try:
                new_cfg = MCPServerConfig.from_dict(name, new_raw)
            except Exception:
                log.exception("mcp[%s] sync_configs: bad entry, keeping old config", name)
                continue
            was_enabled = old.enabled
            old.enabled = new_cfg.enabled
            old.disabled_tools = new_cfg.disabled_tools
            if was_enabled and not old.enabled:
                # disable：当场逐出（禁用不是失败，清失败簿记）
                live = self._live.pop(name, None)
                if live is not None:
                    evicted.append(name)
                    self._spawn_close(live)
                self._failed.pop(name, None)
                self._last_error.pop(name, None)

        for name, c in raw_servers.items():
            if name in self.servers:
                continue
            try:
                self.servers[name] = MCPServerConfig.from_dict(name, c)
            except Exception:
                log.exception("mcp[%s] sync_configs: bad new entry ignored", name)
        if evicted:
            log.info("mcp sync_configs evicted: %s", ", ".join(evicted))
        return evicted

    @property
    def failed_servers(self) -> list[str]:
        """Configured servers that are not live (last connect failed)."""
        return [n for n in self._failed if n not in self._live]

    def status(self, include_tools: bool = False) -> list[dict[str, Any]]:
        """每个配置服务器的连接状态，设置页逐行展示（GET /api/mcp）。

        每项基础字段：name/connected/tools/error/enabled，外加 connectedAt
        （连接成功的 epoch 秒，算 uptime 用；未连为 null）。
        ``include_tools=True``（对应 ?tools=1）才追加明细：disabledTools
        （工具黑名单原始名）与 toolDetails（服务器上报的完整工具清单详情，
        含被禁用的——前端结合 disabledTools 打禁用标记）。5s 轮询走轻负载，
        只有工具 tab/详情视图才带参取明细。多词字段一律 camelCase，与本
        端点前端类型（McpServerStatus）一致。
        """
        self.ensure_loaded()
        out: list[dict[str, Any]] = []
        for name in self.servers:
            cfg = self.servers[name]
            live = self._live.get(name)
            connected_at = getattr(live, "connected_at", None) if live else None
            item: dict[str, Any] = {
                "name": name,
                "connected": live is not None,
                "tools": len(live.tools) if live else 0,
                "error": self._last_error.get(name),
                "enabled": cfg.enabled,
                "connectedAt": int(connected_at) if connected_at else None,
            }
            if include_tools:
                item["disabledTools"] = list(cfg.disabled_tools)
                item["toolDetails"] = [
                    _tool_detail(t) for t in (live.tools if live else [])
                ]
            out.append(item)
        return out

    def unconfigured_wrapped_names(self, wrapped_names: set[str]) -> set[str]:
        """wrapped_names 里已不属于任何当前配置服务器「可用工具」的名字。

        包括三类：服务器被删、服务器被禁用（enabled=false）、工具被禁用
        （列入 disabled_tools）。后两类是用户在设置页的显式动作而非连接
        抖动——与「服务器被删」同侧，允许会话图收缩重建，禁用才能对已
        打开的会话在下一 turn 生效。

        未连上的服务器无法离线拿到它的工具清单，所以「属于哪个服务器」
        只能按名字前缀（mcp_{server}_{tool}）判断；前缀拼接有损，多个
        服务器名互为前缀时取最长匹配以还原工具名。
        """
        self.ensure_loaded()
        configured = sorted(self.servers.keys(), key=len, reverse=True)

        def _owner(n: str) -> str | None:
            for s in configured:
                if n.startswith(f"mcp_{s}_"):
                    return s
            return None

        out: set[str] = set()
        for n in wrapped_names:
            owner = _owner(n)
            if owner is None:
                out.add(n)  # 服务器已删
                continue
            cfg = self.servers[owner]
            if not cfg.enabled or n[len(f"mcp_{owner}_"):] in cfg.disabled_tools:
                out.add(n)  # 服务器或工具被禁用
        return out

    def has_pending_failures(self, cooldown_s: float = 120.0) -> bool:
        """True when a retry is due: failures exist, none is being retried
        right now, and the newest failure is older than the cooldown."""
        if not self._failed or self._retry_busy:
            return False
        if all(n in self._live for n in self._failed):
            return False
        return (time.monotonic() - max(self._failed.values())) >= cooldown_s

    async def retry_failed(self, cooldown_s: float = 120.0) -> list[str]:
        """Re-attempt servers whose last connect failed (lazy healing).

        Called opportunistically from the turn path and GET /api/mcp so a
        startup-time DNS/network blip doesn't leave MCP dead until a manual
        reload (2026-08-06: all four servers failed with Errno 8 at boot and
        stayed dead for days). connect_all() only touches servers not in
        _live; the cooldown (keyed on the newest failure) bounds the rate.
        Never raises — a failed retry just stays failed until the next window.
        """
        if self._retry_busy:
            return []
        pending = [n for n in self._failed if n not in self._live]
        if not pending:
            return []
        if (time.monotonic() - max(self._failed.values())) < cooldown_s:
            return []
        self._retry_busy = True
        try:
            await self.connect_all()
        except Exception:
            log.exception("mcp retry_failed: connect_all raised")
        finally:
            self._retry_busy = False
        recovered = [n for n in pending if n in self._live]
        if recovered:
            log.info("mcp retry recovered: %s", ", ".join(recovered))
        return recovered

    def all_langchain_tools(self) -> list[StructuredTool]:
        tools: list[StructuredTool] = []
        for live in self._live.values():
            tools.extend(live.to_langchain_tools())
        # Deterministic order regardless of server connect order: the tools
        # array rides at the FRONT of the provider cache prefix, so a reorder
        # on reconnect invalidates the whole prefix cache (2026-08 cache-rate
        # diagnosis). Wrapped names carry the server prefix, so a plain
        # name-sort is stable across reconnects.
        tools.sort(key=lambda t: t.name)
        return tools

    def list_tools(self) -> list[str]:
        return [t.name for live in self._live.values() for t in live.tools]

    def list_wrapped_tools(self) -> list[str]:
        """Graph-facing (wrapped) names of all tools of all live servers —
        without constructing langchain wrappers. This is what sessions store
        in ``mcp_tool_names``, so it is the correct fingerprint for the
        per-turn graph-refresh check (``list_tools`` returns RAW server-side
        names, which never match the wrapped session list)."""
        return [
            _full_tool_name(name, t.name)
            for name, live in self._live.items()
            for t in live._active_tools()
        ]

    def server_tools(self, server_name: str) -> list[str]:
        """Raw tool names of one connected server ([] when not connected) —
        used by todo-provider readiness checks (mcp link)."""
        live = self._live.get(server_name)
        return [t.name for t in live.tools] if live else []
