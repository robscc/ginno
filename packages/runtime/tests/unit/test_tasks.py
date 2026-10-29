"""Unit: 后台任务系统（background-tasks-design.md）——registry 状态机、store
对账/保留、detached 认领、watch/healthy 匹配、bg_* 工具、注入双路。

reaper 不靠 start()（需要 lifespan 的 loop）：测试直接泵 ``_reap_once()``。
注入侧 monkeypatch ``tasks.inject`` 的协作模块（_ensure_session/_run_stream），
不真起 graph。
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from ginno_runtime import paths
from ginno_runtime.tasks import get_registry, reset_registry
from ginno_runtime.tasks import inject as task_inject
from ginno_runtime.tasks import store as task_store
from ginno_runtime.tools.tasks import build_task_tools

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_registry(isolated_home: Path):
    reset_registry()
    yield
    reset_registry()


def _reap_until(reg, task_id: str, timeout_s: float = 8.0) -> dict:
    """泵 reaper 直到任务离开 running（或超时返回当前 record）。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        reg._reap_once()
        rec = reg.try_get(task_id)
        if rec and rec.get("status") != "running":
            return rec
        time.sleep(0.05)
    reg._reap_once()
    return reg.try_get(task_id) or {}


# --------------------------------------------------------------------------- #
# registry：状态机
# --------------------------------------------------------------------------- #

def test_spawn_finish_exit_code_and_log(isolated_home: Path):
    reg = get_registry()
    rec = reg.spawn(command="echo hi; exit 3", title="t", cwd=None, origin_session=None,
                    origin_turn=None)
    assert rec["status"] == "running"
    assert rec["pid"]
    final = _reap_until(reg, rec["id"])
    assert final["status"] == "finished"
    assert final["exit_code"] == 3
    assert "hi" in task_store.log_path(rec["id"]).read_text()
    # index + meta 双写
    idx = task_store.read_index()
    assert idx[rec["id"]]["status"] == "finished"
    assert json.loads(task_store.meta_path(rec["id"]).read_text())["exit_code"] == 3
    assert final["dur"]


def test_stop_settles_as_stopped(isolated_home: Path):
    reg = get_registry()
    rec = reg.spawn(command="sleep 30", title="s", cwd=None, origin_session=None,
                    origin_turn=None)
    reg.stop(rec["id"])
    final = _reap_until(reg, rec["id"], timeout_s=10)
    assert final["status"] == "stopped"
    assert (final["exit_code"] or 0) < 0  # 信号死


def test_timeout_kills(isolated_home: Path):
    reg = get_registry()
    rec = reg.spawn(command="sleep 30", title="s", cwd=None, origin_session=None,
                    origin_turn=None, timeout_s=1)
    final = _reap_until(reg, rec["id"], timeout_s=10)
    assert final["status"] == "timeout"


def test_concurrency_cap(isolated_home: Path, monkeypatch):
    monkeypatch.setattr(task_store, "config", lambda: {**task_store.DEFAULTS, "max_concurrent": 1})
    reg = get_registry()
    rec = reg.spawn(command="sleep 5", title="a", cwd=None, origin_session=None,
                    origin_turn=None)
    with pytest.raises(RuntimeError, match="max_concurrent"):
        reg.spawn(command="echo x", title="b", cwd=None, origin_session=None,
                  origin_turn=None)
    reg.remove(rec["id"])


def test_remove_kills_and_deletes(isolated_home: Path):
    reg = get_registry()
    rec = reg.spawn(command="sleep 30", title="r", cwd=None, origin_session=None,
                    origin_turn=None)
    reg.remove(rec["id"])
    assert reg.try_get(rec["id"]) is None
    assert rec["id"] not in task_store.read_index()
    assert not task_store.task_dir(rec["id"]).exists()
    _reap_until(reg, rec["id"])  # 不炸（已移除）


def test_failed_to_start(isolated_home: Path):
    reg = get_registry()
    with pytest.raises(RuntimeError):
        reg.spawn(command="echo ok", title="f", cwd="/definitely/not/a/dir",
                  origin_session=None, origin_turn=None)
    recs = [r for r in reg.list() if r["title"] == "f"]
    assert recs and recs[0]["status"] == "failed_to_start"


# --------------------------------------------------------------------------- #
# watch / healthy（P2 泛化）
# --------------------------------------------------------------------------- #

def _with_inject_recorder(monkeypatch) -> list[tuple[dict, str]]:
    calls: list[tuple[dict, str]] = []

    def fake_on_task_event(record, kind, *, line="", label=""):
        calls.append((dict(record), kind))

    monkeypatch.setattr(task_inject, "on_task_event", fake_on_task_event)
    return calls


def test_healthy_fires_once(isolated_home: Path, monkeypatch):
    calls = _with_inject_recorder(monkeypatch)
    reg = get_registry()
    rec = reg.spawn(
        command="echo READY; sleep 30",
        title="dev", cwd=None, origin_session=None, origin_turn=None,
        mode="server", healthy="READY",
    )
    # 泵 reaper 直到 healthy 事件出现（once 语义：只有一个 watcher，重复
    # 行也只发一次——匹配状态在 _WatchState.fired 上）
    deadline = time.time() + 5
    while time.time() < deadline and not any(k == "healthy" for _r, k in calls):
        reg._reap_once()
        time.sleep(0.05)
    kinds = [k for _r, k in calls]
    assert kinds.count("healthy") == 1
    reg.stop(rec["id"])
    final = _reap_until(reg, rec["id"], timeout_s=10)
    assert final["status"] == "stopped"


def test_watch_every_events(isolated_home: Path, monkeypatch):
    calls = _with_inject_recorder(monkeypatch)
    reg = get_registry()
    rec = reg.spawn(
        command="echo HIT one; echo HIT two; sleep 5",
        title="w", cwd=None, origin_session=None, origin_turn=None,
        watch=[{"pattern": "HIT", "label": "hits", "every": True}],
    )
    _reap_until(reg, rec["id"], timeout_s=6)
    reg.stop(rec["id"])
    _reap_until(reg, rec["id"])
    watch_kinds = [k for _r, k in calls if k == "watch"]
    # 两条 HIT 命中但 15s 节流合并为首条（WATCH_MIN_INTERVAL_S）
    assert len(watch_kinds) >= 1


# --------------------------------------------------------------------------- #
# detached（P2）：wrapper 退出码 + 重启认领
# --------------------------------------------------------------------------- #

def test_detached_wrapper_records_exit(isolated_home: Path):
    reg = get_registry()
    rec = reg.spawn(command="echo detached; exit 7", title="d", cwd=None,
                    origin_session=None, origin_turn=None, detached=True)
    final = _reap_until(reg, rec["id"], timeout_s=10)
    assert final["status"] == "finished"
    assert final["exit_code"] == 7
    assert task_store.exit_code_path(rec["id"]).read_text().strip() == "7"


def test_startup_migrate_and_adopt(isolated_home: Path):
    now = time.time()
    idx = {
        "t_child": {"id": "t_child", "status": "running", "detached": False,
                    "pid": 111, "started_at": now - 10, "origin_session": "s1"},
        "t_det_gone": {"id": "t_det_gone", "status": "running", "detached": True,
                       "pid": 999999, "started_at": now - 10, "origin_session": "s1"},
        "t_done": {"id": "t_done", "status": "finished", "exit_code": 0,
                   "detached": False, "started_at": now - 100, "ended_at": now - 90,
                   "origin_session": "s1"},
    }
    task_store.write_index(dict(idx))
    for tid in idx:
        task_store.write_meta(tid, idx[tid])
        task_store.task_dir(tid).mkdir(parents=True, exist_ok=True)

    # detached 但 wrapper 已写好退出码 → 认领为 finished
    task_store.task_dir("t_det_gone").mkdir(parents=True, exist_ok=True)
    task_store.exit_code_path("t_det_gone").write_text("0")

    async def scenario() -> None:
        reg = get_registry()
        reg.start()
        try:
            assert reg.try_get("t_child") is None  # 非 detached 不认领
            gone = task_store.read_index()["t_det_gone"]
            assert gone["status"] == "finished" and gone["exit_code"] == 0
            child = task_store.read_index()["t_child"]
            assert child["status"] == "interrupted"
            assert child.get("interrupted_note") == "app quit"
        finally:
            await reg.stop_all()

    asyncio.run(scenario())


def test_cleanup_expired(isolated_home: Path, monkeypatch):
    monkeypatch.setattr(task_store, "config",
                        lambda: {**task_store.DEFAULTS, "retention_days": 0.0001})
    task_store.write_index({
        "t_old": {"id": "t_old", "status": "finished", "exit_code": 0,
                  "started_at": time.time() - 9999, "ended_at": time.time() - 9999},
    })
    task_store.task_dir("t_old").mkdir(parents=True, exist_ok=True)
    n = task_store.cleanup_expired(task_store.read_index())
    assert n == 1
    assert "t_old" not in task_store.read_index()


def test_shrink_log(isolated_home: Path, monkeypatch):
    monkeypatch.setattr(task_store, "config",
                        lambda: {**task_store.DEFAULTS, "log_cap_mb": 1})
    reg_rec = {"id": "t_shrink", "log_bytes": 0, "truncated": False}
    d = task_store.task_dir("t_shrink")
    d.mkdir(parents=True, exist_ok=True)
    task_store.log_path("t_shrink").write_bytes(b"x" * 100 + b"TAIL-MARKER\n")
    # 1MB 阈值没到 → 不截
    assert not task_store.shrink_log_if_needed("t_shrink", reg_rec)
    # 超 1MB → 截头保尾，TAIL-MARKER 必须还在
    big = b"x" * (1024 * 1024 + 512) + b"TAIL-MARKER\n"
    task_store.log_path("t_shrink").write_bytes(big)
    assert task_store.shrink_log_if_needed("t_shrink", reg_rec)
    assert reg_rec["truncated"]
    assert task_store.read_log_tail("t_shrink", 5)[-1].endswith("TAIL-MARKER")


# --------------------------------------------------------------------------- #
# bg_* 工具
# --------------------------------------------------------------------------- #

@pytest.fixture()
def tools(isolated_home: Path, tmp_path: Path):
    """返回 (invoke, invoke, ...) 五元组——StructuredTool 经 invoke 走 schema
    校验，等价于模型真实调用路径。"""
    bg_run, bg_list, bg_logs, bg_stop, bg_remove = build_task_tools(
        workspace=str(tmp_path), session_id="sess-1"
    )
    return (
        lambda *a, **kw: bg_run.invoke({"command": a[0], **kw} if a else kw),
        lambda *a, **kw: bg_list.invoke(dict(kw)),
        lambda task_id, **kw: bg_logs.invoke({"task_id": task_id, **kw}),
        lambda task_id, **kw: bg_stop.invoke({"task_id": task_id}),
        lambda task_id, **kw: bg_remove.invoke({"task_id": task_id}),
    )


def test_bg_run_returns_machine_header(tools, isolated_home: Path):
    bg_run = tools[0]
    out = bg_run("echo bg-tool-ok")
    assert out.startswith("task=t_")
    assert "status=running" in out
    task_id = out.split()[0].split("=", 1)[1]
    reg = get_registry()
    _reap_until(reg, task_id)
    rec = reg.get(task_id)
    assert rec["origin_session"] == "sess-1"
    assert rec["status"] == "finished" and rec["exit_code"] == 0
    reg.remove(task_id)


def test_bg_run_deny_root(tools, isolated_home: Path):
    out = tools[0]("cat ~/.ssh/config")
    assert out.startswith("[error]") and "denied region" in out


def test_bg_server_requires_healthy(tools, isolated_home: Path):
    out = tools[0]("pnpm dev", mode="server")
    assert out.startswith("[error]") and "healthy" in out


def test_bg_run_warns_on_tail_pipe(tools, isolated_home: Path):
    """`… | tail -60` 攥住全部输出到退出——bg_run 应回 [hint] 提醒（2026-10-29
    实录：pytest 管道 tail 跑了半小时 log_bytes=0，任务中心无实时输出）。"""
    bg_run = tools[0]
    out = bg_run("echo hi | tail -5")
    assert out.startswith("task=t_")  # 任务照常起（不硬拒），只加提示
    assert "[hint]" in out and "tail/head" in out
    task_id = out.split()[0].split("=", 1)[1]
    reg = get_registry()
    _reap_until(reg, task_id)
    reg.remove(task_id)
    # 无管道的正常命令不打扰
    out2 = bg_run("echo plain")
    assert "[hint]" not in out2
    tid2 = out2.split()[0].split("=", 1)[1]
    _reap_until(reg, tid2)
    reg.remove(tid2)


def test_bg_list_logs_stop_remove(tools, isolated_home: Path):
    bg_run, bg_list, bg_logs, bg_stop, bg_remove = tools
    out = bg_run("echo LOG-LINE; sleep 5")
    task_id = out.split()[0].split("=", 1)[1]
    listing = bg_list()
    assert task_id in listing
    # 日志可见（running 时也能读）
    deadline = time.time() + 5
    while time.time() < deadline:
        if "LOG-LINE" in bg_logs(task_id, tail=10):
            break
        time.sleep(0.1)
    assert "LOG-LINE" in bg_logs(task_id, tail=10)
    bg_stop(task_id)
    reg = get_registry()
    _reap_until(reg, task_id)
    assert reg.get(task_id)["status"] == "stopped"
    assert "removed" in bg_remove(task_id)
    assert bg_logs("t_nope", tail=5).startswith("[error]")


# --------------------------------------------------------------------------- #
# 注入双路（steer / idle wake）
# --------------------------------------------------------------------------- #

def test_render_event_templates(isolated_home: Path):
    task_store.task_dir("t_tpl").mkdir(parents=True, exist_ok=True)
    task_store.log_path("t_tpl").write_text("line1\nline2\n")
    text = task_inject.render_event(
        {"id": "t_tpl", "title": "make app", "exit_code": 0, "dur": "12m 30s"},
        "finished",
    )
    assert text.startswith('[task finished] "make app" (t_tpl) exit 0')
    assert "line2" in text and "Full log:" in text


async def test_idle_injection_wakes_turn(isolated_home: Path, monkeypatch):
    import ginno_runtime.api.sessions as sessions_api
    import ginno_runtime.api.stream as stream_api
    from ginno_runtime import server_shared as shared

    shared._SESSIONS["sess-wake"] = {
        "session_id": "sess-wake", "project_slug": "default", "workspace": "",
        "agent_id": "a1", "graph": object(),
    }
    monkeypatch.setattr(sessions_api, "_ensure_session", lambda sid: shared._SESSIONS.get(sid))
    monkeypatch.setattr(sessions_api, "_first_agent_id", lambda: "a1")
    wake_calls: list[dict] = []

    async def fake_run_stream(ws, graph, config, text, session, agent_id, **kw):
        wake_calls.append({"config": config, "text": text, "kw": kw})

    monkeypatch.setattr(stream_api, "_run_stream", fake_run_stream)
    monkeypatch.setattr(task_inject, "COALESCE_S", 0.05)

    task_inject.on_task_event(
        {"id": "t_w1", "title": "build", "origin_session": "sess-wake",
         "exit_code": 0, "dur": "3s"},
        "finished",
    )
    for _ in range(50):
        await asyncio.sleep(0.05)
        if wake_calls:
            break
    assert len(wake_calls) == 1
    cfg = wake_calls[0]["config"]["configurable"]
    assert cfg["usage_source"] == "task"
    assert cfg["user_text"].startswith("[task finished]")
    kw = wake_calls[0]["kw"]
    assert kw["user_extra_kwargs"]["ginno_task_event"]["task"] == "t_w1"
    assert shared._RUNNING_TURNS.get("sess-wake") is None  # 收尾清旗
    shared._SESSIONS.pop("sess-wake", None)


async def test_busy_injection_steers_and_rescues(isolated_home: Path, monkeypatch):
    import ginno_runtime.api.sessions as sessions_api
    from ginno_runtime import server_shared as shared

    shared._SESSIONS["sess-busy"] = {
        "session_id": "sess-busy", "project_slug": "default", "workspace": "",
    }
    monkeypatch.setattr(sessions_api, "_ensure_session", lambda sid: shared._SESSIONS.get(sid))
    # 造一个「活 turn」：_TURN_TASKS 塞一个未完成 future
    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()
    shared._TURN_TASKS["sess-busy"] = fut
    monkeypatch.setattr(task_inject, "_STEER_RESCUE_S", 0.2)

    try:
        task_inject.on_task_event(
            {"id": "t_b1", "title": "tests", "origin_session": "sess-busy",
             "exit_code": 1, "dur": "5s"},
            "finished",
        )
        stash = shared._STEER_STASH.get("sess-busy") or []
        assert any(e.get("origin") == "task" for e in stash)
        entry = next(e for e in stash if e.get("origin") == "task")
        assert entry["extra_kwargs"]["ginno_task_event"]["task"] == "t_b1"
        # turn 结束且未吸收 → 补投 watcher 捞回（转 idle 队列）。watcher 每
        # 1s 醒一次，等到条目消失为止。
        fut.set_result(None)
        shared._TURN_TASKS.pop("sess-busy", None)
        deadline = time.time() + 4
        while time.time() < deadline:
            await asyncio.sleep(0.1)
            stash_now = shared._STEER_STASH.get("sess-busy") or []
            if not any(e.get("origin") == "task" for e in stash_now):
                break
        assert not any(
            e.get("origin") == "task" for e in (shared._STEER_STASH.get("sess-busy") or [])
        )
    finally:
        shared._TURN_TASKS.pop("sess-busy", None)
        shared._STEER_STASH.pop("sess-busy", None)
        shared._SESSIONS.pop("sess-busy", None)
        fut.cancel()


# --------------------------------------------------------------------------- #
# API 回落：重启后的历史任务必须可查（TaskCard 回放轮询路径）
# --------------------------------------------------------------------------- #

def test_task_or_404_falls_back_to_index(monkeypatch):
    """registry 内存 miss（sidecar 重启后只加载 running）→ 回落 index；
    仍无 → 404。2026-10-29 实录：回放会话的 TaskCard 全场 404 触发前端崩溃。"""
    from fastapi import HTTPException

    from ginno_runtime.api import tasks as tasks_api

    class _Reg:
        def try_get(self, task_id):
            return None

    monkeypatch.setattr(tasks_api, "get_registry", lambda: _Reg())
    monkeypatch.setattr(
        tasks_api.task_store, "read_index",
        lambda: {"t_hist": {"id": "t_hist", "status": "interrupted"}})
    rec = tasks_api._task_or_404("t_hist")
    assert rec["status"] == "interrupted"
    with pytest.raises(HTTPException) as ei:
        tasks_api._task_or_404("t_gone")
    assert ei.value.status_code == 404
