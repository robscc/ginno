"""API tests for gated memory→KB promotion (preview dedup + apply)."""

from __future__ import annotations

import pytest

from ginno_runtime import paths

pytestmark = pytest.mark.api


def test_promote_requires_kb(isolated_home, client):
    r = client.post("/api/kb/wiki/promote/preview", json={"text": "任意内容"}).json()
    assert r["ok"] is False
    assert r["error"] == "knowledge not configured"
    r2 = client.post("/api/kb/wiki/promote/apply", json={"path": "a.md", "raw": "x"}).json()
    assert r2["ok"] is False
    assert r2["error"] == "knowledge not configured"


def test_promote_preview_lands_in_memory_dir(kb_vault, client):
    text = "## 工作流约定\n- 分支命名使用 feat/ 前缀\n- 提交前跑全量测试"
    r = client.post("/api/kb/wiki/promote/preview", json={"text": text}).json()
    assert r["ok"] is True
    assert r["suggestion"] == "create"
    assert r["draft"]["path"].startswith("Ginno/Memory/")
    raw = r["draft"]["raw"]
    assert "type: memory" in raw or 'type: "memory"' in raw
    assert "ginno://memory" in raw
    assert "分支命名使用 feat/ 前缀" in raw


def test_promote_preview_dedup_gate(kb_vault, client):
    # Text close to an existing page triggers the merge suggestion.
    text = "权限节点按 deny ask allow 顺序匹配，ask 触发 interrupt 等待用户确认"
    r = client.post("/api/kb/wiki/promote/preview", json={"text": text}).json()
    assert r["ok"] is True
    assert r["similar"], "expected retrieval hits against the seeded vault"
    assert r["suggestion"] == "merge"
    assert r["merge_target"]["path"].endswith("permission.md")


def test_promote_preview_unrelated_creates(kb_vault, client):
    # Text unrelated to the vault → no hits, plain create.
    r = client.post(
        "/api/kb/wiki/promote/preview",
        json={"text": "- TypeScript 严格模式全开，禁用 any"},
    ).json()
    assert r["ok"] is True
    assert r["suggestion"] == "create"
    assert r["merge_target"] is None


def test_promote_apply_creates_page_and_index(kb_vault, client):
    preview = client.post(
        "/api/kb/wiki/promote/preview",
        json={"text": "- 部署前必须跑 make sidecar"},
    ).json()
    assert preview["ok"] is True
    path = preview["draft"]["path"]
    raw = preview["draft"]["raw"]

    r = client.post("/api/kb/wiki/promote/apply", json={"path": path, "raw": raw}).json()
    assert r["ok"] is True
    assert (kb_vault / path).exists()

    # The page is indexed immediately and carries the memory type.
    pages = client.get("/api/kb/wiki/list").json()["pages"]
    mine = [p for p in pages if p["path"] == path]
    assert mine and mine[0]["type"] == "memory"

    # …and searchable → injectable like any knowledge page.
    hits = client.get("/api/kb/wiki/search?q=sidecar").json()["results"]
    assert any(h["path"] == path for h in hits)


def test_promote_apply_removes_from_memory(kb_vault, client):
    mem = paths.memory_index_path()
    mem.write_text("## 约定\n- 部署前必须跑 make sidecar\n- 保留我", encoding="utf-8")

    preview = client.post(
        "/api/kb/wiki/promote/preview",
        json={"text": "## 约定\n- 部署前必须跑 make sidecar"},
    ).json()
    r = client.post(
        "/api/kb/wiki/promote/apply",
        json={
            "path": preview["draft"]["path"],
            "raw": preview["draft"]["raw"],
            "remove_from_memory": ["## 约定", "- 部署前必须跑 make sidecar"],
        },
    ).json()
    assert r["ok"] is True
    assert r["removed_from_memory"] == 2
    text = mem.read_text(encoding="utf-8")
    assert "make sidecar" not in text
    assert "保留我" in text


def test_promote_apply_rejects_overwrite_and_escape(kb_vault, client):
    preview = client.post(
        "/api/kb/wiki/promote/preview", json={"text": "- 内容"}
    ).json()
    path = preview["draft"]["path"]
    # first write ok
    assert client.post("/api/kb/wiki/promote/apply", json={"path": path, "raw": "x"}).json()["ok"]
    # overwrite refused (safety net — apply never silently clobbers)
    again = client.post("/api/kb/wiki/promote/apply", json={"path": path, "raw": "y"}).json()
    assert again["ok"] is False
    assert again["error"] == "already exists"
    # vault escape refused
    esc = client.post(
        "/api/kb/wiki/promote/apply",
        json={"path": "../evil.md", "raw": "x"},
    ).json()
    assert esc["ok"] is False
