"""A2A peer 注册表（a2a/peers.py）单测。

覆盖：增删改查、原子写、SEC-01 token 掩码、删除守卫 PendingTaskError、
畸形 JSON 容错、持久化往返。路径全部 tmp_path 注入，不碰真实 ~/.ginno。
"""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a.contexts import ContextMap, ContextRecord, TaskBrief
from ginno_runtime.a2a.peers import (
    PeerRecord,
    PeerStore,
    PendingTaskError,
    is_masked_token,
)

pytestmark = pytest.mark.unit


@pytest.fixture()
def store(tmp_path):
    return PeerStore(tmp_path / "peers.json",
                     contexts_path=tmp_path / "contexts.json")


@pytest.fixture()
def contexts(tmp_path):
    return ContextMap(tmp_path / "contexts.json")


def _peer(slug="expense-bot", **kw):
    defaults = dict(name="报销助手", endpoint="https://bot.example/a2a",
                    token="secret-token-12345678")
    defaults.update(kw)
    return PeerRecord(slug=slug, **defaults)


# ---- CRUD ------------------------------------------------------------------


def test_add_and_find_roundtrip(store):
    store.add(_peer())
    found = store.find("expense-bot")
    assert found is not None
    assert found.name == "报销助手"
    assert found.endpoint == "https://bot.example/a2a"
    assert found.token == "secret-token-12345678"  # 磁盘是真源,明文
    assert found.trust == "readonly"
    assert found.caps == ["L0"]
    assert found.task_timeout_s == 600
    assert found.max_concurrent == 2
    assert found.confirm_mode == "every"
    assert found.allow_local_files is False
    assert found.created_at > 0


def test_id_derived_from_slug(store):
    rec = store.add(_peer())
    assert rec.id == "a2a-expense-bot"
    assert store.find("expense-bot").id == "a2a-expense-bot"


def test_add_duplicate_raises(store):
    store.add(_peer())
    with pytest.raises(ValueError):
        store.add(_peer())


def test_list_all_sorted(store):
    store.add(_peer(slug="zeta"))
    store.add(_peer(slug="alpha"))
    assert [p.slug for p in store.list_all()] == ["alpha", "zeta"]


def test_update_fields(store):
    store.add(_peer())
    updated = store.update("expense-bot", {
        "trust": "normal", "enabled": False, "task_timeout_s": 30,
        "caps": ["L0", "L1"], "token": "new-token-abcdefgh",
    })
    assert updated.trust == "normal"
    assert updated.enabled is False
    assert updated.task_timeout_s == 30
    assert updated.caps == ["L0", "L1"]
    assert updated.token == "new-token-abcdefgh"
    # 落盘一致
    assert store.find("expense-bot").trust == "normal"


def test_update_missing_slug_raises(store):
    with pytest.raises(KeyError):
        store.update("nope", {"trust": "full"})


def test_update_ignores_masked_token(store):
    """SEC-01：掩码回写 = 未修改占位，绝不把 •••• 落盘。"""
    store.add(_peer())
    store.update("expense-bot", {"token": "••••5678", "trust": "normal"})
    rec = store.find("expense-bot")
    assert rec.token == "secret-token-12345678"  # 原值保留
    assert rec.trust == "normal"  # 其余字段正常更新
    assert "••••" not in store.path.read_text()


def test_update_ignores_unknown_and_immutable_keys(store):
    store.add(_peer())
    before = store.find("expense-bot")
    store.update("expense-bot", {"bogus_key": "x", "slug": "hijack",
                                 "id": "hijack", "created_at": 0})
    after = store.find("expense-bot")
    assert after.slug == "expense-bot"
    assert after.id == before.id
    assert after.created_at == before.created_at


def test_remove_ok(store):
    store.add(_peer())
    assert store.remove("expense-bot") is True
    assert store.find("expense-bot") is None
    assert store.remove("expense-bot") is False  # 幂等


# ---- 删除守卫 ----------------------------------------------------------------


def test_remove_guard_pending_task(store, contexts):
    store.add(_peer())
    contexts.set(ContextRecord(
        ginno_sid="s1", peer_slug="expense-bot", context_id="C1",
        tasks=[TaskBrief(task_id="T1", state="working")]))
    with pytest.raises(PendingTaskError) as ei:
        store.remove("expense-bot")
    assert ei.value.slug == "expense-bot"
    assert ei.value.pending[0]["task_id"] == "T1"
    assert store.find("expense-bot") is not None  # 未删


def test_remove_guard_input_required_is_pending(store, contexts):
    """input-required/auth-required 是非终态,同样挡删除。"""
    store.add(_peer())
    contexts.set(ContextRecord(
        ginno_sid="s1", peer_slug="expense-bot", context_id="C1",
        tasks=[TaskBrief(task_id="T1", state="input-required")]))
    with pytest.raises(PendingTaskError):
        store.remove("expense-bot")


def test_remove_guard_all_terminal_ok(store, contexts):
    store.add(_peer())
    contexts.set(ContextRecord(
        ginno_sid="s1", peer_slug="expense-bot", context_id="C1",
        tasks=[TaskBrief(task_id="T1", state="completed"),
               TaskBrief(task_id="T2", state="failed"),
               TaskBrief(task_id="T3", state="canceled"),
               TaskBrief(task_id="T4", state="rejected")]))
    assert store.remove("expense-bot") is True


def test_remove_guard_other_peer_tasks_dont_block(store, contexts):
    store.add(_peer(slug="expense-bot"))
    store.add(_peer(slug="legal-review"))
    contexts.set(ContextRecord(
        ginno_sid="s1", peer_slug="legal-review", context_id="C2",
        tasks=[TaskBrief(task_id="T9", state="working")]))
    assert store.remove("expense-bot") is True  # 别的 peer 的 task 不挡


# ---- SEC-01 掩码 -------------------------------------------------------------


def test_to_public_masks_long_token():
    pub = _peer(token="secret-token-12345678").to_public()
    assert pub["token"] == "••••5678"
    assert pub["endpoint"] == "https://bot.example/a2a"  # 非敏感不掩


def test_to_public_masks_short_and_empty_token():
    assert _peer(token="abc").to_public()["token"] == "••••"
    assert _peer(token="").to_public()["token"] == ""
    assert is_masked_token("••••5678") is True
    assert is_masked_token("real-token") is False


# ---- 存储韧性 ----------------------------------------------------------------


def test_atomic_write_valid_json_no_tmp_leftover(store, tmp_path):
    store.add(_peer())
    raw = json.loads(store.path.read_text())
    assert raw["expense-bot"]["slug"] == "expense-bot"
    leftovers = [p.name for p in tmp_path.iterdir()
                 if p.name.startswith(".peers-")]
    assert leftovers == []


def test_corrupt_json_tolerated(store):
    store.path.write_text("{not valid json!!")
    assert store.list_all() == []
    assert store.find("expense-bot") is None
    # 损坏后仍可正常写入
    store.add(_peer())
    assert store.find("expense-bot") is not None


def test_non_dict_json_tolerated(store):
    store.path.write_text("[1, 2, 3]")
    assert store.list_all() == []


def test_persistence_roundtrip(store, tmp_path):
    store.add(_peer())
    store.update("expense-bot", {"trust": "full"})
    fresh = PeerStore(tmp_path / "peers.json")
    rec = fresh.find("expense-bot")
    assert rec is not None
    assert rec.trust == "full"
    assert rec.token == "secret-token-12345678"