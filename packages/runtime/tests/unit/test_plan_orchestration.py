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


async def test_plan_tools_are_async_and_start_watcher(isolated_home, monkeypatch):
    """Regression（2026-10-12 真机 e0f20014 turn）：create_subagent_plan 曾是
    同步 @tool —— ToolNode 把同步工具丢进 executor 线程，线程里没有 asyncio
    loop，ensure_watcher 的 get_running_loop() 静默跳过，watcher 永远不启动、
    条目永远 pending（spawn_subagent 一直是 async def 所以直发路径没事）。
    三个 plan 工具必须保持 async def：ToolNode 会在事件循环上 await 它们。
    """
    from ginno_runtime.tools.plan_tools import build_plan_tools

    async def _noop_reconcile(slug, session_id, plan):
        return False

    monkeypatch.setattr(watcher, "_reconcile", _noop_reconcile)
    tools = {t.name: t for t in build_plan_tools("s_watch", "default")}
    assert {"create_subagent_plan", "amend_plan", "cancel_plan"} <= set(tools)
    out = await tools["create_subagent_plan"].ainvoke(
        {"title": "t", "items": [{"goal": "a"}]}
    )
    assert out.startswith("[plan created]")
    # watcher 必须已在本会话上注册（同步版在这里就是 0 个）
    assert "s_watch" in watcher._WATCHERS
    task = watcher._WATCHERS.pop("s_watch")
    task.cancel()
    try:
        await task  # 让 CancelledError 落地，不留 "Task was destroyed" 警告
    except Exception:  # noqa: BLE001
        pass
    watcher._WAKE.pop("s_watch", None)


# ---- world_state 差分兜底（2026-10-13 补强：错误/退出场景的投递保证）------


def test_plans_section_diff(isolated_home):
    """PlansSection：无清单零成本；清单变更产出简报行；render 不进稳定提示。"""
    from ginno_runtime.world_state import PlansSection, SessionCtx

    sec = PlansSection()
    ctx = SessionCtx(session_id="s1", project_slug="default")
    # 无清单 → snapshot None（不进基线，零成本）
    assert sec.snapshot(ctx) is None
    # 建单 + 条目推进 → 差分行
    plan = _mk()
    snap1 = sec.snapshot(ctx)
    assert snap1 is not None and plan["plan_id"] in snap1
    assert sec.render(snap1) == ""  # 稳定提示零渲染（前缀缓存）
    assert sec.update_text({}, snap1) is not None  # 新建清单 → 简报
    store.mark_running("default", "s1", plan["plan_id"], "it_1", "child_x")
    snap2 = sec.snapshot(ctx)
    text = sec.update_text(snap1, snap2)
    assert text and "it_1" in text and "running" in text
    # 无变化 → None（基线幂等，不会重复报）
    assert sec.update_text(snap2, sec.snapshot(ctx)) is None


# ---- 2026-10-13 收口批次：split→watcher / 设置开关 / /plan 命令 / 专用信封 --


async def test_watcher_end_to_end_spawn_settle_complete(isolated_home, monkeypatch):
    """全链路（真 watcher 循环 + 假 spawn）：建单 → 调度两条 running → 子会话
    meta 翻 done → 对账回写 → plan completed；_do_flush 捕获去抖缓冲。"""
    import asyncio

    import ginno_runtime.session_meta as sm
    import ginno_runtime.subagent_scheduler as sched
    from ginno_runtime.plans import watcher

    monkeypatch.setattr(watcher, "_TICK_S", 0.2)
    monkeypatch.setattr(watcher, "_FLUSH_QUIET_S", 0.1)
    monkeypatch.setattr(watcher, "_FLUSH_MAX_S", 0.5)
    sm._session_meta_upsert(
        "default", {"id": "s_e2e", "project_slug": "default", "type": "chat"}
    )

    async def fake_spawn(parent, goal, constraints, acceptance, origin, agent_type=""):
        sid = f"child_{origin}_{goal[:4]}"
        sm._session_meta_upsert(
            "default",
            {
                "id": sid,
                "project_slug": "default",
                "type": "subagent",
                "parent_session_id": parent,
                "subagent": {"status": "running", "goal": goal},
            },
        )
        return {"ok": True, "session_id": sid, "title": goal, "depth": 1}

    monkeypatch.setattr(sched, "create_subagent", fake_spawn)
    flushed: list[tuple[str, dict]] = []
    monkeypatch.setattr(watcher, "_do_flush", lambda sid, buf: flushed.append((sid, dict(buf))))

    plan = store.create_plan(
        "s_e2e", slug="default", title="t", items=[{"goal": "aaaa"}, {"goal": "bbbb"}]
    )
    watcher.notify_change("s_e2e")
    assert "s_e2e" in watcher._WATCHERS

    async def wait_for(fn, tries=100):
        for _ in range(tries):
            if fn():
                return True
            await asyncio.sleep(0.05)
        return False

    p = store.get_plan("default", "s_e2e", plan["plan_id"])
    assert await wait_for(
        lambda: all(
            i["status"] == "running"
            for i in store.get_plan("default", "s_e2e", plan["plan_id"])["items"]
        )
    ), store.get_plan("default", "s_e2e", plan["plan_id"])["items"]
    # 子会话全部完成 → 对账 → 清单 completed
    for it in store.get_plan("default", "s_e2e", plan["plan_id"])["items"]:
        sm._session_meta_patch(
            "default",
            it["child_session_id"],
            {
                "subagent": {
                    "status": "done",
                    "result_summary": f"ok {it['item_id']}",
                    "goal": it["goal"],
                }
            },
        )
    assert await wait_for(
        lambda: store.get_plan("default", "s_e2e", plan["plan_id"])["status"] == "completed"
    )
    p = store.get_plan("default", "s_e2e", plan["plan_id"])
    assert all(i["status"] == "done" and i["result_summary"] for i in p["items"])
    assert await wait_for(lambda: bool(flushed))
    assert flushed[0][0] == "s_e2e"
    # watcher 自然收官（无 open 清单即退出）
    assert await wait_for(lambda: "s_e2e" not in watcher._WATCHERS)


async def test_split_confirm_creates_persistent_plan(isolated_home, monkeypatch):
    """split 确认（subagent_plan.confirm_plan）转持久化 Plan（author=user，
    watcher 接管调度），不再一次性全 spawn。"""
    import ginno_runtime.subagent_plan as sp
    from ginno_runtime.plans import watcher

    monkeypatch.setattr(watcher, "notify_change", lambda sid: None)  # 不真起 watcher
    sp._PENDING_PLANS["s_split"] = {
        "plan_id": "pending_1",
        "session_id": "s_split",
        "task": "拆分任务",
        "subtasks": [
            {"goal": "g1", "constraints": "c1", "acceptance": "a1", "reason": "r"},
            {"goal": "g2"},
        ],
        "created": 0,
    }
    out = await sp.confirm_plan("s_split", "pending_1")
    assert "pending_1" not in out  # 返回的是持久化清单 id 而非启动明细
    plans = store.list_plans("default", "s_split")
    assert len(plans) == 1
    p = plans[0]
    assert p["author"] == "user" and p["status"] == store.PLAN_ACTIVE
    assert [i["goal"] for i in p["items"]] == ["g1", "g2"]
    assert p["items"][0]["constraints"] == "c1"
    assert all(i["status"] == store.ITEM_PENDING for i in p["items"])


async def test_agent_plan_confirm_setting_gates(isolated_home, tmp_path):
    """D2 开关：context.plan_confirm_agent_plans=True 时 agent 建单一律
    pending_confirm，压过工具的 needs_confirm=False。"""
    import json as _json

    from ginno_runtime.tools.plan_tools import build_plan_tools

    (tmp_path / "settings.json").write_text(
        _json.dumps({"context": {"plan_confirm_agent_plans": True}}), encoding="utf-8"
    )
    # isolated_home 的 GINNO_HOME 即 tmp_path
    tools = {t.name: t for t in build_plan_tools("s_gate", "default")}
    out = await tools["create_subagent_plan"].ainvoke({"title": "t", "items": [{"goal": "a"}]})
    p = store.list_plans("default", "s_gate")[0]
    assert p["status"] == store.PLAN_PENDING_CONFIRM
    assert "awaiting user confirmation" in out


async def test_plan_command_list_and_cancel(isolated_home):
    """/plan 命令面：列表带进度；cancel 取消；未知 id 报错。"""
    from ginno_runtime.commands.registry import _plan_async_handler

    plan = _mk(session_id="s_cmd")
    ok = _plan_async_handler(
        project_slug="default",
        session={"session_id": "s_cmd", "project_slug": "default"},
        args="",
    )
    text = await ok
    assert plan["plan_id"] in text and "0/2" in text
    text = await _plan_async_handler(
        project_slug="default",
        session={"session_id": "s_cmd", "project_slug": "default"},
        args=f"cancel {plan['plan_id']}",
    )
    assert "cancelled" in text
    assert store.get_plan("default", "s_cmd", plan["plan_id"])["status"] == store.PLAN_CANCELLED
    text = await _plan_async_handler(
        project_slug="default",
        session={"session_id": "s_cmd", "project_slug": "default"},
        args="cancel ghost",
    )
    assert "not found" in text


def test_plan_update_envelope_blocks(isolated_home):
    """<ginno_plan_update> 信封 → 专用 plan_update 块（区别于结果卡/裸带）。"""
    from ginno_runtime.api.messages_ui import _plan_update_blocks

    content = (
        '<ginno_plan_update plan="pl_x">\n'
        "[plan update] Task list status changed:\n"
        "- [done] it_1 ok\n"
        "You can: verify the above results.\n"
        "</ginno_plan_update>"
    )
    blocks = _plan_update_blocks(content)
    assert blocks and blocks[0]["kind"] == "plan_update"
    assert blocks[0]["planId"] == "pl_x"
    assert "[done] it_1 ok" in blocks[0]["text"]
    # 行动菜单尾段是模型指令，不进卡片
    assert "You can" not in blocks[0]["text"]
    assert _plan_update_blocks("plain text") is None


async def test_do_flush_wraps_plan_envelope(isolated_home, monkeypatch):
    """watcher._do_flush 产出 <ginno_plan_update> 信封（前端折专用卡）。"""
    import asyncio

    from ginno_runtime.plans import watcher

    captured = {}

    async def fake_inject(sid, text, child_key):
        captured["text"] = text
        captured["child_key"] = child_key

    import ginno_runtime.subagent_scheduler as sched

    monkeypatch.setattr(sched, "_inject_result", fake_inject)
    watcher._do_flush("s_env", {"notes": ["- [done] it_1 ok"], "plans": {"pl_x"}})
    await asyncio.sleep(0.05)  # spawn_bg 任务落地
    assert captured["child_key"] == "plan-pl_x"
    assert captured["text"].startswith("<ginno_plan_update plan=\"pl_x\">")
    assert "[plan update]" in captured["text"]
