"""Per-turn session-graph refresh when the live MCP toolset drifts.

2026-08-10 incident: the graph binds its toolset at session-create time; MCP
servers that connected later never reached existing sessions (the registry held
97 DingTalk tools while a research session still offered the single MCP tool it
froze with). ``_maybe_refresh_session_graph`` rebuilds the graph on drift —
these tests pin the fingerprint semantics so rebuilds happen exactly when the
live MCP name set changes, and never otherwise.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ginno_runtime import server_shared
from ginno_runtime.api import stream as stream_mod

pytestmark = pytest.mark.unit


class _StubRegistry:
    def __init__(self, names, configured_servers=("a", "钉钉文档")):
        self.names = list(names)
        self.configured_servers = list(configured_servers)

    def list_wrapped_tools(self):
        return list(self.names)

    def all_langchain_tools(self):
        return [SimpleNamespace(name=n) for n in self.names]

    def unconfigured_wrapped_names(self, wrapped_names):
        return {
            n for n in wrapped_names
            if not any(n.startswith(f"mcp_{s}_") for s in self.configured_servers)
        }


def _session(**kw):
    base = dict(
        session_id="s1",
        project_slug="default",
        workspace="",
        model=object(),
        graph=object(),
        all_tool_names=[],
        mcp_tool_names=[],
    )
    base.update(kw)
    return base


@pytest.fixture()
def record_build(monkeypatch):
    calls = []
    sentinel = object()

    def fake_build_graph(**kwargs):
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(stream_mod, "build_graph", fake_build_graph)
    return calls, sentinel


def test_no_rebuild_when_mcp_names_match(monkeypatch, record_build):
    calls, sentinel = record_build
    monkeypatch.setattr(server_shared, "_mcp", _StubRegistry(["mcp_a_search"]))
    session = _session(mcp_tool_names=["mcp_a_search"], all_tool_names=["mcp_a_search", "read_file"])
    original_graph = session["graph"]
    stream_mod._maybe_refresh_session_graph(session)
    assert calls == []
    assert session["graph"] is original_graph


def test_rebuild_when_live_mcp_tools_drift(monkeypatch, record_build):
    calls, sentinel = record_build
    # Session froze with one MCP tool; the registry now has two (a DingTalk
    # server connected after session creation).
    monkeypatch.setattr(
        server_shared, "_mcp", _StubRegistry(["mcp_a_search", "mcp_钉钉文档_get_document_content"])
    )
    session = _session(mcp_tool_names=["mcp_a_search"])
    stream_mod._maybe_refresh_session_graph(session)

    assert len(calls) == 1
    assert session["graph"] is sentinel
    assert session["mcp_tool_names"] == ["mcp_a_search", "mcp_钉钉文档_get_document_content"]
    rebuilt_all = session["all_tool_names"]
    assert "mcp_钉钉文档_get_document_content" in rebuilt_all
    assert "mcp_a_search" in rebuilt_all
    # the rebuild passes the fresh MCP tool objects into the graph
    assert [t.name for t in calls[0]["mcp_tools"]] == session["mcp_tool_names"]
    assert calls[0]["model"] is session["model"]

    # second call with unchanged live set → no further rebuild
    stream_mod._maybe_refresh_session_graph(session)
    assert len(calls) == 1


def test_no_rebuild_when_server_down_pure_loss(monkeypatch, record_build):
    """2026-10-07 contract: a configured-but-down server must NOT strip its
    tools from the session. The toolset stays frozen (the model keeps
    declaring/calling them; wrappers self-heal on reconnect), so the graph
    is not rebuilt on a pure loss."""
    calls, _ = record_build
    # DingTalk froze into the session, then its DNS window dropped it from _live.
    monkeypatch.setattr(
        server_shared, "_mcp", _StubRegistry(["mcp_a_search"])
    )
    session = _session(
        mcp_tool_names=["mcp_a_search", "mcp_钉钉文档_get_document_content"],
        all_tool_names=["mcp_a_search", "mcp_钉钉文档_get_document_content", "read_file"],
    )
    original_graph = session["graph"]
    stream_mod._maybe_refresh_session_graph(session)
    assert calls == []
    assert session["graph"] is original_graph
    # frozen tool list untouched — no mid-conversation toolset churn
    assert "mcp_钉钉文档_get_document_content" in session["mcp_tool_names"]


def test_rebuild_when_server_removed_from_config(monkeypatch, record_build):
    """A server deleted from mcp.json is deliberate shrinkage: rebuild and
    drop its tools (unlike a merely-down server, nothing will heal back)."""
    calls, sentinel = record_build
    monkeypatch.setattr(
        server_shared, "_mcp", _StubRegistry(["mcp_a_search"], configured_servers=("a",))
    )
    session = _session(
        mcp_tool_names=["mcp_a_search", "mcp_钉钉文档_get_document_content"],
    )
    stream_mod._maybe_refresh_session_graph(session)
    assert len(calls) == 1
    assert session["graph"] is sentinel
    assert session["mcp_tool_names"] == ["mcp_a_search"]


def test_no_registry_is_noop(monkeypatch, record_build):
    calls, _ = record_build
    monkeypatch.setattr(server_shared, "_mcp", None)
    session = _session()
    original_graph = session["graph"]
    stream_mod._maybe_refresh_session_graph(session)
    assert calls == []
    assert session["graph"] is original_graph
