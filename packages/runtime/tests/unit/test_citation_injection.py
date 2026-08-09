"""Citation contract + injection-side telemetry (citations-design.md §3)."""

from __future__ import annotations

import json

import pytest

from ginno_runtime.knowledge import citations as cit
from ginno_runtime.knowledge import usage
from ginno_runtime.knowledge.injection import (
    CITATIONS_CONTRACT,
    CITATIONS_CONTRACT_WIKI_ONLY,
    build_wiki_context,
    sanitize_for_memory,
)

pytestmark = pytest.mark.unit


def test_contract_appended_and_injection_counted(kb_vault):
    lst = cit.begin_turn_sources("s-cite")
    token = cit.CURRENT_TURN_SOURCES.set(lst)
    try:
        ctx = build_wiki_context("权限节点怎么工作")
    finally:
        cit.CURRENT_TURN_SOURCES.reset(token)
    # Full wording: the web block defaults to enabled=True, and the contract
    # gate now delegates to the SAME reader that registers the web tools (one
    # reader owns the gate — tools and contract can never disagree again).
    assert "引用规范" in ctx
    assert "<ginno_citations>" in ctx
    assert "wiki|<相对路径>" in ctx
    assert ctx.endswith(CITATIONS_CONTRACT)
    assert "[sN]" in ctx
    # injection telemetry
    data = json.loads(usage.usage_path().read_text())
    assert data["Ginno/Wiki/concepts/permission.md"]["injected"] == 1
    assert data["Ginno/Wiki/concepts/permission.md"]["checksum"]
    # source registry: injected pages registered for later validation
    wikis = [s for s in lst if s["kind"] == "wiki"]
    assert any(s["identity"] == "Ginno/Wiki/concepts/permission.md" for s in wikis)
    assert all(s["depth"] == "injected" for s in wikis)


def test_wiki_only_wording_when_web_disabled(kb_vault, isolated_home):
    settings = json.loads((isolated_home / "settings.json").read_text())
    settings["web"] = {"enabled": False}
    (isolated_home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    ctx = build_wiki_context("权限节点怎么工作")
    assert ctx.endswith(CITATIONS_CONTRACT_WIKI_ONLY)
    assert "[sN]" not in ctx


def test_full_wording_when_web_enabled(kb_vault, isolated_home):
    settings = json.loads((isolated_home / "settings.json").read_text())
    settings["web"] = {"enabled": True}
    (isolated_home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    ctx = build_wiki_context("权限节点怎么工作")
    assert ctx.endswith(CITATIONS_CONTRACT)
    assert "[sN]" in ctx


def test_citations_disabled_opt_out(kb_vault, isolated_home):
    settings = json.loads((isolated_home / "settings.json").read_text())
    settings.setdefault("knowledge", {})["citations"] = False
    (isolated_home / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    lst = cit.begin_turn_sources("s-off")
    token = cit.CURRENT_TURN_SOURCES.set(lst)
    try:
        ctx = build_wiki_context("权限节点怎么工作")
    finally:
        cit.CURRENT_TURN_SOURCES.reset(token)
    assert "相关知识" in ctx  # retrieval itself unaffected
    assert "引用规范" not in ctx
    assert not (usage.usage_path().exists())
    assert lst == []  # nothing registered


def test_no_contract_when_no_hits(kb_vault):
    ctx = build_wiki_context("完全无关的查询词xyzzy")
    assert "引用规范" not in ctx


def test_sanitize_strips_citation_tags():
    text = "回答 <ginno_citations>\nwiki|A.md\n</ginno_citations> 尾部"
    out = sanitize_for_memory(text)
    assert "<ginno_citations>" not in out and "</ginno_citations>" not in out
    # legacy spelling too
    assert "ginno_citations" not in sanitize_for_memory("<ginno_wiki_citations>x</ginno_wiki_citations>")
