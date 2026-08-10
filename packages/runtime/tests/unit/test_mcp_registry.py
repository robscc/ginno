"""MCP server config transport normalization.

Configs in the wild spell the HTTP transport several ways ("streamable-http",
"streamable_http", "Streamable_HTTP") and use either "transport" or "type" as
the key. A misspelled transport used to raise ``unknown transport`` at connect
time, silently dropping the server's tools (the 2026-08 web-search server never
registered because its config said ``"type": "streamable_http"``). These tests
pin the normalization so no spelling silently fails.
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.mcp import registry as reg_mod
from ginno_runtime.mcp.registry import MCPServerConfig, MCPRegistry

pytestmark = pytest.mark.unit


def test_streamable_http_underscore_normalized():
    # The exact shape of the failing web-search server config.
    cfg = MCPServerConfig.from_dict(
        "搜索", {"type": "streamable_http", "url": "http://x/mcp"}
    )
    assert cfg.transport == "streamable-http"


@pytest.mark.parametrize(
    "raw",
    [
        "streamable-http",
        "streamable_http",
        "Streamable_HTTP",
        "STREAMABLE-HTTP",
        " streamable-http ",
    ],
)
def test_streamable_http_spelling_variants(raw):
    cfg = MCPServerConfig.from_dict("s", {"type": raw, "url": "http://x/mcp"})
    assert cfg.transport == "streamable-http"


def test_streamablehttp_no_separator_accepted():
    cfg = MCPServerConfig.from_dict("s", {"type": "streamablehttp", "url": "http://x"})
    assert cfg.transport == "streamablehttp"


@pytest.mark.parametrize(
    "raw,expected",
    [("stdio", "stdio"), ("sse", "sse"), ("http", "http"), ("HTTP", "http")],
)
def test_other_transports_passthrough(raw, expected):
    cfg = MCPServerConfig.from_dict("s", {"type": raw, "url": "http://x", "command": "c"})
    assert cfg.transport == expected


def test_transport_key_wins_over_type():
    cfg = MCPServerConfig.from_dict(
        "s", {"transport": "sse", "type": "stdio", "url": "http://x"}
    )
    assert cfg.transport == "sse"


def test_defaults_to_stdio_when_absent():
    cfg = MCPServerConfig.from_dict("s", {"command": "npx", "args": []})
    assert cfg.transport == "stdio"


def test_normalized_transport_is_dispatchable():
    """The normalized value must match a branch in _LiveServer._run_inner,
    i.e. it is one of the recognized transports (never 'unknown')."""
    recognized = {"stdio", "sse", "streamable-http", "streamablehttp", "http"}
    for raw in ["streamable_http", "streamable-http", "Streamable_HTTP"]:
        cfg = MCPServerConfig.from_dict("s", {"type": raw, "url": "http://x"})
        assert cfg.transport in recognized


# --------------------------------------------------------------------------- #
# Lazy retry of failed connections (2026-08-10 incident: all servers failed
# with a DNS blip at boot and stayed dead for days — nothing retried them).
# --------------------------------------------------------------------------- #
class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name
        self.description = None
        self.inputSchema = None


class _FakeLiveServer:
    """Stands in for _LiveServer: fails the first ``fail_times`` connects."""

    fail_times = 0

    def __init__(self, config) -> None:
        self.config = config
        self.tools: list = []

    async def connect(self) -> None:
        if type(self).fail_times > 0:
            type(self).fail_times -= 1
            raise OSError("simulated DNS failure")
        self.tools = [_FakeTool("tool_a")]

    async def close(self) -> None:
        pass


@pytest.fixture()
def fake_live(monkeypatch):
    _FakeLiveServer.fail_times = 0
    monkeypatch.setattr(reg_mod, "_LiveServer", _FakeLiveServer)
    return _FakeLiveServer


def _registry_with(tmp_path, names=("s1",)) -> MCPRegistry:
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    n: {"type": "streamable-http", "url": "http://x/mcp"} for n in names
                }
            }
        )
    )
    return MCPRegistry(config_path=cfg)


async def test_failed_connect_is_tracked_and_recovered(tmp_path, fake_live):
    reg = _registry_with(tmp_path)
    fake_live.fail_times = 1  # first attempt fails, retry succeeds
    await reg.connect_all()
    assert reg.list_tools() == []
    assert reg.failed_servers == ["s1"]
    assert not reg.has_pending_failures(cooldown_s=3600)  # inside cooldown
    assert await reg.retry_failed(cooldown_s=3600) == []  # refused: too soon

    recovered = await reg.retry_failed(cooldown_s=0)
    assert recovered == ["s1"]
    assert reg.list_tools() == ["tool_a"]
    assert reg.failed_servers == []
    assert not reg.has_pending_failures(cooldown_s=0)


async def test_retry_is_noop_when_everything_live(tmp_path, fake_live):
    reg = _registry_with(tmp_path)
    await reg.connect_all()
    assert reg.list_tools() == ["tool_a"]
    assert await reg.retry_failed(cooldown_s=0) == []


async def test_still_failing_server_stays_failed(tmp_path, fake_live):
    reg = _registry_with(tmp_path)
    fake_live.fail_times = 10**9  # never recovers
    await reg.connect_all()
    assert reg.failed_servers == ["s1"]
    assert await reg.retry_failed(cooldown_s=0) == []
    assert reg.failed_servers == ["s1"]


def test_wrapped_names_match_langchain_wrapper():
    """list_wrapped_tools() is the per-turn graph-refresh fingerprint and must
    produce EXACTLY the names _wrap_tool gives the langchain tools — raw
    list_tools() names never equal the wrapped session list (2026-08-10)."""
    from ginno_runtime.mcp.registry import _full_tool_name

    assert _full_tool_name("钉钉文档", "get_document_content") == (
        "mcp_钉钉文档_get_document_content"
    )
    live = reg_mod._LiveServer(MCPServerConfig(name="s1", transport="http", url="http://x"))
    live.tools = [_FakeTool("tool_a"), _FakeTool("tool_b")]
    wrapped = [t.name for t in live.to_langchain_tools()]
    assert wrapped == [_full_tool_name("s1", n) for n in ("tool_a", "tool_b")]
