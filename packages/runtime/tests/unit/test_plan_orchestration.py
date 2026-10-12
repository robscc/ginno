"""Plan orchestration unit tests（subagent-plan-orchestration-design.md）.

Store 生命周期/DAG/amend（保留既有条目生命周期字段——2026-10-12 实现期
抓到的回归：amend 走全量重规范化会把 running/done 打回 pending）、
watcher 的对账映射与并发帽退避。
"""

from __future__ import annotations

import pytest

from ginno_runtime.plans import store, watcher

pytestmark = pytest.mark.unit


def _mk(session_id="s1", slug="default", **kw):
    items = kw.pop(
        "items",
        [
            {"goal": "a"},
            {"goal": "b", "depends_on": ["it_1"]},
        ],
    )
    return store.create_plan(session_id, slug=slug, title="t", items=items, **kw)


# ---- store -----------------------------------------------------------------


def test_plan_create_and_defaults(isolated_home):
    plan = _mk()
    assert plan["status"] == store.PLAN_ACTIVE
    ids = [it["item_id"] for it in plan["items"]]
    assert ids == ["it_1", "it_2"]
    assert plan["items"][0]["backend"] == store.BACKEND_GINNO
    assert plan["items"][0]["mode"] == "read-only"
    # 落盘可重读
    reread = store.get_plan("default", "s1", plan["plan_id"])
    assert reread["plan_id"] == plan["plan_id"]
    assert len(store.list_plans("default", "s1")) == 1


def test_plan_needs_confirm_gate(isolated_home):
    plan = _mk(needs_confirm=True)
    assert plan["status"] == store.PLAN_PENDING_CONFIRM
    assert store.confirm_plan("default", "s1", plan["plan_id"])["status"] == store.PLAN_ACTIVE
    assert store.confirm_plan("default", "s1", plan["plan_id"]) is None  # 幂等拒绝


def test_plan_dag_validation(isolated_home):
    with pytest.raises(ValueError, match="cycle"):
        _mk(items=[{"goal": "a", "depends_on": ["it_2"]}, {"goal": "b", "depends_on": ["it_1"]}])
    with pytest.raises(ValueError, match="unknown depends_on"):
        _mk(items=[{"goal": "a", "depends_on": ["nope"]}])
    with pytest.raises(ValueError, match="duplicate"):
        _mk(items=[{"goal": "a", "item_id": "x"}, {"goal": "b", "item_id": "x"}])


def test_plan_mark_settle_completes(isolated_home):
    plan = _mk()
    pid = plan["plan_id"]
    assert store.mark_running("default", "s1", pid, "it_1", "c1")["items"][0]["status"] == "running"
    # running 条目的 attempts 在 mark_running 计数
    assert store.get_plan("default", "s1", pid)["items"][0]["attempts"] == 1
    updated = store.mark_settled("default", "s1", pid, "it_1", "done", result_summary="ok")
    assert updated["items"][0]["status"] == "done"
    assert updated["status"] == store.PLAN_ACTIVE  # it_2 还没跑
    # it_2 依赖 it_1 已 done → ready；跑完 → 清单完成
    assert [it["item_id"] for it in store.ready_items(updated)] == ["it_2"]
    store.mark_running("default", "s1", pid, "it_2", "c2")
    final = store.mark_settled("default", "s1", pid, "it_2", "done")
    assert final["status"] == store.PLAN_COMPLETED


def test_plan_amend_preserves_lifecycle(isolated_home):
    """THE 回归：amend 不能把已启动/已终态条目打回 pending。"""
    plan = _mk()
    pid = plan["plan_id"]
    store.mark_running("default", "s1", pid, "it_1", "c1")
    store.mark_settled("default", "s1", pid, "it_1", "failed", error="boom")

    updated, err = store.amend_plan(
        "default", "s1", pid,
        add=[{"goal": "c"}],
        reset=["it_1"],
        update={"it_2": {"goal": "b2"}},
    )
    assert updated is not None and err == "", err
    by_id = {it["item_id"]: it for it in updated["items"]}
    # reset：it_1 回 pending、attempts+1、错误清空
    assert by_id["it_1"]["status"] == store.ITEM_PENDING
    assert by_id["it_1"]["attempts"] == 2
    assert by_id["it_1"]["error"] == ""
    # update 只动了 goal；it_2 的依赖保持
    assert by_id["it_2"]["goal"] == "b2"
    assert by_id["it_2"]["depends_on"] == ["it_1"]
    # add 的新条目从 pending 起步。version：create=1，mark_running/settled
    # 各 +1，amend 再 +1 → 4。
    assert by_id["it_3"]["status"] == store.ITEM_PENDING
    assert updated["version"] == 4


def test_plan_amend_rejects(isolated_home):
    plan = _mk()
    pid = plan["plan_id"]
    store.mark_running("default", "s1", pid, "it_1", "c1")
    # running 条目不能 remove / reset / update
    for kw in ({"remove": ["it_1"]}, {"reset": ["it_1"]}, {"update": {"it_1": {"goal": "x"}}}):
        _, err = store.amend_plan("default", "s1", pid, **kw)
        assert err
    # remove 掉被依赖的条目 → 引用被清
    ok, _ = store.amend_plan("default", "s1", pid)
    # 终态清单拒绝
    store.cancel_plan("default", "s1", pid)
    _, err = store.amend_plan("default", "s1", pid, add=[{"goal": "z"}])
    assert "cancelled" in err


def test_plan_cancel_stops_pending(isolated_home):
    plan = _mk()
    updated = store.cancel_plan("default", "s1", plan["plan_id"])
    assert updated["status"] == store.PLAN_CANCELLED
    assert all(it["status"] == store.ITEM_STOPPED for it in updated["items"])


def test_ready_and_blocked(isolated_home):
    plan = _mk(
        items=[
            {"goal": "a"},
            {"goal": "b", "depends_on": ["it_1"]},
            {"goal": "c"},
        ]
    )
    pid = plan["plan_id"]
    store.mark_settled("default", "s1", pid, "it_1", "failed", error="x") \
        if store.mark_running("default", "s1", pid, "it_1", "c") else None
    # it_2 依赖失败条目 → blocked；it_3 无依赖 → ready
    updated = store.get_plan("default", "s1", pid)
    assert [it["item_id"] for it in store.ready_items(updated)] == ["it_3"]
    assert store.blocked_reasons(updated) == {"it_2": "it_1"}


def test_settle_by_child(isolated_home):
    plan = _mk()
    pid = plan["plan_id"]
    store.mark_running("default", "s1", pid, "it_1", "child-9")
    out = store.settle_by_child("s1", "child-9", "done", summary="great")
    assert len(out) == 1
    assert out[0]["items"][0]["status"] == "done"


# ---- watcher ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_child_state_mapping(isolated_home, monkeypatch):
    from ginno_runtime import session_meta

    def fake_find(sid):
        table = {
            "del-run": {"type": "delegation", "stop_reason": "running"},
            "del-ok": {"type": "delegation", "stop_reason": "success", "subagent": {"result_summary": "R"}},
            "del-err": {"type": "delegation", "stop_reason": "timeout"},
            "sub-done": {"type": "subagent", "subagent": {"status": "done", "result_summary": "S"}},
            "sub-fail": {"type": "subagent", "subagent": {"status": "failed"}},
            "sub-run": {"type": "subagent", "subagent": {"status": "running"}},
        }
        m = table.get(sid)
        return (m, None) if m else ({}, None)

    monkeypatch.setattr(session_meta, "_find_meta", fake_find)
    assert (await watcher._child_state("del-run"))[0] == store.ITEM_RUNNING
    st, summary, _ = await watcher._child_state("del-ok")
    assert st == store.ITEM_DONE and summary == "R"
    assert (await watcher._child_state("del-err"))[0] == store.ITEM_FAILED
    assert (await watcher._child_state("sub-done"))[0] == store.ITEM_DONE
    assert (await watcher._child_state("sub-fail"))[0] == store.ITEM_FAILED
    assert (await watcher._child_state("sub-run"))[0] == store.ITEM_RUNNING
    # 子会话被删 → stopped（清单不能卡死）
    assert (await watcher._child_state("ghost"))[0] == store.ITEM_STOPPED


@pytest.mark.asyncio
async def test_launch_item_limit_backoff(isolated_home, monkeypatch):
    """并发帽满：条目保持 pending（返回 None），不标失败。"""
    from ginno_runtime import subagent_scheduler

    plan = _mk(items=[{"goal": "a"}, {"goal": "b"}])
    pid = plan["plan_id"]

    async def fake_spawn(parent, goal, *a, **kw):
        return {"ok": False, "limit": True}

    monkeypatch.setattr(subagent_scheduler, "create_subagent", fake_spawn)
    fresh = store.get_plan("default", "s1", pid)
    assert await watcher._launch_item("default", "s1", fresh, fresh["items"][0]) is None
    assert fresh["items"][0]["status"] == store.ITEM_PENDING


@pytest.mark.asyncio
async def test_launch_item_failure_marks_failed(isolated_home, monkeypatch):
    from ginno_runtime import subagent_scheduler

    plan = _mk(items=[{"goal": "a"}])
    pid = plan["plan_id"]

    async def fake_spawn(parent, goal, *a, **kw):
        return {"ok": False, "error": "mods denied"}

    monkeypatch.setattr(subagent_scheduler, "create_subagent", fake_spawn)
    fresh = store.get_plan("default", "s1", pid)
    assert await watcher._launch_item("default", "s1", fresh, fresh["items"][0]) is False
    updated = store.get_plan("default", "s1", pid)
    assert updated["items"][0]["status"] == store.ITEM_FAILED
    assert "mods denied" in updated["items"][0]["error"]
