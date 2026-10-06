"""API integration tests for the wiki endpoints (search/related/discover/...).

Wiki pages are now written by the「Wiki 编译」workflow's agent steps
(agent-wiki-workflow-design.md) — the deterministic build/ingest endpoints are
gone. These tests write pages directly into the vault and exercise the read
path (index → search → related/discover/orphans/backlinks) plus the
maintenance helpers behind the workflow's python entries.
"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.knowledge.association import reset_engines
from ginno_runtime.knowledge.indexer import reset_indexers

pytestmark = pytest.mark.api


def _wiki_page(title, tags, sources, links):
    tags_s = ", ".join(tags)
    srcs = "\n".join(f'  - "{s}"' for s in sources)
    link_lines = "\n".join(f"- [[{t}]]" for t in links)
    return (
        "---\n"
        f'title: "{title}"\n'
        'date: "2026-10-07"\n'
        f"tags: [{tags_s}]\n"
        "type: summary\n"
        "confidence: medium\n"
        "sources:\n"
        f"{srcs}\n"
        "---\n\n"
        f"# {title}\n\n"
        "a sufficiently detailed distilled summary of the source document.\n\n"
        "## Key Concepts\n\n"
        f"{link_lines}\n"
    )


@pytest.fixture
def kb_setup(client, isolated_home):
    reset_indexers()
    reset_engines()
    vault = isolated_home / "vault"
    wiki = vault / "Ginno" / "Wiki"
    wiki.mkdir(parents=True, exist_ok=True)
    (vault / "Ginno" / "Raw").mkdir(parents=True, exist_ok=True)
    # Two compiled pages (as the workflow agent would write them) + their raw
    # sources. DocA/DocB don't link each other directly — they co-occur via the
    # shared 权限节点 page, which is what the association engine scores.
    (wiki / "doc-a.md").write_text(
        _wiki_page("DocA", ["arch", "perm"], ["Ginno/Raw/a.md"], ["权限节点"]),
        encoding="utf-8",
    )
    (wiki / "doc-b.md").write_text(
        _wiki_page("DocB", ["arch", "perm"], ["Ginno/Raw/b.md"], ["权限节点"]),
        encoding="utf-8",
    )
    (wiki / "concepts").mkdir()
    (wiki / "concepts" / "权限节点.md").write_text(
        _wiki_page("权限节点", ["arch"], ["Ginno/Raw/a.md", "Ginno/Raw/b.md"], ["DocA", "DocB"]),
        encoding="utf-8",
    )
    for rel, title in (("a.md", "DocA"), ("b.md", "DocB")):
        p = vault / "Ginno" / "Raw" / rel
        p.write_text(
            f"---\ntitle: {title}\ntags: [arch]\n---\n\n# {title}\n\n"
            "raw source body long enough to matter for the indexer.\n",
            encoding="utf-8",
        )

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


def _reindex(client) -> None:
    r = client.post("/api/kb/wiki/index").json()
    assert r["ok"] is True


def test_guard_when_disabled(client):
    assert client.get("/api/kb/wiki/discover").json()["ok"] is False
    assert client.get("/api/kb/wiki/orphans").json()["ok"] is False


def test_index_refreshes_search(client, kb_setup):
    _reindex(client)
    sr = client.get("/api/kb/wiki/search?q=权限").json()
    assert any("权限节点" in x["title"] for x in sr["results"])


def test_related_and_backlinks(client, kb_setup):
    _reindex(client)
    rel = client.get("/api/kb/wiki/related?title=DocA").json()
    assert rel["ok"] is True
    assert "DocB" in {x["title"] for x in rel["related"]}
    bl = client.get("/api/kb/wiki/backlinks?title=权限节点").json()
    assert {"DocA", "DocB"} <= set(bl["backlinks"])
    bla = client.get("/api/kb/wiki/backlinks?title=DocA").json()
    assert "权限节点" in set(bla["backlinks"])


def test_discover_shape(client, kb_setup):
    _reindex(client)
    d = client.get("/api/kb/wiki/discover").json()
    assert d["ok"] is True
    for key in ("strong", "clusters", "isolated", "orphan_bridges", "merge_candidates", "stats"):
        assert key in d
    assert d["stats"]["pages"] > 0
    assert d["stats"]["edges"] > 0


def test_orphans_list(client, kb_setup):
    _reindex(client)
    o = client.get("/api/kb/wiki/orphans").json()
    assert o["ok"] is True
    assert isinstance(o["pages"], list)
