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
from contextlib import AsyncExitStack
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

from .. import paths

log = logging.getLogger(__name__)


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

    @classmethod
    def from_dict(cls, name: str, cfg: dict[str, Any]) -> "MCPServerConfig":
        return cls(
            name=name,
            transport=cfg.get("transport", "stdio"),
            command=cfg.get("command"),
            args=cfg.get("args", []),
            env=cfg.get("env"),
            url=cfg.get("url"),
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
            elif self.config.transport in ("streamable-http", "http"):
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
        full_name = f"mcp_{server_name}_{tool_name}"
        description = mcp_tool.description or f"MCP tool {full_name}"
        schema = mcp_tool.inputSchema or {"type": "object", "properties": {}}

        async def _arun(**kwargs: Any) -> str:
            payload = {k: v for k, v in kwargs.items() if v is not None}
            result = await self.session.call_tool(tool_name, payload)
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
        """Spawn/connect all configured servers. Idempotent.

        Each server is given a connect timeout so a hung spawn (e.g. `npx`
        stalling on first-run install or a missing binary) cannot block
        sidecar startup — the HTTP/WS server must come up regardless so the
        UI can connect and chat even when an MCP server is misbehaving.
        """
        self.ensure_loaded()
        for name, cfg in self.servers.items():
            if name in self._live:
                continue
            live = _LiveServer(cfg)
            try:
                await asyncio.wait_for(live.connect(), timeout=15)
                self._live[name] = live
            except Exception:
                log.exception("mcp[%s] failed to connect (skipped)", name)
                try:
                    await live.close()
                except Exception:
                    pass
        return self._live

    async def close_all(self) -> None:
        for live in list(self._live.values()):
            try:
                await live.close()
            except Exception:
                pass
        self._live.clear()

    def all_langchain_tools(self) -> list[StructuredTool]:
        tools: list[StructuredTool] = []
        for live in self._live.values():
            tools.extend(live.to_langchain_tools())
        return tools

    def list_tools(self) -> list[str]:
        return [t.name for live in self._live.values() for t in live.tools]
