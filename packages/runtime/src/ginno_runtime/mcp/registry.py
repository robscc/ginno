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
          "url": "https://example.com/mcp"
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass
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
        return cls(
            name=name,
            transport=transport,
            command=cfg.get("command"),
            args=cfg.get("args", []),
            env=cfg.get("env"),
            url=cfg.get("url"),
            connect_timeout=float(cfg.get("connect_timeout", 15.0)),
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
        for t in self.tools:
            out.append(self._wrap_tool(t))
        return out

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
                t = getattr(c, "text", None)
                if t:
                    texts.append(t)
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

    def load(self) -> dict[str, MCPServerConfig]:
        if not self.config_path.exists():
            self.servers = {}
            self._loaded = True
            return self.servers
        raw = json.loads(self.config_path.read_text() or "{}")
        servers = raw.get("mcpServers") or raw.get("servers") or {}
        self.servers = {name: MCPServerConfig.from_dict(name, c) for name, c in servers.items()}
        self._loaded = True
        return self.servers

    def ensure_loaded(self) -> dict[str, MCPServerConfig]:
        if not self._loaded:
            self.load()
        return self.servers

    async def connect_all(self) -> dict[str, _LiveServer]:
        """Spawn/connect all configured servers concurrently. Idempotent.

        Each server is given a connect timeout so a hung spawn (e.g. `npx`
        stalling on first-run install or a missing binary) cannot block the
        caller — the HTTP/WS server must come up regardless so the UI can
        connect and chat even when an MCP server is misbehaving. Connections
        are started concurrently so N servers cost ~max(latency), not sum.
        """
        self.ensure_loaded()
        pending = {n: c for n, c in self.servers.items() if n not in self._live}

        async def _connect_one(name: str, cfg: MCPServerConfig) -> None:
            live = _LiveServer(cfg)
            try:
                await asyncio.wait_for(live.connect(), timeout=cfg.connect_timeout)
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

    @property
    def failed_servers(self) -> list[str]:
        """Configured servers that are not live (last connect failed)."""
        return [n for n in self._failed if n not in self._live]

    def status(self) -> list[dict[str, Any]]:
        """每个配置服务器的连接状态，设置页逐行展示（GET /api/mcp）。"""
        self.ensure_loaded()
        out: list[dict[str, Any]] = []
        for name in self.servers:
            live = self._live.get(name)
            out.append({
                "name": name,
                "connected": live is not None,
                "tools": len(live.tools) if live else 0,
                "error": self._last_error.get(name),
            })
        return out

    def unconfigured_wrapped_names(self, wrapped_names: set[str]) -> set[str]:
        """wrapped_names 里已不属于任何当前配置服务器的名字（服务器被删）。

        未连上的服务器无法离线拿到它的工具清单，所以「是否仍在配置里」
        只能按服务器名前缀（mcp_{server}_{tool}）判断——这正是会话图
        区分「连接抖动导致的缺失」（保留工具）和「用户删掉服务器」
        （允许收缩重建）的依据。
        """
        self.ensure_loaded()
        configured = list(self.servers.keys())
        return {
            n for n in wrapped_names
            if not any(n.startswith(f"mcp_{s}_") for s in configured)
        }

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
            for t in live.tools
        ]

    def server_tools(self, server_name: str) -> list[str]:
        """Raw tool names of one connected server ([] when not connected) —
        used by todo-provider readiness checks (mcp link)."""
        live = self._live.get(server_name)
        return [t.name for t in live.tools] if live else []
