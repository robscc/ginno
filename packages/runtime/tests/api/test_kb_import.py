"""API tests for importing an existing LLM Wiki: probe + index scoping."""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime.knowledge.association import reset_engines
from ginno_runtime.knowledge.indexer import reset_indexers

pytestmark = pytest.mark.api


def _w(p: Path, title: str, tags=None, body="some body text that is long enough."):
    p.parent.mkdir(parents=True, exist_ok=True)
    tg = f"[{', '.join(tags)}]" if tags else "[]"
    p.write_text(f"---\ntitle: {title}\ntags: {tg}\n---\n# {title}\n\n{body}\n", encoding="utf-8")


def _molly_vault(vault: Path):
    _w(vault / "Molly" / "Wiki" / "concepts" / "perm.md", "权限节点", ["arch", "permission"])
    _w(vault / "Molly" / "Wiki" / "concepts" / "interrupt.md", "interrupt", ["arch", "permission"])
    (vault / "Molly" / "Wiki" / "INDEX.md").write_text(
        "---\ntitle: Wiki Index\n---\n# Wiki Index\n\n- [[权限节点]]\n", encoding="utf-8"
    )
    _w(vault / "Molly" / "Raw" / "r.md", "RAWDOC", ["arch"])
    _w(vault / "Molly" / "Research" / "x.md", "RESEARCHDOC", ["arch"])
    _w(vault / "Molly" / "Memory" / "m.md", "MEMORYDOC", ["arch"])
    _w(vault / "loose.md", "LOOSEDOC", ["misc"])


@pytest.fixture
def molly_vault(client, isolated_home):
    reset_indexers()
    reset_engines()
    vault = isolated_home / "vault"
    _molly_vault(vault)
    return vault


def test_probe_detects_molly_layout(client, molly_vault):
    r = client.get(f"/kb/wiki/probe?path={molly_vault}").json()
    assert r["ok"] is True
    d = r["detected"]
    assert d["namespace"] == "Molly"
    assert d["wiki_dir"] == "Molly/Wiki"
    assert d["raw_dir"] == "Molly/Raw"
    assert d["research_dir"] == "Molly/Research"
    assert d["memory_dir"] == "Molly/Memory"
    assert r["wiki_pages"] == 3  # 2 concepts + INDEX
    assert r["raw_pages"] == 1
    assert r["has_index"] is True
    assert r["total_md"] == 7  # 3 wiki (incl INDEX) + raw + research + memory + loose


def test_probe_invalid_paths(client):
    assert client.get("/kb/wiki/probe?path=").json()["ok"] is False
    assert client.get("/kb/wiki/probe?path=/no/such/dir/here").json()["ok"] is False


def test_import_indexes_vault_minus_raw(client, molly_vault):
    cfg = {
        "enabled": True,
        "vault_path": str(molly_vault),
        "wiki_dir": "Molly/Wiki",
        "raw_dir": "Molly/Raw",
        "rescan_interval_s": 60,
        "inject_top_k": 5,
        "inject_min_score": 0.3,
    }
    assert client.put("/kb/wiki/config", json=cfg).json()["ok"] is True
    assert client.post("/kb/wiki/index").json()["ok"] is True

    titles = {p["title"] for p in client.get("/kb/wiki/list").json()["pages"]}
    # compiled wiki is indexed ...
    assert {"权限节点", "interrupt"} <= titles
    # ... and so are finished notes anywhere else in the vault (research /
    # memory / loose) — the whole vault is the knowledge corpus now ...
    assert {"RESEARCHDOC", "MEMORYDOC", "LOOSEDOC"} <= titles
    # ... only the raw compile-sources dir is excluded (it surfaces via wiki).
    assert "RAWDOC" not in titles

    sr = client.get("/kb/wiki/search?q=权限").json()
    assert any("权限节点" in x["title"] for x in sr["results"])
