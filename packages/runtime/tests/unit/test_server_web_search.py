"""Anthropic server-side web_search: model binding + citation extraction.

No network: we monkeypatch ``load_providers`` and only *construct* the chat
model (construction does not call the API), then inspect what ``bind_tools``
forwards. Whether the gateway actually searches is a property of that gateway
and is left to the user's own testing.

The citation half is pure parsing — the block shapes below are what an
Anthropic-compatible gateway returns, including the mis-typed envelope it
produces when it relays ``web_search_tool_result`` without the official
structure (items then parse as ``WebSearchToolResultError``, but still carry
url/title/content, which is all the registry needs).
"""

from __future__ import annotations

import pytest

from ginno_runtime import providers as prov_mod
from ginno_runtime.api.stream.engine import (
    _SERVER_SEARCH_BLOCK_TYPES,
    _server_search_hits,
)
from ginno_runtime.graph import _server_web_search_active
from ginno_runtime.knowledge import citations as cit
from ginno_runtime.models import build_model, server_web_search_on

pytestmark = pytest.mark.unit


def _cfg(**over) -> dict:
    base = {
        "enabled": True,
        "protocol": "anthropic",
        "name": "t",
        "api_key": "sk-x",
        "base_url": "https://example.com",
        "default_model": "some-model",
        "max_tokens": 100,
        "temperature": 0.7,
        "timeout_s": 60,
        "bearer_auth": True,
        "server_web_search": False,
        "server_web_search_max_uses": 5,
    }
    base.update(over)
    return base


# --------------------------- binding the tool ---------------------------


def test_server_web_search_on_requires_anthropic_protocol():
    assert server_web_search_on(_cfg(server_web_search=True)) is True
    # the tool is an Anthropic-protocol construct; a compat gateway ignores it
    assert server_web_search_on(_cfg(protocol="openai-compatible", server_web_search=True)) is False
    assert server_web_search_on(_cfg()) is False


def test_server_web_search_tool_injected_on_bind(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"anthropic": _cfg(server_web_search=True)})
    bound = build_model("anthropic").bind_tools([])
    sent = bound.kwargs["tools"]
    web = [t for t in sent if isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_")]
    assert len(web) == 1
    assert web[0]["name"] == "web_search"


def test_server_web_search_coexists_with_local_tools(monkeypatch):
    """The graph binds the agent allowlist itself — the gateway tool must be
    added *alongside* those, not replace them."""
    from langchain_core.tools import tool

    @tool
    def read_file() -> str:
        """Read a file."""
        return "x"

    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"anthropic": _cfg(server_web_search=True)})
    sent = build_model("anthropic").bind_tools([read_file]).kwargs["tools"]
    # langchain converts local tools to the Anthropic wire form (plain dicts,
    # no `type` key) — only the gateway tool carries a `type`.
    assert any(t.get("name") == "read_file" for t in sent if isinstance(t, dict))
    assert any(isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_") for t in sent)


def test_server_web_search_not_duplicated(monkeypatch):
    """A caller that already bound the tool must not get a second copy."""
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"anthropic": _cfg(server_web_search=True)})
    sent = (
        build_model("anthropic")
        .bind_tools([{"type": "web_search_20250305", "name": "web_search", "max_uses": 2}])
        .kwargs["tools"]
    )
    assert len([t for t in sent if isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_")]) == 1


def test_no_server_tool_when_disabled(monkeypatch):
    monkeypatch.setattr(prov_mod, "load_providers", lambda: {"anthropic": _cfg()})
    sent = build_model("anthropic").bind_tools([]).kwargs["tools"]
    assert not [t for t in sent if isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_")]


def test_max_uses_clamped(monkeypatch):
    monkeypatch.setattr(
        prov_mod, "load_providers", lambda: {"anthropic": _cfg(server_web_search=True, server_web_search_max_uses=999)}
    )
    sent = build_model("anthropic").bind_tools([]).kwargs["tools"]
    web = next(t for t in sent if isinstance(t, dict) and str(t.get("type", "")).startswith("web_search_"))
    assert web["max_uses"] == 20


def test_graph_withdraws_gino_web_search_when_provider_searches(monkeypatch):
    monkeypatch.setattr(prov_mod, "get_default_config", lambda *a, **k: "anthropic")
    monkeypatch.setattr(prov_mod, "get_config", lambda *a, **k: _cfg(server_web_search=True))
    assert _server_web_search_active() is True


def test_graph_keeps_gino_web_search_otherwise(monkeypatch):
    monkeypatch.setattr(prov_mod, "get_default_config", lambda *a, **k: "custom")
    monkeypatch.setattr(prov_mod, "get_config", lambda *a, **k: _cfg(server_web_search=False))
    assert _server_web_search_active() is False


def test_graph_falls_back_to_gino_tool_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("settings unreadable")

    monkeypatch.setattr(prov_mod, "get_default_config", boom)
    assert _server_web_search_active() is False


# ------------------------- extracting the hits -------------------------


def _block(items, btype="web_search_tool_result"):
    return {"type": btype, "tool_use_id": "srvtoolu_1", "content": items}


def _hit(url="https://a.example/1", title="A"):
    return {"type": "web_search_result", "url": url, "title": title, "content": "body", "page_age": None}


def test_extracts_url_and_title():
    hits = _server_search_hits(_block([_hit()]), _SERVER_SEARCH_BLOCK_TYPES)
    assert hits == [{"url": "https://a.example/1", "title": "A", "content": "body", "page_age": ""}]


def test_ignores_non_result_blocks():
    assert _server_search_hits({"type": "text", "text": "hi"}, _SERVER_SEARCH_BLOCK_TYPES) == []
    assert _server_search_hits({"type": "server_tool_use", "name": "web_search"}, _SERVER_SEARCH_BLOCK_TYPES) == []


def test_skips_search_failures():
    """A failed search reports the block with a different item type / error
    code and has nothing citable in it."""
    failed = {"type": "web_search_tool_error", "error_code": "unavailable"}
    assert _server_search_hits(_block([failed]), _SERVER_SEARCH_BLOCK_TYPES) == []


def test_skips_hits_without_url():
    assert _server_search_hits(_block([{"type": "web_search_result", "title": "x"}]), _SERVER_SEARCH_BLOCK_TYPES) == []


def test_unwraps_nested_content_envelope():
    nested = {"type": "web_search_tool_result", "content": {"content": [_hit()]}}
    assert len(_server_search_hits(nested, _SERVER_SEARCH_BLOCK_TYPES)) == 1


def test_non_list_payload_is_ignored():
    assert _server_search_hits({"type": "web_search_tool_result", "content": "oops"}, _SERVER_SEARCH_BLOCK_TYPES) == []


# ------------------------ registering as sources ------------------------


def test_register_server_search_hits_assigns_sequential_ids():
    lst = cit.begin_turn_sources("s1")
    out = cit.register_server_search_hits(
        "s1", [{"url": "https://a.example/1", "title": "A"}, {"url": "https://b.example/2", "title": "B"}]
    )
    assert [s["id"] for s in out] == ["s1", "s2"]
    assert [s["origin"] for s in lst] == ["provider", "provider"]
    assert lst[0]["identity"] == "https://a.example/1"


def test_register_dedupes_by_url():
    lst = cit.begin_turn_sources("s1")
    hit = {"url": "https://a.example/1", "title": "A"}
    assert len(cit.register_server_search_hits("s1", [hit])) == 1
    # the model re-searched the same page — one entry, not two
    assert cit.register_server_search_hits("s1", [hit]) == []
    assert len(lst) == 1


def test_register_is_noop_outside_a_turn():
    assert cit.register_server_search_hits("never-began", [{"url": "https://a.example/1"}]) == []


def test_register_drops_non_http():
    cit.begin_turn_sources("s1")
    assert cit.register_server_search_hits("s1", [{"url": "javascript:alert(1)"}]) == []
