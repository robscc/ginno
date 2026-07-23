"""API tests for the KB page read/write/create endpoints + path safety."""

from __future__ import annotations

import pytest

from ginno_runtime.knowledge.association import reset_engines
from ginno_runtime.knowledge.indexer import reset_indexers

pytestmark = pytest.mark.api


@pytest.fixture
def cfg_vault(client, isolated_home):
    reset_indexers()
    reset_engines()
    vault = isolated_home / "vault"
    vault.mkdir()
    cfg = {
        "enabled": True,
        "vault_path": str(vault),
        "wiki_dir": "Wiki",
        "raw_dir": "Raw",
        "rescan_interval_s": 60,
        "inject_top_k": 5,
        "inject_min_score": 0.3,
    }
    assert client.put("/api/kb/wiki/config", json=cfg).json()["ok"] is True
    return vault


def test_page_create_read_update(client, cfg_vault):
    raw = "---\ntitle: A\ntags: [x]\n---\n# A\n\nhello [[B]] and [[C|see]]\n"
    c = client.post("/api/kb/wiki/page", json={"path": "notes/a.md", "raw": raw}).json()
    assert c["ok"] is True, c

    g = client.get("/api/kb/wiki/page?path=notes/a.md").json()
    assert g["ok"] and g["exists"] is True
    assert g["title"] == "A"
    assert g["tags"] == ["x"]
    assert g["links"] == ["B", "C"]  # alias stripped from link target
    assert "[[B]]" in g["raw"]

    # resolve by title
    gt = client.get("/api/kb/wiki/page?title=A").json()
    assert gt["exists"] is True and gt["path"] == "notes/a.md"

    # dangling link → create-able stub
    gd = client.get("/api/kb/wiki/page?title=NoSuchPage").json()
    assert gd["ok"] is True and gd["exists"] is False

    # update overwrites + refreshes the index
    u = client.put("/api/kb/wiki/page", json={"path": "notes/a.md", "raw": "---\ntitle: A2\n---\n# A2\n\nworld\n"}).json()
    assert u["ok"] is True
    g2 = client.get("/api/kb/wiki/page?path=notes/a.md").json()
    assert g2["title"] == "A2" and "world" in g2["raw"]
    titles = {p["title"] for p in client.get("/api/kb/wiki/list").json()["pages"]}
    assert "A2" in titles


def test_page_post_refuses_overwrite(client, cfg_vault):
    client.post("/api/kb/wiki/page", json={"path": "n.md", "raw": "# n\n"})
    r = client.post("/api/kb/wiki/page", json={"path": "n.md", "raw": "# n2\n"}).json()
    assert r["ok"] is False


def test_page_path_safety(client, cfg_vault):
    # must stay inside the vault and be a markdown file
    assert client.put("/api/kb/wiki/page", json={"path": "../escape.md", "raw": "x"}).json()["ok"] is False
    assert client.post("/api/kb/wiki/page", json={"path": "../../etc/x.md", "raw": "x"}).json()["ok"] is False
    assert client.put("/api/kb/wiki/page", json={"path": "notes/a.txt", "raw": "x"}).json()["ok"] is False
    assert client.put("/api/kb/wiki/page", json={"path": "", "raw": "x"}).json()["ok"] is False
    # the escape attempts must not have created files outside the vault
    assert not (cfg_vault.parent / "escape.md").exists()
