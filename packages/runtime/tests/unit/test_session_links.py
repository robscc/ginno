"""关联会话(linked sessions, topbar-unification.md §3)机制层单测。

覆盖:
* stamp_link:幂等(同 id 同 rel 刷 ts)、rel 优先级(spawned 不被 watching
  覆盖)、自链/未知会话拒绝、内存 _SESSIONS 副本同步
* linked_report:派生(spawned/delegated parent 扫描 + assigned/watching 戳)、
  mounted 手动挂、hidden 只压 auto、live 投影(working 置顶)、incoming
  (subscribers → watching_me / in_links → assigned_me)、missing 目标行
* mount/detach:linked_user 增删、linked_hidden 抑制、touch=False(不 bump
  updated——视野变更不得顶侧栏排序)
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime import server_shared
from ginno_runtime.session_links import (
    detach_linked,
    linked_report,
    mount_linked,
    stamp_link,
)
from ginno_runtime.session_meta import _session_meta_list, _session_meta_upsert
from ginno_runtime.worker_bus import deliver as wd

pytestmark = pytest.mark.unit

SLUG = "default"


@pytest.fixture(autouse=True)
def _clean_state():
    server_shared._SESSIONS.clear()
    server_shared._RUNNING_TURNS.clear()
    server_shared._MANAGED_TURNS.clear()
    server_shared._PENDING_RESUME.clear()
    yield
    server_shared._SESSIONS.clear()
    server_shared._RUNNING_TURNS.clear()
    server_shared._MANAGED_TURNS.clear()
    server_shared._PENDING_RESUME.clear()


def _meta(sid: str, **kw) -> dict:
    m = {
        "id": sid,
        "title": kw.pop("title", f"t-{sid}"),
        "type": kw.pop("type", "main"),
        "created": time.time(),
        "updated": kw.pop("updated", time.time()),
    }
    m.update(kw)
    return m


def _put(sid: str, **kw) -> dict:
    m = _meta(sid, **kw)
    _session_meta_upsert(SLUG, m)
    return m


def _get(sid: str) -> dict | None:
    return next((m for m in _session_meta_list(SLUG) if m.get("id") == sid), None)


# ---------- stamp_link ----------


def test_stamp_link_idempotent_and_precedence():
    _put("boss")
    _put("w1")
    assert stamp_link("boss", "w1", "assigned")
    assert stamp_link("boss", "w1", "assigned")  # 幂等:一条
    stamps = _get("boss")["linked_auto"]
    assert len(stamps) == 1 and stamps[0]["id"] == "w1"
    # assigned(更强)不被 watching 覆盖
    assert stamp_link("boss", "w1", "watching")
    stamps = _get("boss")["linked_auto"]
    assert len(stamps) == 1 and stamps[0]["rel"] == "assigned"


def test_stamp_link_guards():
    _put("boss")
    assert not stamp_link("boss", "boss", "assigned")  # 自链
    assert not stamp_link("nobody", "boss", "assigned")  # 未知 owner
    # 目标不存在允许落戳(戳后目标被删是正常演化,面板给 missing 行可 detach)
    assert stamp_link("boss", "ghost", "assigned")


def test_stamp_link_syncs_memory_entry():
    _put("boss")
    server_shared._SESSIONS["boss"] = {
        "session_id": "boss", "title": "boss", "project_slug": SLUG,
    }
    stamp_link("boss", "w9", "assigned")
    mem = server_shared._SESSIONS["boss"]
    assert any(e["id"] == "w9" for e in mem["linked_auto"])


# ---------- linked_report ----------


def test_report_derives_children_and_stamps():
    _put("boss")
    _put("c1", type="subagent", parent_session_id="boss", subagent={"status": "done"})
    _put("d1", type="delegation", parent_session_id="boss", stop_reason="running")
    _put("w1")
    _put("w2")
    stamp_link("boss", "w1", "assigned")
    stamp_link("boss", "w2", "watching")

    rep = linked_report("boss")
    assert rep["ok"]
    rels = {m["id"]: m["rel"] for m in rep["members"]}
    assert rels["c1"] == "spawned"
    assert rels["d1"] == "delegated"
    assert rels["w1"] == "assigned"
    assert rels["w2"] == "watching"
    # 终态行带 terminal 标记
    row_c1 = next(m for m in rep["members"] if m["id"] == "c1")
    assert row_c1["terminal"] is True and row_c1["live_status"] == "done"


def test_report_working_first_and_live_count():
    _put("boss")
    _put("idle1", updated=1000.0)
    _put("busy1", updated=500.0)
    stamp_link("boss", "idle1", "assigned")
    stamp_link("boss", "busy1", "assigned")
    server_shared._RUNNING_TURNS["busy1"] = "turn-x"  # dict:sid → turn_id

    rep = linked_report("boss")
    assert rep["members"][0]["id"] == "busy1"
    assert rep["members"][0]["live_status"] == "working"
    assert rep["live"] == 1


def test_report_hidden_and_mounted():
    _put("boss")
    _put("auto1")
    _put("user1")
    stamp_link("boss", "auto1", "assigned")
    # detach auto 成员 → hidden;手动挂 user1
    detach_linked("boss", "auto1")
    mount_linked("boss", "user1")
    rep = linked_report("boss")
    ids = {m["id"] for m in rep["members"]}
    assert "auto1" not in ids  # auto 被抑制
    assert "user1" in ids
    assert next(m for m in rep["members"] if m["id"] == "user1")["rel"] == "mounted"

    # 手动重新挂回被 hidden 的 auto 成员 → mounted 豁免抑制
    mount_linked("boss", "auto1")
    rep = linked_report("boss")
    ids = {m["id"] for m in rep["members"]}
    assert "auto1" in ids
    assert next(m for m in rep["members"] if m["id"] == "auto1")["rel"] == "mounted"


def test_report_incoming_from_subscribers_and_in_links():
    _put("me")
    _put("watcher")
    _put("assigner")
    _put("me2", subscribers=[{"session_id": "watcher", "want": "digest", "since": 1}])
    # 直接铺 in_links 戳(assign 路径在 test_report_assign_flow 里过一遍真实调用)
    _session_meta_upsert(
        SLUG,
        dict(_get("me2"), in_links=[{"id": "assigner", "rel": "assigned-by", "ts": 1}]),
    )
    rep = linked_report("me2")
    inc = {m["id"]: m["rel"] for m in rep["incoming"]}
    assert inc["watcher"] == "watching_me"
    assert inc["assigner"] == "assigned_me"
    assert rep["members"] == []


def test_report_derives_watching_from_same_project_subscribers():
    """2026-10-30 实机踩坑:历史 watch 只在目标方留 subscribers,订阅方
    无 linked_auto 戳 → members 空。同项目索引内直接派生兜底。"""
    _put("chan", type="channel")  # 订阅方(渠道会话也能看面板)
    _put("t1", subscribers=[{"session_id": "chan", "want": "digest", "since": 1}])
    rep = linked_report("chan")
    rels = {m["id"]: m["rel"] for m in rep["members"]}
    assert rels["t1"] == "watching"
    # 戳的 rel 更强时不被兜底覆盖(assign + 被订阅同目标)
    _put("t2", subscribers=[{"session_id": "chan", "want": "digest", "since": 1}])
    stamp_link("chan", "t2", "assigned")
    rep = linked_report("chan")
    rels = {m["id"]: m["rel"] for m in rep["members"]}
    assert rels["t2"] == "assigned"


def test_backfill_linked_stamps_idempotent():
    from ginno_runtime.session_links import backfill_linked_stamps

    _put("sub")
    _put("t1", subscribers=[{"session_id": "sub", "want": "digest", "since": 1}])
    _put("t2", subscribers=[{"session_id": "ghost", "want": "digest", "since": 1}])  # 订阅方已删:跳过
    assert backfill_linked_stamps() == 1
    stamps = _get("sub")["linked_auto"]
    assert [e["id"] for e in stamps] == ["t1"] and stamps[0]["rel"] == "watching"
    assert backfill_linked_stamps() == 0  # 幂等:收敛后零写入


def test_report_missing_target_row():
    _put("boss")
    stamp_link("boss", "ghost", "assigned")  # 目标已删(或从未存在)
    rep = linked_report("boss")
    assert len(rep["members"]) == 1
    m = rep["members"][0]
    assert m["id"] == "ghost" and m["live_status"] == "missing" and m["terminal"]


def test_report_unknown_session():
    assert not linked_report("nope")["ok"]


# ---------- mount / detach 持久化 ----------


def test_mount_detach_meta_fields_and_no_touch():
    _put("boss", updated=123.456)
    _put("t1", updated=999.0)
    _put("t2", updated=999.0)
    stamp_link("boss", "t2", "assigned")

    mount_linked("boss", "t1")
    assert _get("boss")["updated"] == 123.456  # touch=False:侧栏排序不动
    detach_linked("boss", "t1")  # user 成员 detach = 移出 linked_user
    assert _get("boss").get("linked_user") == []
    detach_linked("boss", "t2")  # auto 成员 detach = 落 hidden
    assert _get("boss").get("linked_hidden") == ["t2"]
    assert _get("boss")["updated"] == 123.456


def test_mount_validates_target():
    _put("boss")
    assert not mount_linked("boss", "ghost")["ok"]
    assert not mount_linked("boss", "boss")["ok"]
    assert not mount_linked("ghost", "boss")["ok"]


# ---------- assign 真实链路落戳(deliver.py 三 ok 出口之一) ----------


async def test_assign_flow_stamps_both_sides(monkeypatch):
    _put("sender")
    _put("w1", type="subagent", subagent={"status": "running", "goal": "g"})

    async def _fake_wake(*a, **k):
        return "ran"

    monkeypatch.setattr(wd, "_wake_parent_turn", _fake_wake)

    out = await wd.assign_to_worker("sender", "w1", "do it")
    assert out["ok"] and out["receipt"] == "absorbed"
    # 发送方 linked_auto + 目标方 in_links 双侧戳
    rep = linked_report("sender")
    assert [m["rel"] for m in rep["members"] if m["id"] == "w1"] == ["assigned"]
    inc = {m["id"]: m["rel"] for m in linked_report("w1")["incoming"]}
    assert inc["sender"] == "assigned_me"
