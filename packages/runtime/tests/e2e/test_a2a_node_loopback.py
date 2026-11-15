"""A2A 自环 e2e（N2b 验收硬要求）—— 本机起 node 形态 A2A server，真 A2AClient 派活。

设计 §3 N2「自环 e2e（主 Ginno 派活给本机 node）」+ 验收 #7/#8/#17/#31/#32/#40。
**只有 LLM 是脚本缝（GINNO_FAKE_LLM）**——node 起服、A2AClient、入站执行层、worker
子进程、turn 链全部真实现。断言：
- 任务真被执行（终态 + 回报内容，**不是 submitted 占位**）
- trust=readonly 写操作被拒（guard 双侧强制，#8/#31）
- exec_root 越界被拒（fail-loud，#32）
- 超时 killpg 清理（#40，任务转 failed 不悬空 working）

端口纪律：**port=0 随机端口**，绝不占 8787/8790（本机 Ginno 在跑）。worker 子进程
start_new_session 隔离；fixture 收尾 stop listener + executor.aclose 杀活 worker。
路径隔离：GINNO_HOME→tmp_path/home，exec_root→tmp_path/ws（home 之外，避免 runtime-home
强制只读挡住写产物）。
"""

from __future__ import annotations

import asyncio

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a import inbound, node
from ginno_runtime.a2a.client import A2AClient
from ginno_runtime.a2a.models import Task, TaskState

pytestmark = pytest.mark.e2e


@pytest.fixture
async def node_factory(tmp_path, monkeypatch):
    """起一个真 node 形态 A2A server（port=0），返回 (handle, endpoint, ws)。

    每次调用独立 home/scripts/policy；收尾统一停服 + 杀活 worker + 清 handle 表。
    """
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("GINNO_HOME", str(home))
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    inbound._HANDLES.clear()
    started: list = []

    async def _start(scripts, *, trust="readonly", caps=None, timeout_s=60, allow=()):
        ws = tmp_path / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        # 策略 + 预授权写进 settings 枢纽块（resolve_config 会读回并保留）。
        sp = home / "settings.json"
        settings = json.loads(sp.read_text()) if sp.exists() else {}
        hub = settings.setdefault("connectors", {}).setdefault("a2a", {})
        hub["trust"] = trust
        hub["caps"] = list(caps) if caps is not None else ["L0"]
        hub["inbound_task_timeout_s"] = timeout_s
        if allow:
            settings.setdefault("permissions", {})["allow"] = list(allow)
        sp.write_text(json.dumps(settings), encoding="utf-8")
        sf = tmp_path / "scripts.json"
        sf.write_text(json.dumps(scripts), encoding="utf-8")
        monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(sf))
        args = node.build_parser().parse_args(
            ["--root", str(ws), "--listen", "127.0.0.1:0", "--home", str(home)]
        )
        cfg = node.resolve_config(args)
        assert cfg.port == 0  # 随机端口纪律
        handle = node.ensure_node_listener(cfg)
        await handle.start()
        started.append(handle)
        endpoint = f"http://127.0.0.1:{handle.actual_port}/a2a/{handle.token}"
        return handle, endpoint, ws

    yield _start

    for handle in started:
        deps = handle._deps
        for bg in list(deps.background):
            bg.cancel()
        if deps.executor is not None:
            await deps.executor.aclose()
        await handle.stop()
    inbound._HANDLES.clear()


async def _poll(client: A2AClient, tid: str, *, timeout: float) -> Task:
    """轮询 tasks/get 至终态/挂起。0.5s 间隔 + 429 退避（入站令牌桶 30/min，
     aggressive 轮询会耗尽快照——退避后重试，不裸断言一次）。"""
    from ginno_runtime.a2a.errors import A2AError

    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        try:
            t = await client.get_task(tid)
        except A2AError as exc:
            if "429" in str(exc):
                await asyncio.sleep(1.5)  # 令牌桶退避
                continue
            raise
        if t.is_terminal or t.state == TaskState.INPUT_REQUIRED:
            return t
        await asyncio.sleep(0.5)
    raise AssertionError(f"task {tid} did not settle within {timeout}s")


async def _drive(endpoint: str, text: str, *, timeout: float = 45.0) -> Task:
    """真 A2AClient 派活 → 轮询 tasks/get 至终态/挂起。断言非 submitted 占位。"""
    client = A2AClient(endpoint, timeout_s=timeout)
    try:
        task = await client.send_message(text)
        assert isinstance(task, Task)
        # 真执行：message/send 立即回 working（绝不 submitted 占位）。
        assert task.state == TaskState.WORKING, f"expected working, got {task.state}"
        return await _poll(client, task.id, timeout=timeout)
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# 自环：派活 → 真执行 → 终态 + 回报内容（验收 #3 核心）
# --------------------------------------------------------------------------- #
async def test_loopback_dispatch_executes_and_reports(node_factory):
    handle, endpoint, ws = await node_factory(
        [{"content": "LOOPBACK-REPORT-OK: the node really executed this task."}]
    )
    print(f"\n[self-loop] node listening: {endpoint}")
    task = await _drive(endpoint, "Please run this task and report back.")
    print(f"[self-loop] task={task.id} state={task.state.value}")
    print(f"[self-loop] report={task.result_text()!r}")
    print(f"[self-loop] session_id={task.metadata.get('session_id')}")
    assert task.state == TaskState.COMPLETED
    assert "LOOPBACK-REPORT-OK" in task.result_text()  # 回报内容来自真 turn 链
    assert task.metadata.get("session_id")  # 真建了入站会话
    # 会话隔离实证：type=a2a / 独立 slug（#17），落在 node home。
    from ginno_runtime import paths
    from ginno_runtime.session_meta import _find_meta

    meta, slug = _find_meta(task.metadata["session_id"])
    assert meta["type"] == "a2a" and slug == "a2a-inbound"
    assert handle.running  # server 仍在跑（任务完成不拖垮 listener）


async def test_loopback_followup_reuses_context_new_task(node_factory):
    handle, endpoint, ws = await node_factory(
        [{"content": "answer-1"}, {"content": "answer-2"}]
    )
    client = A2AClient(endpoint, timeout_s=45)
    try:
        t1 = await client.send_message("first question")
        assert t1.state == TaskState.WORKING
        t1 = await _poll(client, t1.id, timeout=45)
        assert t1.state == TaskState.COMPLETED
        cid = t1.context_id
        # 追问：同 contextId → 新 taskId（终态不可重启，§8.1 / #15）。
        t2 = await client.send_message("follow-up", context_id=cid)
        assert t2.id != t1.id
        assert t2.context_id == cid
        t2 = await _poll(client, t2.id, timeout=45)
        assert t2.state == TaskState.COMPLETED
        # 同 context 复用同一入站会话（context 级映射）。
        assert t2.metadata.get("session_id") == t1.metadata.get("session_id")
    finally:
        await client.aclose()


# --------------------------------------------------------------------------- #
# trust=readonly 写操作被拒（#8 / #31，执行侧强制实证）
# --------------------------------------------------------------------------- #
async def test_loopback_readonly_write_rejected(node_factory, tmp_path):
    ws = tmp_path / "ws"  # exec_root（node_factory 会 mkdir）
    handle, endpoint, ws = await node_factory(
        [
            {"content": "", "tool_calls": [{"name": "write_file",
                                            "args": {"path": str(ws / "hack.txt"), "content": "pwned"}}]},
            {"content": "I cannot write in readonly mode."},
        ],
        trust="readonly", caps=["L0", "L1", "L2", "L3"],
    )
    task = await _drive(endpoint, "Write a file called hack.txt")
    print(f"\n[self-loop] readonly task state={task.state.value} denials={task.metadata.get('guard_denials')}")
    assert task.state == TaskState.COMPLETED
    codes = [d["code"] for d in task.metadata.get("guard_denials", [])]
    assert "readonly_write_forbidden" in codes  # guard 执行侧拒绝
    assert not (ws / "hack.txt").exists()  # 写工具真没执行


# --------------------------------------------------------------------------- #
# exec_root 越界被拒（#32，fail-loud 实证）
# --------------------------------------------------------------------------- #
async def test_loopback_exec_root_violation_rejected(node_factory, tmp_path):
    outside = tmp_path / "outside.txt"  # exec_root（tmp_path/ws）之外
    handle, endpoint, ws = await node_factory(
        [
            {"content": "", "tool_calls": [{"name": "write_file",
                                            "args": {"path": str(outside), "content": "escape"}}]},
            {"content": "That path is outside my workspace."},
        ],
        trust="normal", caps=["L0", "L1", "L2", "L3"], allow=["write_file"],
    )
    task = await _drive(endpoint, "Write outside the workspace")
    print(f"\n[self-loop] exec_root task state={task.state.value} denials={task.metadata.get('guard_denials')}")
    assert task.state == TaskState.COMPLETED
    codes = [d["code"] for d in task.metadata.get("guard_denials", [])]
    assert "exec_root_violation" in codes  # 越界 fail-loud 拒绝
    assert not outside.exists()  # 绝不静默修正/放行


# --------------------------------------------------------------------------- #
# 超时 killpg（#40）：任务转 failed 不悬空 working，worker 进程组被清理
# --------------------------------------------------------------------------- #
async def test_loopback_timeout_kills_worker(node_factory):
    handle, endpoint, ws = await node_factory(
        [
            {"content": "", "tool_calls": [{"name": "bash", "args": {"command": "sleep 60"}}]},
            {"content": "done"},
        ],
        trust="normal", caps=["L0", "L1", "L2", "L3"], allow=["bash"], timeout_s=8,
    )
    task = await _drive(endpoint, "Run a long sleep", timeout=40)
    print(f"\n[self-loop] timeout task state={task.state.value} error={task.status.message.text() if task.status.message else None!r}")
    assert task.state == TaskState.FAILED  # 超时转终态，不悬空 working
    body = task.status.message.text() if task.status.message else ""
    assert "timed out" in body and "killpg" in body
    # 收尾后无活 worker（executor.aclose 在 fixture 兜底；这里断言监督器已清理）。
    deps = handle._deps
    assert not deps.executor._live  # 超时 worker 已被 killpg 回收
