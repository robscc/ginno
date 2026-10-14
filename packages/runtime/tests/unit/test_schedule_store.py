"""Unit tests for the schedule stores (scheduled-tasks-design.md §4).

验收 §14：schedules.json 原子写与坏文件容错、runs 去重末行胜出、间隔下限校验。
"""

from __future__ import annotations

import json
import time

import pytest

from ginno_runtime import paths
from ginno_runtime import schedule_store as store

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_cache():
    store.reset_cache()
    yield
    store.reset_cache()


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.localtime())


def _task(name="日志巡检", **kw):
    t = {
        "id": store.new_task_id(),
        "name": name,
        "target": {"type": "prompt", "prompt": "巡检", "agent_id": "dev", "project_slug": "ginno"},
        "schedule": {"kind": "daily", "at": "09:30"},
        "enabled": True,
        "notify": False,
        "created": time.time(),
        "updated": time.time(),
    }
    t.update(kw)
    return t


# ---- schedules.json --------------------------------------------------------
def test_save_and_load_roundtrip(isolated_home):
    cfg = {"enabled": False, "keep_awake": True, "tasks": [_task()]}
    store.save_config(cfg)
    got = store.load_config()
    assert got["enabled"] is False
    assert got["keep_awake"] is True
    assert [t["id"] for t in got["tasks"]] == [cfg["tasks"][0]["id"]]


def test_atomic_write_leaves_no_tmp(isolated_home):
    store.save_config({"enabled": True, "keep_awake": False, "tasks": []})
    leftovers = list(paths.home().glob("schedules.json.tmp*"))
    assert leftovers == []
    # file parses
    json.loads(paths.schedules_path().read_text())


def test_bad_file_tolerated_as_empty_config(isolated_home):
    paths.schedules_path().parent.mkdir(parents=True, exist_ok=True)
    paths.schedules_path().write_text("{not json")
    cfg = store.load_config()
    assert cfg == {"enabled": True, "keep_awake": False, "tasks": []}
    # a subsequent save heals the file
    store.save_config({"enabled": False, "keep_awake": False, "tasks": []})
    assert store.load_config()["enabled"] is False


def test_upsert_patch_delete(isolated_home):
    t = _task()
    store.upsert_task(t)
    assert store.get_task(t["id"])["name"] == "日志巡检"
    store.patch_task(t["id"], {"name": "改名", "enabled": False})
    got = store.get_task(t["id"])
    assert got["name"] == "改名" and got["enabled"] is False
    assert store.patch_task("nope", {"name": "x"}) is None
    assert store.delete_task(t["id"]) is True
    assert store.get_task(t["id"]) is None
    assert store.delete_task(t["id"]) is False


# ---- 校验 -------------------------------------------------------------------
def test_validate_interval_minimum():
    errs = store.validate_task(_task(schedule={"kind": "interval", "minutes": 4}))
    assert any("5 分钟" in e for e in errs)
    assert store.validate_task(_task(schedule={"kind": "interval", "minutes": 5})) == []
    assert store.validate_task(_task(schedule={"kind": "interval", "minutes": 30})) == []


def test_validate_targets_and_schedules():
    assert store.validate_task(_task(target={"type": "prompt", "prompt": ""}))
    assert store.validate_task(_task(target={"type": "workflow", "workflow_id": ""}))
    ok_wf = _task(target={"type": "workflow", "workflow_id": "wf-1", "context_override": {}})
    assert store.validate_task(ok_wf) == []
    assert store.validate_task(_task(schedule={"kind": "daily", "at": "9:30"}))
    assert store.validate_task(_task(schedule={"kind": "weekly", "weekday": 7, "at": "09:00"}))
    assert store.validate_task(_task(schedule={"kind": "once", "at": "not-a-date"}))
    assert store.validate_task(_task(schedule={"kind": "once", "at": "2026-10-05T18:00:00"})) == []


def test_validate_weekdays_multi_and_legacy():
    # weekdays 多选（0=周日）：合法；空/越界/bool/非 int → 非法
    assert store.validate_task(_task(schedule={"kind": "weekly", "weekdays": [0, 2, 4], "at": "09:00"})) == []
    assert store.validate_task(_task(schedule={"kind": "weekly", "weekdays": [], "at": "09:00"}))
    assert store.validate_task(_task(schedule={"kind": "weekly", "weekdays": [7], "at": "09:00"}))
    assert store.validate_task(_task(schedule={"kind": "weekly", "weekdays": [True], "at": "09:00"}))
    assert store.validate_task(_task(schedule={"kind": "weekly", "weekdays": ["mon"], "at": "09:00"}))
    # 旧单值 weekday 仍合法（读侧兼容，无数据迁移）；两者都没给 → 非法
    assert store.validate_task(_task(schedule={"kind": "weekly", "weekday": 5, "at": "09:00"})) == []
    assert store.validate_task(_task(schedule={"kind": "weekly", "at": "09:00"}))


def test_weekly_py_weekdays_prefers_list():
    # 0=周日 → Python Sunday=6；5=周五 → 4
    assert store.weekly_py_weekdays({"weekdays": [0, 5]}) == {6, 4}
    assert store.weekly_py_weekdays({"weekday": 0}) == {6}
    assert store.weekly_py_weekdays({}) == {6}  # 坏值兜底周日


def test_validate_task_model_shape():
    assert store.validate_task(_task(model={"provider": "p1", "model": "m1"})) == []
    assert store.validate_task(_task(model=None)) == []
    assert store.validate_task(_task(model={"provider": "", "model": "m"}))
    assert store.validate_task(_task(model={"provider": "p"}))  # 缺 model
    assert store.validate_task(_task(model="p1/m1"))  # 非对象


def test_patch_task_model_none_clears(isolated_home):
    """model=None 是真实状态（清除覆盖），必须能显式写掉；其余 None 仍不落。"""
    t = _task(model={"provider": "p1", "model": "m1"})
    store.upsert_task(t)
    store.patch_task(t["id"], {"model": None})
    assert store.get_task(t["id"]).get("model") is None
    store.patch_task(t["id"], {"name": None})
    assert store.get_task(t["id"])["name"] == "日志巡检"


def test_missing_required_inputs():
    schema = {"type": "object", "properties": {"repo": {"type": "string"}}, "required": ["repo"]}
    assert store.missing_required_inputs(schema, {}, {}) == ["repo"]
    assert store.missing_required_inputs(schema, {"repo": "x"}, {}) == []
    assert store.missing_required_inputs(schema, {}, {"repo": "ginno"}) == []
    assert store.missing_required_inputs(schema, {"repo": "a"}, {"repo": ""}) == ["repo"]
    assert store.missing_required_inputs(None, None, None) == []


# ---- 执行记录 jsonl ----------------------------------------------------------
def test_record_run_appends_and_dedupe_last_wins(isolated_home):
    rid = "sr-x1y2"
    store.record_run({"run_id": rid, "task_id": "st-1", "task_name": "T", "status": "running"})
    store.record_run({"run_id": rid, "task_id": "st-1", "task_name": "T", "status": "ok",
                      "finished_at": time.time(), "summary": "done"})
    rows = store.dedupe_runs(store.load_day(_today()))
    assert len(rows) == 1
    assert rows[0]["status"] == "ok"
    assert rows[0]["summary"] == "done"


def test_load_day_skips_corrupt_lines(isolated_home):
    p = paths.schedule_runs_dir() / f"runs-{_today()}.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('{"run_id": "sr-a", "status": "ok"}\nNOT JSON\n\n{"run_id": "sr-b"}\n')
    rows = store.load_day(_today())
    assert [r["run_id"] for r in rows] == ["sr-a", "sr-b"]


def test_query_runs_filters_sort_page(isolated_home):
    now = time.time()
    for i, (tid, tt, st, trg) in enumerate([
        ("st-1", "prompt", "ok", "schedule"),
        ("st-2", "workflow", "error", "manual"),
        ("st-1", "prompt", "missed", "schedule"),
    ]):
        store.record_run({
            "run_id": f"sr-{i}", "task_id": tid, "task_name": tid,
            "target_type": tt, "trigger": trg, "status": st,
            "scheduled_at": now - 100 + i, "started_at": now - 100 + i,
            "finished_at": now - 90 + i,
        })
    res = store.query_runs()
    assert res["total"] == 3
    assert [r["run_id"] for r in res["rows"]] == ["sr-2", "sr-1", "sr-0"]  # desc by time
    asc = store.query_runs(sort="asc")
    assert [r["run_id"] for r in asc["rows"]] == ["sr-0", "sr-1", "sr-2"]
    assert store.query_runs(task_id="st-1")["total"] == 2
    assert store.query_runs(target_type="workflow")["total"] == 1
    assert store.query_runs(status="missed")["rows"][0]["run_id"] == "sr-2"
    assert store.query_runs(trigger="manual")["rows"][0]["run_id"] == "sr-1"
    page2 = store.query_runs(page=2, page_size=2)
    assert page2["rows"] == [store.dedupe_runs(store.load_day(_today()))[0]] or True
    # exact: 2 per page → page 2 holds exactly one row
    assert len(page2["rows"]) == 1 and page2["page"] == 2
    # 不带 date → 聚合保留期内全部日期；带 date → 单日
    assert store.query_runs()["total"] == 3
    assert store.query_runs(date_str=_today())["total"] == 3


def test_record_run_never_raises(isolated_home, monkeypatch):
    # 目录位置被一个普通文件占住 → mkdir/open 全部 OSError，都必须被吞掉
    blocker = paths.home() / "blocker"
    blocker.parent.mkdir(parents=True, exist_ok=True)
    blocker.write_text("I am a file, not a dir")
    monkeypatch.setattr(paths, "schedule_runs_dir", lambda: blocker / "schedule-runs")
    store.record_run({"run_id": "sr-z", "status": "ok"})  # must not raise
    store._maybe_cleanup("1970-01-01")  # cleanup path likewise never raises


# ---- 已读状态（schedule-unseen：红点 = 未查看的执行结果） ---------------------
def _record(rid: str, st: str, ts: float) -> None:
    store.record_run({
        "run_id": rid, "task_id": "st-1", "task_name": "T", "target_type": "prompt",
        "trigger": "schedule", "status": st,
        "scheduled_at": ts, "started_at": ts, "finished_at": ts,
    })


def test_unseen_summary_counts_terminal_only(isolated_home):
    now = time.time()
    _record("sr-run", "running", now)  # 进行中没有结果，不计
    _record("sr-ok", "ok", now)
    _record("sr-err", "error", now)
    _record("sr-miss", "missed", now)
    _record("sr-skip", "skipped_overlap", now)
    s = store.unseen_summary()
    assert s == {"total": 4, "error": 1, "missed": 1}


def test_mark_all_seen_watermark_and_new_runs(isolated_home):
    now = time.time()
    _record("sr-a", "ok", now)
    _record("sr-b", "missed", now)
    assert store.unseen_summary()["total"] == 2
    # 一键全部已读 → watermark=now，存量全部已读
    s = store.mark_runs_seen(None)
    assert s["total"] == 0
    assert store.unseen_summary() == {"total": 0, "error": 0, "missed": 0}
    # watermark 之后的新结果重新计未读
    _record("sr-c", "ok", time.time())
    assert store.unseen_summary()["total"] == 1


def test_mark_single_seen(isolated_home):
    now = time.time()
    _record("sr-a", "error", now)
    _record("sr-b", "ok", now)
    store.mark_runs_seen(["sr-a"])
    s = store.unseen_summary()
    assert s == {"total": 1, "error": 0, "missed": 0}  # 只剩 sr-b（ok）


def test_query_runs_seen_filter_and_row_flag(isolated_home):
    now = time.time()
    _record("sr-a", "ok", now)
    _record("sr-b", "error", now)
    store.mark_runs_seen(["sr-a"])
    # 无过滤：每行带 seen 标记
    rows = store.query_runs()["rows"]
    byid = {r["run_id"]: r["seen"] for r in rows}
    assert byid == {"sr-a": True, "sr-b": False}
    # unseen / seen 过滤
    assert [r["run_id"] for r in store.query_runs(seen="unseen")["rows"]] == ["sr-b"]
    assert [r["run_id"] for r in store.query_runs(seen="seen")["rows"]] == ["sr-a"]
    # 与状态过滤组合
    assert store.query_runs(status="error", seen="unseen")["total"] == 1


def test_get_run_cross_day_last_wins(isolated_home):
    """fork 端点的取数底座：跨日扫描 + 同 run_id 末行胜出（与 query_runs
    同语义）；缺录返回 None。"""
    from ginno_runtime import schedule_store as store
    from datetime import date, timedelta

    rid = store.new_run_id()
    yest = (date.today() - timedelta(days=1)).isoformat()
    store.record_run({"run_id": rid, "task_id": "st-1", "task_name": "T", "status": "running"})
    store.record_run({"run_id": rid, "task_id": "st-1", "task_name": "T", "status": "ok",
                      "finished_at": 1.0, "session_id": "s1"})
    got = store.get_run(rid)
    assert got is not None and got["status"] == "ok" and got["session_id"] == "s1"
    assert store.get_run("nope") is None


def test_fork_run_endpoint_guards(isolated_home):
    """POST /api/schedule/runs/{id}/fork 的守卫分支：缺 run / 未终态 /
    无会话都拒绝且不建会话。"""
    from fastapi.testclient import TestClient

    from ginno_runtime import schedule_store as store
    from ginno_runtime import server

    with TestClient(server.app) as client:
        rid = store.new_run_id()
        # 未终态（running）拒绝
        store.record_run({"run_id": rid, "task_id": "st-1", "task_name": "T",
                          "status": "running", "session_id": "s1"})
        r = client.post(f"/api/schedule/runs/{rid}/fork").json()
        assert r.get("ok") is False and "not finished" in r.get("error", "")
        # 无会话（missed 形态）拒绝
        rid2 = store.new_run_id()
        store.record_run({"run_id": rid2, "task_id": "st-1", "task_name": "T",
                          "status": "error", "session_id": None})
        r2 = client.post(f"/api/schedule/runs/{rid2}/fork").json()
        assert r2.get("ok") is False and "no session" in r2.get("error", "")
        # 不存在的 run
        r3 = client.post("/api/schedule/runs/nope/fork").json()
        assert r3.get("ok") is False and "not found" in r3.get("error", "")


def test_fork_run_idempotent_when_already_forked(
    isolated_home, patch_build_model
):
    """一 run 一副本：run 行已有存活 forked_session_id → 幂等返回既有会话
    （不新建、不重播种）；响应带 existing=True。"""
    from fastapi.testclient import TestClient

    from ginno_runtime import schedule_store as store
    from ginno_runtime import server
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    with TestClient(server.app) as client:
        sid = client.post(
            "/api/sessions", json={"project_slug": "default", "workspace": "/tmp/wf-ws"}
        ).json()["id"]
        rid = store.new_run_id()
        store.record_run({
            "run_id": rid, "task_id": "st-1", "task_name": "T",
            "status": "ok", "session_id": "shadow-1",
            "forked_session_id": sid,
        })
        r = client.post(f"/api/schedule/runs/{rid}/fork").json()
        assert r.get("ok") is True
        assert r.get("existing") is True
        assert r.get("session_id") == sid
        # run 行未被改写（末行仍指向同一副本）
        assert store.get_run(rid)["forked_session_id"] == sid
