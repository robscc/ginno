"""§六 /workflow-{action} 命令族 — BUILTINS 薄封装层测试。

直调 async handler（/subagent 命令测试先例）；workflow 定义/run 用真实
文件 store（isolated_home），引擎侧只 monkeypatch 边界（``_spawn_run_task``
不真跑 LLM、``engine.request_pause`` 模拟活 loop）。断言用英文文案
（isolated_home pin 英文 locale，纯中文断言会挂）。
"""

from __future__ import annotations

import pytest

from ginno_runtime import workflows as wf_store
from ginno_runtime.commands.registry import BUILTINS, _help_handler
from ginno_runtime.commands.resolver import resolve_turn
from ginno_runtime.workflows import store as wf_storemod

pytestmark = pytest.mark.unit

SESSION = {"project_slug": "default", "session_id": "sess-1"}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _mk_def(name: str = "Demo", system: bool = False) -> dict:
    return wf_store.create_def(
        {
            "name": name,
            "system": system,
            "dsl": {
                "entry": "s1",
                "nodes": [{"id": "s1", "type": "step", "agent": "dev", "goal": "do it"}],
                "edges": [],
            },
        }
    )


def _mk_run(wf: dict, **overrides) -> dict:
    run = wf_store.create_run(wf, session_id="sess-1")
    if overrides:
        run.update(overrides)
        wf_storemod._write_json(wf_storemod._run_path(run["id"]), run)
    return run


def _no_spawn(monkeypatch) -> list:
    """Stop the run engine from starting; record spawn calls instead.

    The discarded coroutine is closed so pytest doesn't warn about it
    never being awaited."""
    calls: list = []

    def _fake_spawn(run_id: str, coro) -> None:
        coro.close()
        calls.append(run_id)

    monkeypatch.setattr("ginno_runtime.api.workflows._spawn_run_task", _fake_spawn)
    return calls


async def _run_cmd(name: str, args: str = "", session=SESSION) -> str:
    return await BUILTINS[name].async_handler("default", session, args)


# --------------------------------------------------------------------------- #
# registration + /help + resolver routing
# --------------------------------------------------------------------------- #
def test_family_registered_with_async_handlers(isolated_home):
    names = [
        "workflow-run",
        "workflow-status",
        "workflow-pause",
        "workflow-resume",
        "workflow-cancel",
        "workflow-list",
        "workflow-delete",
    ]
    for n in names:
        assert n in BUILTINS, n
        assert BUILTINS[n].async_handler is not None


def test_help_lists_family(isolated_home):
    out = _help_handler("default")
    for n in (
        "workflow-run",
        "workflow-status",
        "workflow-pause",
        "workflow-resume",
        "workflow-cancel",
        "workflow-list",
        "workflow-delete",
    ):
        assert f"`/{n}`" in out


def test_resolve_turn_routes_family_to_async_path(isolated_home):
    plan = resolve_turn({"message": "/workflow-list"}, SESSION)
    assert plan.builtin_async == "workflow-list"
    assert plan.builtin_reply is None
    plan = resolve_turn({"message": "/workflow-run Demo"}, SESSION)
    assert plan.builtin_async == "workflow-run" and plan.builtin_args == "Demo"


# --------------------------------------------------------------------------- #
# /workflow-run
# --------------------------------------------------------------------------- #
async def test_run_fuzzy_name_match_starts_run(isolated_home, monkeypatch):
    spawns = _no_spawn(monkeypatch)
    wf = _mk_def("Daily Report")
    reply = await _run_cmd("workflow-run", "daily")
    assert "started" in reply and "Daily Report" in reply and "run `" in reply
    assert "Daily Report" in reply
    assert "notified" in reply  # 完成会通知提示
    assert len(spawns) == 1
    run = wf_store.list_runs()[0]
    assert run["workflow_id"] == wf["id"] and run["status"] == "running"
    assert run["session_id"] == "sess-1"  # 绑定发起会话（in-chat 渲染）


async def test_run_by_exact_id(isolated_home, monkeypatch):
    _no_spawn(monkeypatch)
    wf = _mk_def("Alpha")
    reply = await _run_cmd("workflow-run", wf["id"])
    assert "started" in reply and "Alpha" in reply


async def test_run_multiple_candidates_no_spawn(isolated_home, monkeypatch):
    spawns = _no_spawn(monkeypatch)
    _mk_def("Weekly Report")
    _mk_def("Biweekly Report")
    reply = await _run_cmd("workflow-run", "week")
    assert "matches 2 workflows" in reply
    assert "Weekly Report" in reply and "Biweekly Report" in reply
    assert spawns == []  # 多候选待选：不猜、不起 run
    assert wf_store.list_runs() == []


async def test_run_no_match_lists_existing(isolated_home, monkeypatch):
    _no_spawn(monkeypatch)
    _mk_def("Alpha")
    reply = await _run_cmd("workflow-run", "nope")
    assert "No workflow matches `nope`" in reply
    assert "Alpha" in reply  # 列出现有定义供参考


async def test_run_no_args_returns_usage(isolated_home):
    reply = await _run_cmd("workflow-run", "")
    assert "/workflow-run <name|id>" in reply


async def test_run_no_workflows_friendly(isolated_home):
    reply = await _run_cmd("workflow-run", "x")
    assert "No workflows yet" in reply and "Workflows page" in reply


# --------------------------------------------------------------------------- #
# /workflow-status — 缺省最近 run / 多 running 候选 / interrupt 提示
# --------------------------------------------------------------------------- #
async def test_status_defaults_to_latest_run(isolated_home):
    wf = _mk_def("Latest")
    r_old = _mk_run(wf)  # running，较早
    r_new = _mk_run(wf, status="done")  # 更晚创建 → 全局最近
    reply = await _run_cmd("workflow-status", "")
    assert r_new["id"] in reply and r_old["id"] not in reply
    assert "done" in reply and "Progress: 0/1 steps" in reply


async def test_status_by_explicit_run_id(isolated_home):
    wf = _mk_def("Explicit")
    r = _mk_run(wf)
    reply = await _run_cmd("workflow-status", r["id"])
    assert r["id"] in reply


async def test_status_multiple_running_lists_candidates(isolated_home):
    wf = _mk_def("Parallel")
    r1 = _mk_run(wf)
    r2 = _mk_run(wf)
    reply = await _run_cmd("workflow-status", "")
    assert "Multiple runs are in progress" in reply
    assert r1["id"] in reply and r2["id"] in reply


async def test_status_pending_interrupt_points_to_workflows_page(isolated_home):
    wf = _mk_def("HumanGate")
    r = _mk_run(
        wf,
        status="paused",
        pending_interrupt={"kind": "human", "node": "h", "question": "approve?"},
    )
    reply = await _run_cmd("workflow-status", r["id"])
    assert "Workflows page" in reply and "approve?" in reply


async def test_status_failed_run_shows_error(isolated_home):
    wf = _mk_def("Boom")
    r = _mk_run(wf, status="failed", error="node s1 exploded")
    reply = await _run_cmd("workflow-status", r["id"])
    assert "failed" in reply and "node s1 exploded" in reply


async def test_status_no_runs_friendly(isolated_home):
    _mk_def("Empty")
    reply = await _run_cmd("workflow-status", "")
    assert "No workflow runs yet" in reply


async def test_status_unknown_run_id(isolated_home):
    reply = await _run_cmd("workflow-status", "deadbeef00")
    assert "not found" in reply


# --------------------------------------------------------------------------- #
# /workflow-pause · resume · cancel — 缺省解析 + 端点分派
# --------------------------------------------------------------------------- #
async def test_pause_dispatches_to_latest_run(isolated_home, monkeypatch):
    _no_spawn(monkeypatch)
    monkeypatch.setattr("ginno_runtime.workflows.engine.request_pause", lambda rid: True)
    wf = _mk_def("Pausable")
    run = _mk_run(wf)
    reply = await _run_cmd("workflow-pause", "")
    assert f"Pause requested for run `{run['id']}`" in reply


async def test_pause_without_live_loop_surfaces_endpoint_error(isolated_home, monkeypatch):
    """真跑 pause 端点：无活 loop → 409 detail 被摊平为回执（非异常逃逸）。"""
    _no_spawn(monkeypatch)
    monkeypatch.setattr("ginno_runtime.workflows.engine.request_pause", lambda rid: False)
    wf = _mk_def("NoLoop")
    run = _mk_run(wf)
    reply = await _run_cmd("workflow-pause", run["id"])
    assert "Pause failed" in reply and "no live execution loop" in reply


async def test_resume_plain_paused_run(isolated_home, monkeypatch):
    _no_spawn(monkeypatch)
    wf = _mk_def("Resumable")
    run = _mk_run(wf, status="paused")
    reply = await _run_cmd("workflow-resume", run["id"])
    assert f"Run `{run['id']}` resumed" in reply


async def test_resume_pending_human_redirects_to_workflows_page(isolated_home, monkeypatch):
    spawns = _no_spawn(monkeypatch)
    wf = _mk_def("HumanOnly")
    run = _mk_run(
        wf,
        status="paused",
        pending_interrupt={"kind": "human", "node": "h", "question": "approve?"},
    )
    reply = await _run_cmd("workflow-resume", run["id"])
    assert "Workflows page" in reply
    assert spawns == []  # 未触发 resume 执行


async def test_cancel_real_endpoint_marks_cancelled(isolated_home, monkeypatch):
    _no_spawn(monkeypatch)
    wf = _mk_def("Cancellable")
    run = _mk_run(wf)
    reply = await _run_cmd("workflow-cancel", "")
    assert f"Run `{run['id']}` cancelled" in reply
    assert wf_store.get_run(run["id"])["status"] == "cancelled"


async def test_pause_resume_cancel_no_runs_friendly(isolated_home):
    for cmd in ("workflow-pause", "workflow-resume", "workflow-cancel"):
        reply = await _run_cmd(cmd, "")
        assert "No workflow runs yet" in reply, cmd


# --------------------------------------------------------------------------- #
# /workflow-list
# --------------------------------------------------------------------------- #
async def test_list_table(isolated_home):
    wf_a = _mk_def("Alpha Flow")
    _mk_def("Beta Flow")
    _mk_def("Locked Flow", system=True)
    _mk_run(wf_a, status="done")
    reply = await _run_cmd("workflow-list")
    assert "**Workflows** (3)" in reply
    assert "Alpha Flow" in reply and "v1" in reply and "1 steps" in reply
    assert "last run done" in reply
    assert "never run" in reply
    assert "🔒" in reply  # system 定义标记


async def test_list_empty_friendly(isolated_home):
    reply = await _run_cmd("workflow-list")
    assert "No workflows yet" in reply


# --------------------------------------------------------------------------- #
# /workflow-delete
# --------------------------------------------------------------------------- #
async def test_delete_receipt_and_run_history_kept(isolated_home):
    wf = _mk_def("Disposable")
    run = _mk_run(wf, status="done")
    reply = await _run_cmd("workflow-delete", "disposable")
    assert "Deleted workflow **Disposable**" in reply
    assert "history is kept" in reply
    assert wf_store.get_def(wf["id"]) is None
    assert wf_store.get_run(run["id"]) is not None  # 历史 run 保留


async def test_delete_system_workflow_rejected(isolated_home):
    wf = _mk_def("BuiltIn", system=True)
    reply = await _run_cmd("workflow-delete", "BuiltIn")
    assert "Built-in workflow" in reply
    assert wf_store.get_def(wf["id"]) is not None


async def test_delete_multiple_candidates(isolated_home):
    _mk_def("Weekly Report")
    _mk_def("Biweekly Report")
    reply = await _run_cmd("workflow-delete", "week")
    assert "matches 2 workflows" in reply
    assert len(wf_store.list_defs()) == 2  # 未删任何


async def test_delete_no_match_lists_existing(isolated_home):
    _mk_def("Alpha")
    reply = await _run_cmd("workflow-delete", "nope")
    assert "No workflow matches `nope`" in reply and "Alpha" in reply


async def test_delete_no_args_returns_usage(isolated_home):
    reply = await _run_cmd("workflow-delete", "")
    assert "/workflow-delete" in reply
