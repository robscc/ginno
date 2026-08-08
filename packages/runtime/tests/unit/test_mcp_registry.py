"""MCP server config transport normalization.

Configs in the wild spell the HTTP transport several ways ("streamable-http",
"streamable_http", "Streamable_HTTP") and use either "transport" or "type" as
the key. A misspelled transport used to raise ``unknown transport`` at connect
time, silently dropping the server's tools (the 2026-08 web-search server never
registered because its config said ``"type": "streamable_http"``). These tests
pin the normalization so no spelling silently fails.
"""

from __future__ import annotations

import pytest

from ginno_runtime.mcp.registry import MCPServerConfig

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
