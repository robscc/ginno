"""instincts evolve 单测：聚类门槛/沉淀落点/状态迁移/幂等/同名后缀。"""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime.instincts import store
from ginno_runtime.instincts.evolve import evolve_cluster, find_clusters
from ginno_runtime.instincts.types import Instinct


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    home = tmp_path / "ginno-home"
    (home / "skills").mkdir(parents=True)
    monkeypatch.setattr("ginno_runtime.paths.home", lambda: home)
    monkeypatch.setattr(store, "instincts_root", lambda: home / "instincts")
    yield home


def _mk(id_: str, domain: str = "workflow", conf: float = 0.8, status: str = "active",
        scope: str = "project", slug: str = "p1") -> Instinct:
    return store.save_instinct(Instinct(
        id=id_, trigger=f"when {id_}", behavior=f"do {id_}",
        confidence=conf, domain=domain, scope=scope, project_slug=slug,
        status=status,
    ))


def test_cluster_needs_three_active_high_conf():
    _mk("a"); _mk("b")
    assert find_clusters() == []  # 2 条不够
    _mk("c")
    cl = find_clusters()
    assert len(cl) == 1 and cl[0]["count"] == 3
    assert set(cl[0]["ids"]) == {"a", "b", "c"}


def test_low_conf_and_pending_excluded():
    _mk("a"); _mk("b"); _mk("c", conf=0.5)  # 低置信不参与
    assert find_clusters() == []
    _mk("d", status="pending")  # pending 不参与
    assert find_clusters() == []
    _mk("e")  # a,b,e 三条达标
    assert len(find_clusters()) == 1


def test_evolve_writes_skill_and_marks_sources():
    for i in ("a", "b", "c"):
        _mk(i)
    r = evolve_cluster("project", "p1", "workflow")
    assert r["ok"] is True
    p = Path(r["path"])
    assert p.exists() and p.name == "SKILL.md"
    text = p.read_text()
    assert "name: instincts-workflow-p1" in text
    assert "when a" in text and "do c" in text
    # 源规则置 evolved + evolved_to，且不再出现在 active 列表
    for i in ("a", "b", "c"):
        inst = store.get_instinct("project", "p1", i)
        assert inst.status == "evolved" and inst.evolved_to == r["name"]
    assert store.list_instincts(status="active") == []
    assert find_clusters() == []  # 幂等：evolved 不再参与


def test_evolve_small_cluster_rejected():
    _mk("a"); _mk("b")
    r = evolve_cluster("project", "p1", "workflow")
    assert r["ok"] is False and "too small" in r["error"]


def test_evolve_name_collision_suffix():
    for i in ("a", "b", "c"):
        _mk(i)
    r1 = evolve_cluster("project", "p1", "workflow")
    # 再造一批同域规则（不同 id）→ 同名 skill 应追加后缀
    for i in ("d", "e", "f"):
        _mk(i)
    r2 = evolve_cluster("project", "p1", "workflow")
    assert r2["ok"] is True
    assert r2["name"] != r1["name"] and r2["name"].startswith(r1["name"])
    assert Path(r2["path"]).exists()


def test_domains_isolated():
    for i in ("a", "b", "c"):
        _mk(i, domain="workflow")
    for i in ("x", "y"):
        _mk(i, domain="code-style")
    cl = find_clusters()
    assert len(cl) == 1 and cl[0]["domain"] == "workflow"
