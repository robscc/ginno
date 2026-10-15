"""API tests for /api/badge — Dock 徽标三源聚合（会话未读 + 定时任务未读 run
+ 团队 needs）与会话已读上报。

服务端会话未读的收件箱语义（last_finished > seen、排除集、touch=False 不
bump updated）一并在此覆盖。
"""

from __future__ import annotations

import time

import pytest

from ginno_runtime import schedule_store
from ginno_runtime.session_meta import _session_meta_list, _session_meta_upsert

pytestmark = pytest.mark.api


@pytest.fixture(autouse=True)
def _clean_state():
    schedule_store.reset_cache()
    yield
    schedule_store.reset_cache()


def _seed_session(sid: str, *, type: str = "", **kw) -> None:
    meta = {"id": sid, "title": sid, "type": type, "created": time.time(), "updated": time.time()}
    meta.update(kw)
    _session_meta_upsert("default", meta)


def _seed_run(rid: str, status: str = "ok") -> None:
    schedule_store.record_run({
        "run_id": rid, "task_id": "st-1", "task_name": "t", "target_type": "prompt",
        "trigger": "schedule", "status": status,
        "scheduled_at": time.time(), "started": time.time(), "finished_at": time.time(),
    })


def _get_badge(client) -> dict:
    r = client.get("/api/badge")
    assert r.status_code == 200
    return r.json()


# ---- 三源求和 ----------------------------------------------------------------

def test_badge_sums_three_sources(client, monkeypatch):
    _seed_session("s-unread", last_finished=time.time() + 10)
    _seed_run("sr-1")
    monkeypatch.setattr("ginno_runtime.teams.store.list_teams",
                        lambda *a, **k: [{"id": "t1"}])
    monkeypatch.setattr("ginno_runtime.teams.status.rollup",
                        lambda t: {"blocked": 1, "done_unread": 2})
    b = _get_badge(client)
    assert b["ok"] is True
    assert b["sessions"] == 1
    assert b["schedule_runs"] == 1
    assert b["teams"] == 3 and b["blocked"] == 1 and b["done_unread"] == 2
    assert b["needs"] == 5


def test_badge_empty_home_is_zero(client):
    b = _get_badge(client)
    assert b["needs"] == 0
    assert b == {"ok": True, "needs": 0, "sessions": 0, "schedule_runs": 0,
                 "teams": 0, "blocked": 0, "done_unread": 0}


# ---- 会话未读收件箱语义 --------------------------------------------------------

def test_session_unread_inbox_semantics(client):
    # 已看过的（seen ≥ last_finished）不算；没跑完过的不算
    _seed_session("s-seen", last_finished=100.0, seen=200.0)
    _seed_session("s-never")
    assert _get_badge(client)["sessions"] == 0
    # 跑完未看 → 未读
    _seed_session("s-fresh", last_finished=300.0)
    assert _get_badge(client)["sessions"] == 1
    # seen 上报后清除（touch=False：updated 不变，侧栏排序不受读取影响）
    before = {m["id"]: m["updated"] for m in _session_meta_list("default")}
    r = client.post("/api/sessions/s-fresh/seen")
    assert r.status_code == 200 and r.json()["ok"] is True
    assert _get_badge(client)["sessions"] == 0
    after = {m["id"]: m["updated"] for m in _session_meta_list("default")}
    assert after["s-fresh"] == before["s-fresh"]


def test_session_unread_excluded_types(client):
    for i, t in enumerate(
        ["scheduled", "team", "team-member", "subagent", "delegation"]
    ):
        _seed_session(f"s-x{i}", type=t, last_finished=time.time() + 10)
    assert _get_badge(client)["sessions"] == 0


def test_seen_endpoint_unknown_session(client):
    r = client.post("/api/sessions/nope/seen")
    assert r.status_code == 200
    assert r.json() == {"ok": False, "error": "not_found"}


# ---- 会话列表 unread 装饰 ------------------------------------------------------

def test_list_sessions_decorates_unread(client):
    _seed_session("s-unread", last_finished=300.0)
    _seed_session("s-read", last_finished=100.0, seen=200.0)
    rows = {s["id"]: s for s in client.get("/api/sessions").json()}
    assert rows["s-unread"]["unread"] is True
    assert rows["s-read"]["unread"] is False


# ---- 定时任务未读 + memo -------------------------------------------------------

def test_schedule_runs_seen_clears_badge(client):
    _seed_run("sr-1", "ok")
    _seed_run("sr-2", "error")
    _seed_run("sr-3", "missed")
    assert _get_badge(client)["schedule_runs"] == 3
    # mark_runs_seen 写 seen.json → mtime 变 → memo 失效 → 数字回落
    client.post("/api/schedule/runs/seen", json={"all": True})
    assert _get_badge(client)["schedule_runs"] == 0


def test_schedule_runs_memo_invalidates_on_new_run(client):
    _seed_run("sr-1")
    assert _get_badge(client)["schedule_runs"] == 1
    _seed_run("sr-2")  # 新 run 落盘改 mtime，第二轮必须看到
    assert _get_badge(client)["schedule_runs"] == 2
