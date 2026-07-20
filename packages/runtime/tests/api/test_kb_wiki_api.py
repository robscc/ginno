"""API integration tests for the /kb/wiki/* endpoints."""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.api


def test_wiki_search(client, kb_vault):
    r = client.get("/kb/wiki/search?q=权限节点").json()
    assert r["ok"] is True
    titles = [x["title"] for x in r["results"]]
    assert "LangGraph 权限节点" in titles
    assert "红烧肉做法" not in titles
    # result shape
    first = r["results"][0]
    assert {"title", "path", "tags", "summary", "score", "matched_terms"} <= set(first)


def test_wiki_search_by_tag(client, kb_vault):
    r = client.get("/kb/wiki/search?tag=cooking").json()
    assert [x["title"] for x in r["results"]] == ["红烧肉做法"]


def test_wiki_list(client, kb_vault):
    r = client.get("/kb/wiki/list").json()
    assert r["ok"] is True
    titles = {p["title"] for p in r["pages"]}
    assert {"LangGraph 权限节点", "文件 Checkpointer", "红烧肉做法"} <= titles


def test_wiki_stats(client, kb_vault):
    r = client.get("/kb/wiki/stats").json()
    assert r["ok"] is True
    assert r["total_pages"] == 3
    assert "arch" in r["unique_tags"]
    assert r["total_links"] >= 1  # checkpointer links to permission


def test_wiki_index(client, kb_vault):
    r = client.post("/kb/wiki/index").json()
    assert r["ok"] is True
    assert r["indexed"] == 3
    assert "arch" in r["tags"]


def test_wiki_endpoints_guard_when_disabled(client):
    # no kb_vault -> knowledge disabled in default settings
    assert client.get("/kb/wiki/search?q=x").json()["ok"] is False
    assert client.get("/kb/wiki/list").json()["ok"] is False
    assert client.get("/kb/wiki/stats").json()["ok"] is False
    assert client.post("/kb/wiki/index").json()["ok"] is False


def test_wiki_put_config(client, isolated_home):
    vault = isolated_home / "does_not_exist_yet"
    r = client.put("/kb/wiki/config", json={"enabled": True, "vault_path": str(vault)}).json()
    assert r["ok"] is True
    assert r["config"]["enabled"] is True
    assert r["config"]["vault_path"] == str(vault)
    # usable now (enabled + path) but the empty vault indexes zero pages
    stats = client.get("/kb/wiki/stats").json()
    assert stats["ok"] is True
    assert stats["total_pages"] == 0
