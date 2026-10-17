"""Z.ai/GLM gateway server-side web_search normalization (server_tools.py).

Block shapes come from a real turn (2026-10-17, session 3e09b51e): the
gateway answers ``web_search_prime`` inside the ASSISTANT message as a
``server_tool_use`` request block plus a ``tool_result`` payload whose
``content`` is a JSON string (``[[{title, link, content, refer}, …]]``), with
the model narrating the call in plain ``text`` blocks.
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.api import server_tools
from ginno_runtime.api.messages_ui import (
    _WEB_RESULT_RE,
    _ai_content_blocks,
    _glm_tool_blocks,
)
from ginno_runtime.knowledge import citations as cit

pytestmark = pytest.mark.unit


def _server_use(tid: str, query: str) -> dict:
    return {
        "type": "server_tool_use",
        "id": tid,
        "name": "web_search_prime",
        "input": {"content_size": "medium", "location": "cn", "search_query": query},
    }


def _tool_result(tid: str, hits: list[dict]) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": tid,
        "citations": None,
        "content": json.dumps([hits], ensure_ascii=False),
    }


def _hit(title: str, url: str) -> dict:
    return {"title": title, "link": url, "content": f"{title} 的摘要", "refer": "ref_1"}


# --------------------------- narration gating ---------------------------


def test_narration_intro_block_detected():
    t = (
        "**🌐 Z.ai Built-in Tool: web_search_prime**\n\n**Input:**\n"
        '```json\n{"a":1}\n```\n*Executing on server...*\n'
    )
    assert server_tools.is_server_tool_narration(t) is True


def test_narration_output_block_detected():
    t = '**Output:**\n**web_search_prime_result_summary:** [{"text": [{"title": "x"}]}]'
    assert server_tools.is_server_tool_narration(t) is True


def test_model_prose_not_narration():
    assert server_tools.is_server_tool_narration("按用途分四类给你整理：") is False
    assert server_tools.is_server_tool_narration("") is False


# ----------------------------- extraction ------------------------------


def test_groups_pair_request_with_result():
    content = [
        {"type": "thinking", "thinking": "plan"},
        _server_use("call_1", "开源 IM 推荐"),
        {"type": "text", "text": "**Output:**\n**web_search_prime_result_summary:** [ … ]"},
        _tool_result("call_1", [_hit("野火IM", "https://wildfirechat.cn")]),
    ]
    groups = server_tools.glm_server_search_groups(content)
    assert len(groups) == 1
    g = groups[0]
    assert g["tool_id"] == "call_1"
    assert g["query"] == "开源 IM 推荐"
    assert g["hits"] == [
        {
            "url": "https://wildfirechat.cn",
            "title": "野火IM",
            "content": "野火IM 的摘要",
            "page_age": "",
        }
    ]


def test_groups_in_block_order_and_multiple():
    content = [
        _server_use("call_1", "q1"),
        _tool_result("call_1", [_hit("a", "https://a.example")]),
        _server_use("call_2", "q2"),
        _tool_result("call_2", [_hit("b", "https://b.example")]),
    ]
    groups = server_tools.glm_server_search_groups(content)
    assert [g["tool_id"] for g in groups] == ["call_1", "call_2"]


def test_groups_drop_unpaired_blocks():
    # result without a request, and a request without a result — both yield nothing
    content = [
        _tool_result("orphan", [_hit("x", "https://x.example")]),
        _server_use("call_1", "q1"),
        {"type": "tool_result", "tool_use_id": "client_tool", "content": "ok"},
    ]
    assert server_tools.glm_server_search_groups(content) == []
    assert server_tools.glm_server_search_groups("plain string") == []


def test_parse_hit_payload_tolerates_shapes():
    # native list, single object, broken JSON, url-less items → all tolerated
    assert server_tools._parse_hit_payload([{"link": "https://a.example"}])[0]["url"] == "https://a.example"
    assert server_tools._parse_hit_payload(
        {"content": [{"url": "https://b.example"}]}
    )[0]["url"] == "https://b.example"
    assert server_tools._parse_hit_payload("not json") == []
    assert server_tools._parse_hit_payload(json.dumps([{"title": "no url"}])) == []


# --------------------------- contract formatting -----------------------


def test_format_hits_block_matches_web_result_contract():
    hits = [
        (
            "s1",
            {
                "url": "https://wildfirechat.cn",
                "title": "野火IM",
                "content": "摘要",
                "page_age": "",
            },
        )
    ]
    out = server_tools.format_hits_block("web_search_prime", "开源 IM", hits)
    m = _WEB_RESULT_RE.search(out)
    assert m is not None, out
    assert m.group(1) == "s1"
    assert m.group(2) == "https://wildfirechat.cn"
    assert "[s1] 野火IM — wildfirechat.cn" in out


def test_format_hits_block_placeholder_when_unregistered():
    out = server_tools.format_hits_block(
        "web_search_prime",
        "q",
        [(None, {"url": "https://a.example", "title": "", "content": "", "page_age": ""})],
    )
    assert "[·]" in out
    assert "https://a.example" in out


# ------------------------- history replay (messages_ui) ----------------


def test_ai_content_blocks_normalize_glm_search():
    content = [
        {"type": "thinking", "thinking": "plan"},
        _server_use("call_1", "开源 IM"),
        {
            "type": "text",
            "text": "**🌐 Z.ai Built-in Tool: web_search_prime**\n*Executing on server...*",
        },
        {
            "type": "text",
            "text": "**Output:**\n**web_search_prime_result_summary:** [ …raw json… ]",
        },
        _tool_result("call_1", [_hit("野火IM", "https://wildfirechat.cn")]),
        {"type": "text", "text": "正文回答"},
    ]
    glm = _glm_tool_blocks_for(content)
    blocks = _ai_content_blocks(content, glm)
    kinds = [b["kind"] for b in blocks]
    assert kinds == ["thinking", "tool", "text"]
    tb = blocks[1]
    assert tb["name"] == "web_search"
    assert tb["pending"] is False
    assert "[s1] 野火IM — wildfirechat.cn" in tb["content"]
    assert tb["argsPreview"] == "开源 IM"


def _glm_tool_blocks_for(content):
    """_glm_tool_blocks works on a message list — wrap content in one AIMessage."""

    from langchain_core.messages import AIMessage

    return _glm_tool_blocks([AIMessage(content=content)])


def test_citation_entries_with_bare_note_segment_are_clean():
    """GLM cites `web|URL|描述` (no note=): the ref must stay a clean URL or
    every entry validates as unverified (turn a8951c7a: verified=0)."""
    text = (
        "<ginno_citations>\n"
        "web|https://wildfirechat.cn|野火IM 官网\n"
        "web|https://www.reddit.com/r/selfhosted/comments/1mfr048/x/|社区讨论\n"
        "</ginno_citations>"
    )
    entries = cit.parse_citation_block(text)
    assert [e["ref"] for e in entries] == [
        "https://wildfirechat.cn",
        "https://www.reddit.com/r/selfhosted/comments/1mfr048/x/",
    ]
    assert [e["note"] for e in entries] == ["野火IM 官网", "社区讨论"]


def test_normalize_web_ref_drops_reddit_locale_param():
    assert (
        cit.normalize_web_ref("https://www.reddit.com/r/x/comments/1/a/?tl=zh-hans")
        == "https://reddit.com/r/x/comments/1/a"
    )


def test_end_to_end_citation_validation():
    """The whole turn pipeline: gateway hits registered → model's URL+note
    citations validate as verified."""
    content = [_server_use("call_1", "q"), _tool_result("call_1", [_hit("野火IM", "https://wildfirechat.cn")])]
    groups = server_tools.glm_server_search_groups(content)
    cit.begin_turn_sources("s-verify")
    try:
        for g in groups:
            cit.register_server_search_hits("s-verify", g["hits"])
        text = "<ginno_citations>\nweb|https://wildfirechat.cn|官网\n</ginno_citations>"
        validated = cit.validate_citations(
            cit.parse_citation_block(text),
            cit.peek_turn_sources("s-verify"),
            resolve_wiki=lambda r: None,
        )
        assert validated[0]["status"] == "verified"
    finally:
        cit.end_turn_sources("s-verify")
