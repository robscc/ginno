"""损坏降级硬线（settings-integrity 复核补强，2026-11-22）的行为契约。

验收面（goal 原文对照）：

- **真实 startup 验证预置可执行任务不运行**：损坏 settings.json + 预置
  due 的 schedule 任务 → 真实 lifespan 启动后调度器 tick 循环不存在
  （``scheduler._LOOP_TASK is None``）、任务零执行记录；健康文件对照
  启动不受影响（调度器正常起）。
- **ASPI 闸门**：损坏期间非白名单 HTTP → 503 ``settings_corrupt_degraded``、
  WebSocket → 升级被拒；``/api/health``、``/api/settings*``（恢复读写
  面）、静态路径放行；repair 落盘后自动解除。
- **恢复后陈旧 repair 拒绝**：文件已被修好（手动/先到 repair）后迟到的
  repair → 409 ``settings_not_corrupt``，内容不被旧快照覆盖。
- **startup 跳过名单快照**：哪些任务带 ``skip_when_settings_corrupt``
  被钉死（防漂移）；跳过仍计 completed（depends_on 守卫不炸）。
- **恢复后提示重启**：startup 曾降级 → health 带 ``restart_required``，
  即使文件已修好（scheduler 等组件需重启补启）。
"""

from __future__ import annotations

import asyncio
import json as stdjson
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from ginno_runtime import scheduler, schedule_store, server, settings_store
from ginno_runtime import startup_tasks as st
from ginno_runtime.startup_tasks import (
    PHASE_STARTUP,
    PhaseTask,
    execute_phase,
)

pytestmark = [pytest.mark.unit, pytest.mark.api]

CORRUPT_SAMPLE = '{"providers": {"custom": {"api_key": "sk-live-1234"'

#: 损坏降级跳过名单（startup_tasks.py STARTUP_TASKS 前注释的理由逐条
#: 对应）。增删必须是有意识的安全决策——执行/恢复/外部进程型任务不得
#: 在损坏态静默恢复启动。
CORRUPT_SKIP_SNAPSHOT = {
    "plan_watcher_recovery",   # 恢复 active 清单 watcher = 续跑执行
    "team_recovery",           # 恢复型结构变更（建/删会话）
    "requeue_pending_titles",  # 后台 LLM 标题生成
    "connector_seed",          # 渠道注册（配置在损坏文件里）
    "mcp_background_connect",  # 连外部 MCP 进程
    "scheduler_start",         # 定时执行 + 孤儿 running 自动续跑
    "tasks_registry_start",    # 后台任务恢复设施（detached 认领 + reaper）
    "lastwords_recover",       # auto headless 续跑恢复
}


@pytest.fixture
def corrupt_client(isolated_home):
    """隔离 home + 预置损坏 settings.json，再真实跑 lifespan 启动。"""
    sp = isolated_home / "settings.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(CORRUPT_SAMPLE, encoding="utf-8")
    with TestClient(server.app) as c:
        yield c


def _write_raw(home, text: str) -> None:
    (home / "settings.json").write_text(text, encoding="utf-8")


def _seed_due_task() -> str:
    """预置一个「到期立即可执行」的 once 任务（enabled、at 在过去）。"""
    due = (datetime.now() - timedelta(minutes=1)).isoformat(timespec="seconds")
    task = {
        "id": schedule_store.new_task_id(),
        "name": "corrupt-mode-probe",
        "enabled": True,
        "agent_id": "todo",
        "target": {"type": "prompt", "prompt": "hello"},
        "schedule": {"kind": "once", "at": due},
        "created": 1,
        "updated": 1,
    }
    schedule_store.upsert_task(task)
    return task["id"]


# ---- 表守卫：跳过名单快照 ----------------------------------------------------


def test_corrupt_skip_snapshot():
    """带 skip_when_settings_corrupt 的任务恰为快照集合（防静默漂移）。"""
    actual = {
        t.name for t in st.STARTUP_TASKS if t.skip_when_settings_corrupt
    }
    assert actual == CORRUPT_SKIP_SNAPSHOT


def test_gated_tasks_have_no_dependents():
    """被跳任务的 name 不出现在任何 depends_on 里（跳过=completed 的语义
    不会让某个依赖者拿着半初始化状态跑）。"""
    gated = CORRUPT_SKIP_SNAPSHOT
    for t in st.STARTUP_TASKS + st.SHUTDOWN_TASKS:
        assert not (set(t.depends_on) & gated), (
            f"{t.name} depends on gated task(s) {set(t.depends_on) & gated}"
        )


async def test_execute_phase_skips_gated_and_keeps_order(tmp_path, monkeypatch):
    """执行器单元级：损坏 → gate 任务跳过且计 completed；健康 → 照跑。"""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    ran: list[str] = []

    def _mk(name: str, gated: bool) -> PhaseTask:
        return PhaseTask(
            name=name,
            phase=PHASE_STARTUP,
            run=lambda h, _n=name: ran.append(_n),
            skip_when_settings_corrupt=gated,
        )

    table = (_mk("first", True), _mk("second", False), _mk("third", True))

    # 健康（文件不存在 = missing，非损坏）：全部照跑
    completed = await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert ran == ["first", "second", "third"]
    assert completed == ["first", "second", "third"]

    # 损坏：gate 任务跳过（仍计 completed），非 gate 照跑
    ran.clear()
    _write_raw(tmp_path, CORRUPT_SAMPLE)
    completed = await execute_phase(table, PHASE_STARTUP, st.LifespanHandles())
    assert ran == ["second"]
    assert completed == ["first", "second", "third"]


# ---- 真实 startup：预置可执行任务不运行 --------------------------------------


def test_real_startup_corrupt_skips_execution_tasks(corrupt_client, isolated_home):
    """损坏启动：调度器 tick 循环不存在、预置 due 任务零执行记录、
    hooks 空 dispatcher（fail-closed）、恢复面可用、其余 API 503。"""
    # 预置到期任务（schedule.json 独立于 settings.json，任务真实存在）
    task_id = _seed_due_task()

    # lifespan 已在 fixture 里跑完（损坏态）——此刻核验副作用
    assert scheduler._LOOP_TASK is None, "scheduler tick loop must not start"
    assert schedule_store.get_task(task_id) is not None
    runs = schedule_store.query_runs(task_id=task_id)
    assert runs["total"] == 0, "due task must not run in corrupt-degraded mode"

    # hooks fail-closed（既有防线，不因本硬线削弱）
    from ginno_runtime import server_shared

    assert server_shared._hooks is not None
    assert server_shared._hooks.settings == {}

    # 恢复面放行 + 状态如实
    h = corrupt_client.get("/api/settings/health").json()
    assert h["status"] == "corrupt"
    assert h["restart_required"] is True  # startup 曾降级 → 提示重启
    assert corrupt_client.get("/api/health").status_code == 200

    # 非白名单 API → 503 降级（不触发实际工作）
    r = corrupt_client.get("/api/sessions")
    assert r.status_code == 503
    assert r.json()["error"] == "settings_corrupt_degraded"


def test_real_startup_healthy_starts_scheduler(client):
    """对照：健康文件正常启动——调度器照常起（硬线零影响）。"""
    assert scheduler._LOOP_TASK is not None
    assert not scheduler._LOOP_TASK.done()


# ---- ASGI 闸门：UI/WS/渠道入口 ----------------------------------------------


def test_gate_allows_static_and_settings_recovery_plane(corrupt_client):
    """静态资产与前端页面放行（受限恢复 UI 要能加载）；settings 面
    放行（写由 store 层 409 拒绝，repair 是唯一写通道）。"""
    r = corrupt_client.get("/")
    assert r.status_code != 503  # 静态/404 都行，但不得被降级闸门拦
    # 前端路由页面（横幅所在）与 RSC payload 形态放行
    for path in ("/settings", "/_next/static/app.js"):
        assert corrupt_client.get(path).status_code != 503
    assert corrupt_client.get("/api/settings/health").status_code == 200
    assert corrupt_client.get("/api/settings").status_code == 200
    assert corrupt_client.get("/api/settings/env").status_code == 200
    # settings 域写操作由 store 层统一 409（防御纵深第二层）
    assert corrupt_client.patch(
        "/api/settings/env", json={"updates": []}
    ).status_code in (200, 400)


def test_gate_blocks_work_apis_and_channels(corrupt_client):
    """执行/渠道入口全拒：会话、调度触发、A2A、connector 面板、以及
    **不带 /api 前缀的功能端点**（/mcp MCP JSON-RPC、FastAPI 文档面）。"""
    for path in (
        "/api/sessions",
        "/api/schedule/tasks",
        "/api/workflows",
        "/api/connectors",
        "/api/a2a/peers",
        "/mcp",           # MCP JSON-RPC（可驱动工具执行）——无 /api 前缀
        "/mcp/sub",
        "/a2a/some-token",  # A2A 委派端点（若挂进主 app）
        "/docs",
        "/openapi.json",
    ):
        r = corrupt_client.get(path)
        assert r.status_code == 503, (path, r.status_code)
        assert r.json()["error"] == "settings_corrupt_degraded"
    # POST /mcp 同样拒（initialize 也不给——降级态不营业）
    r = corrupt_client.post("/mcp", json={"method": "initialize", "id": 1})
    assert r.status_code == 503


def test_gate_refuses_websocket(corrupt_client):
    """损坏期间一切 WS 升级被拒（turn/schedule/connector 通道）。"""
    with pytest.raises(WebSocketDisconnect):
        with corrupt_client.websocket_connect("/api/ws/sessions/probe"):
            pass


def test_gate_lifts_after_repair(corrupt_client, isolated_home):
    """repair 落盘 → 闸门动态解除（无需重启关闸）；修复后陈旧 repair
    被拒（409 settings_not_corrupt），健康内容不被旧快照覆盖。"""
    r = corrupt_client.put(
        "/api/settings/repair", json={"language": "zh-CN", "repaired": 1}
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["restart_required"] is True  # 恢复后提示重启
    assert body["backup"]  # pre-repair 备份已生成
    # 备份逐字节保留损坏原文件
    import pathlib

    assert pathlib.Path(body["backup"]).read_bytes() == CORRUPT_SAMPLE.encode()

    # 闸门解除：工作 API 恢复可达（不再 503）
    r = corrupt_client.get("/api/sessions")
    assert r.status_code != 503, r.text

    # 陈旧 repair：文件已健康 → 409 拒绝，内容不被覆盖
    r2 = corrupt_client.put(
        "/api/settings/repair", json={"stale": "snapshot-from-old-banner"}
    )
    assert r2.status_code == 409
    assert r2.json()["error"] == "settings_not_corrupt"
    on_disk = stdjson.loads((isolated_home / "settings.json").read_text())
    assert on_disk == {"language": "zh-CN", "repaired": 1}

    # 修复后 health 仍带 restart_required（startup 跳过的组件需重启补启）
    h = corrupt_client.get("/api/settings/health").json()
    assert h["status"] == "ok"
    assert h["restart_required"] is True


def test_repair_missing_state_writes_without_backup(tmp_path, monkeypatch):
    """missing 态 repair 等同首次写（无文件可覆盖，无备份动作）。"""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    result = settings_store.repair({"language": "en"})
    assert result["backup"] is None
    assert stdjson.loads((tmp_path / "settings.json").read_text()) == {"language": "en"}
