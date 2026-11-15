"""A2A 出站 context 映射（a2a/contexts.py）单测。

覆盖：key 映射、多 peer 同 session、task 状态更新、peer_reset 自愈留壳、
remove_session 清理、原子写与畸形容错。路径 tmp_path 注入。
"""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a.contexts import ContextMap, ContextRecord, TaskBrief

pytestmark = pytest.mark.unit


@pytest.fixture()
def cm(tmp_path):
    return ContextMap(tmp_path / "contexts.json")


def _rec(sid="s1", slug="expense-bot", cid="C1", tasks=None):
    return ContextRecord(ginno_sid=sid, peer_slug=slug, context_id=cid,
                         tasks=tasks or [])


# ---- key 映射 ----------------------------------------------------------------


def test_set_and_get_key_mapping(cm):
    cm.set(_rec())
    got = cm.get_or_none("s1:expense-bot")
    assert got is not None
    assert got.context_id == "C1"
    assert got.ginno_sid == "s1"
    assert got.peer_slug == "expense-bot"
    assert got.created > 0
    assert got.last_used > 0


def test_multi_peer_same_session(cm):
    """设计 §8.2：(S, expense-bot)→C1 / (S, legal-review)→C2 并存。"""
    cm.set(_rec(slug="expense-bot", cid="C1"))
    cm.set(_rec(slug="legal-review", cid="C2"))
    assert cm.get_or_none("s1:expense-bot").context_id == "C1"
    assert cm.get_or_none("s1:legal-review").context_id == "C2"
    assert len(cm.list()) == 2


def test_get_missing_returns_none(cm):
    assert cm.get_or_none("nope:none") is None


def test_set_overwrites_same_key(cm):
    cm.set(_rec(cid="C1"))
    cm.set(_rec(cid="C2"))
    assert cm.get_or_none("s1:expense-bot").context_id == "C2"
    assert len(cm.list()) == 1


# ---- task 状态 ---------------------------------------------------------------


def test_update_task_state_existing(cm):
    cm.set(_rec(tasks=[TaskBrief(task_id="T1", state="working")]))
    assert cm.update_task_state("s1:expense-bot", "T1", "completed") is True
    rec = cm.get_or_none("s1:expense-bot")
    assert rec.tasks[0].state == "completed"
    assert rec.tasks[0].updated_at > 0


def test_update_task_state_appends_new(cm):
    cm.set(_rec())
    cm.update_task_state("s1:expense-bot", "T2", "submitted")
    rec = cm.get_or_none("s1:expense-bot")
    assert [t.task_id for t in rec.tasks] == ["T2"]
    assert rec.tasks[0].state == "submitted"


def test_update_task_state_missing_key_noop(cm):
    assert cm.update_task_state("ghost:key", "T1", "working") is False
    assert cm.list() == []


def test_update_task_state_refreshes_last_used(cm):
    cm.set(_rec())
    before = cm.get_or_none("s1:expense-bot").last_used
    cm.update_task_state("s1:expense-bot", "T1", "working")
    assert cm.get_or_none("s1:expense-bot").last_used >= before


# ---- 自愈与清理 ---------------------------------------------------------------


def test_peer_reset_keeps_shell(cm):
    """§9.6 自愈：清 context_id 与旧 tasks，留 (session, peer) 壳。"""
    cm.set(_rec(tasks=[TaskBrief(task_id="T1", state="working")]))
    assert cm.record_peer_reset("s1:expense-bot") is True
    rec = cm.get_or_none("s1:expense-bot")
    assert rec is not None  # 壳还在
    assert rec.context_id == ""
    assert rec.tasks == []  # 幽灵 task 不挡删除守卫
    assert rec.ginno_sid == "s1"
    assert rec.peer_slug == "expense-bot"


def test_peer_reset_missing_key_noop(cm):
    assert cm.record_peer_reset("ghost:key") is False


def test_remove_session_cleans_all_peers(cm):
    cm.set(_rec(sid="s1", slug="expense-bot"))
    cm.set(_rec(sid="s1", slug="legal-review"))
    cm.set(_rec(sid="s2", slug="expense-bot"))
    removed = cm.remove_session("s1")
    assert sorted(removed) == ["s1:expense-bot", "s1:legal-review"]
    assert cm.get_or_none("s1:expense-bot") is None
    assert cm.get_or_none("s1:legal-review") is None
    assert cm.get_or_none("s2:expense-bot") is not None  # 别的 session 不受影响


def test_remove_session_empty_noop(cm):
    assert cm.remove_session("ghost") == []


# ---- 存储韧性 ----------------------------------------------------------------


def test_atomic_write_valid_json_no_tmp_leftover(cm, tmp_path):
    cm.set(_rec(tasks=[TaskBrief(task_id="T1", state="working")]))
    raw = json.loads(cm.path.read_text())
    assert raw["s1:expense-bot"]["tasks"][0]["task_id"] == "T1"
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith(".contexts-")]
    assert leftovers == []


def test_corrupt_json_tolerated(cm):
    cm.path.write_text("{{{garbage")
    assert cm.list() == []
    assert cm.get_or_none("s1:expense-bot") is None
    cm.set(_rec())  # 损坏后仍可写
    assert cm.get_or_none("s1:expense-bot") is not None


def test_persistence_roundtrip(cm, tmp_path):
    cm.set(_rec(tasks=[TaskBrief(task_id="T1", state="completed")]))
    fresh = ContextMap(tmp_path / "contexts.json")
    rec = fresh.get_or_none("s1:expense-bot")
    assert rec is not None
    assert rec.context_id == "C1"
    assert rec.tasks[0].state == "completed"


def test_list_sorted_by_key(cm):
    cm.set(_rec(sid="s2", slug="a"))
    cm.set(_rec(sid="s1", slug="z"))
    assert [r.key for r in cm.list()] == ["s1:z", "s2:a"]