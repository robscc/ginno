"""API integration tests for the P1 wiki endpoints (build/ingest/related/discover/...)."""

from __future__ import annotations

import json

import pytest

from ginno_runtime.knowledge.association import reset_engines
from ginno_runtime.knowledge.indexer import reset_indexers

pytestmark = pytest.mark.api


@pytest.fixture
def kb_setup(client, isolated_home):
    reset_indexers()
    reset_engines()
    vault = isolated_home / "vault"

    def w(rel, title, tags, *concepts):
        p = vault / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        body = "this is a sufficiently long summary paragraph for the document.\n\n" + "\n\n".join(
            f"we rely on **{c}** to do the work here." for c in concepts
        )
        p.write_text(
            f"---\ntitle: {title}\ntags: [{', '.join(tags)}]\n---\n\n# {title}\n\n{body}\n",
            encoding="utf-8",
        )

    w("Ginno/Raw/a.md", "DocA", ["arch", "perm"], "权限节点", "AlphaX")
    w("Ginno/Raw/b.md", "DocB", ["arch", "perm"], "权限节点", "BetaY")

    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["knowledge"] = {
        "enabled": True,
        "vault_path": str(vault),
        "wiki_dir": "Ginno/Wiki",
        "raw_dir": "Ginno/Raw",
        "rescan_interval_s": 60,
        "inject_top_k": 5,
        "inject_min_score": 0.3,
    }
    sp.write_text(json.dumps(s))
    return vault


def test_build_guard_when_disabled(client):
    assert client.post("/kb/wiki/build").json()["ok"] is False
    assert client.get("/kb/wiki/discover").json()["ok"] is False
    assert client.get("/kb/wiki/orphans").json()["ok"] is False


def test_build_creates_pages_and_refreshes_search(client, kb_setup):
    vault = kb_setup
    r = client.post("/kb/wiki/build").json()
    assert r["ok"] is True
    assert r["scanned"] == 2
    assert r["created"]
    assert (vault / "Ginno" / "Wiki" / "concepts" / "权限节点.md").exists()
    assert (vault / "Ginno" / "Wiki" / "INDEX.md").exists()
    # the shared indexer was rescanned → search now sees the compiled concept
    sr = client.get("/kb/wiki/search?q=权限").json()
    assert any("权限节点" in x["title"] for x in sr["results"])


def test_related_and_backlinks(client, kb_setup):
    client.post("/kb/wiki/build")
    # 权限节点 occurs in both raw docs, so every concept/summary neighbour shares a
    # source with it and is (correctly) skipped; AlphaX has genuine neighbours.
    rel = client.get("/kb/wiki/related?title=AlphaX").json()
    assert rel["ok"] is True
    assert "BetaY" in {x["title"] for x in rel["related"]}
    # backlinks come from the per-doc summary pages that wikilink the concepts
    bl = client.get("/kb/wiki/backlinks?title=权限节点").json()
    assert {"DocA", "DocB"} <= set(bl["backlinks"])
    bla = client.get("/kb/wiki/backlinks?title=AlphaX").json()
    assert set(bla["backlinks"]) == {"DocA"}


def test_discover_shape(client, kb_setup):
    client.post("/kb/wiki/build")
    d = client.get("/kb/wiki/discover").json()
    assert d["ok"] is True
    for key in ("strong", "clusters", "isolated", "orphan_bridges", "merge_candidates", "stats"):
        assert key in d
    assert d["stats"]["pages"] > 0
    assert d["stats"]["edges"] > 0


def test_orphans_list(client, kb_setup):
    client.post("/kb/wiki/build")
    o = client.get("/kb/wiki/orphans").json()
    assert o["ok"] is True
    assert isinstance(o["pages"], list)


def test_ingest_single_new_file(client, kb_setup):
    vault = kb_setup
    client.post("/kb/wiki/build")
    new = vault / "Ginno" / "Raw" / "c.md"
    new.write_text(
        "---\ntitle: DocC\ntags: [arch, perm]\n---\n# DocC\n\n"
        "another sufficiently long summary paragraph here.\n\nwe use **GammaZ** now.\n",
        encoding="utf-8",
    )
    r = client.post("/kb/wiki/ingest", json={"path": "Ginno/Raw/c.md"}).json()
    assert r["ok"] is True and r["created"]
    assert (vault / "Ginno" / "Wiki" / "concepts" / "gammaz.md").exists()
    lst = client.get("/kb/wiki/list").json()
    assert any(p["title"] == "GammaZ" for p in lst["pages"])


def test_ingest_rejects_path_outside_vault(client, kb_setup):
    r = client.post("/kb/wiki/ingest", json={"path": "/etc/passwd"}).json()
    assert r["ok"] is False
