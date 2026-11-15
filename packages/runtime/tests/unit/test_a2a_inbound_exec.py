"""Unit tests for a2a.inbound_exec（N2b 入站执行层）。

真执行链（create_session → _run_stream，ws=None）跑在 GINNO_FAKE_LLM 脚本缝上
（零网络）；被测对象是执行层本身，**不 mock**。覆盖：
- 会话映射（#17 type=a2a/独立 slug/不挂用户目录）+ 隔离（#18 读不到用户会话）
- guard 接线（#31 readonly 写拒 / caps 越级 / #32 exec_root 越界 / #33 full 拒改配置）
- 权限 fail-closed（#7 guard 放行但策略 ask → 挂起 input-required，无法自批）
- 状态机（ExecResult 映射 + server working→终态 + 终态不可重启）
- 超时 killpg（#40 SIGTERM→2s→SIGKILL，孤儿不留存）
- 产物 Artifact（§2.5 路径+元信息，不传内容）

路径隔离靠根 conftest isolated_home（GINNO_HOME→tmp_path）；exec_root 恒指 tmp。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime.a2a import auth, inbound_exec, server
from ginno_runtime.a2a.models import Artifact, Part, TaskState

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _home_subdir(tmp_path, monkeypatch):
    """exec_root（tmp_path/ws）必须在 GINNO_HOME 之外。

    context_folders 对 home 内目录强制只读（runtime-home 保护）——若 ws 落在 home
    里会挡住入站写产物。生产 exec_root=~/ginno-a2a-workspace 本就在 ~/.ginno 之外。
    这里把 home 钉到 tmp_path/home（覆盖根 isolated_home 的 tmp_path），ws 为其兄弟。
    """
    monkeypatch.setenv("GINNO_HOME", str(tmp_path / "home"))


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _policy(tmp_path: Path, **over) -> dict:
    base = {
        "trust": "readonly",
        "caps": ["L0"],
        "exec_root": str(tmp_path / "ws"),
        "slug": "a2a-inbound",
        "title_prefix": "A2A · ",
        "timeout_s": 30,
    }
    base.update(over)
    return base


async def _run_job(tmp_path, monkeypatch, scripts, policy, *, instruction="do the task",
                   context_id="ctx-1", session_id=None, peer_name="tester"):
    """在进程内跑真执行链（fake LLM 脚本缝）。返回 result dict。"""
    scripts_file = tmp_path / "scripts.json"
    scripts_file.write_text(json.dumps(scripts), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts_file))
    job = {
        "instruction": instruction,
        "context_id": context_id,
        "session_id": session_id or "",
        "peer_name": peer_name,
        "policy": policy,
    }
    return await inbound_exec._run_job(job)


def _denial_codes(result: dict) -> list[str]:
    return [d["code"] for d in result.get("denials", [])]


# --------------------------------------------------------------------------- #
# 会话映射（#17）+ 隔离（#18）
# --------------------------------------------------------------------------- #
async def test_session_mapping_type_slug_no_user_folders(tmp_path, monkeypatch):
    from ginno_runtime import context_folders as cf
    from ginno_runtime.session_meta import _find_meta

    result = await _run_job(tmp_path, monkeypatch, [{"content": "done"}], _policy(tmp_path))
    assert result["state"] == "completed"
    sid = result["session_id"]
    assert sid
    meta, slug = _find_meta(sid)
    assert meta["type"] == "a2a"
    assert meta["channel"] == "a2a"
    assert slug == "a2a-inbound"  # 独立 slug，与用户 default 隔离
    assert meta["title"].startswith("A2A · ")
    # 只挂 exec_root 专用工作区，不挂任何用户目录（#17）。
    folders = meta["context_folders"]
    assert len(folders) == 1
    primary = meta["primary_folder"]
    assert primary in folders
    assert cf.get_folder(primary)["path"] == str(tmp_path / "ws")


async def test_inbound_cannot_read_user_session_history(tmp_path, monkeypatch):
    # 用户其他会话历史落在 home/projects/default/sessions —— 在 exec_root 之外。
    # 入站 read_file 指向它 → guard exec_root 越界拒绝（#18 数据最小化）。
    secret = paths.home() / "projects" / "default" / "sessions" / "user-session.jsonl"
    secret.parent.mkdir(parents=True, exist_ok=True)
    secret.write_text('{"role":"user","text":"USER PRIVATE HISTORY"}')
    scripts = [
        {"content": "", "tool_calls": [{"name": "read_file", "args": {"path": str(secret)}}]},
        {"content": "I cannot read that."},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts,
        _policy(tmp_path, trust="normal", caps=["L0", "L1", "L2", "L3"]),
    )
    assert "exec_root_violation" in _denial_codes(result)
    assert "USER PRIVATE HISTORY" not in result["text"]


async def test_followup_reuses_session_via_context_map(tmp_path, monkeypatch):
    # 同 contextId 追问 → 复用同一 session_id（#15 / §8.1 context 级映射）。
    pol = _policy(tmp_path)
    r1 = await _run_job(tmp_path, monkeypatch, [{"content": "first"}], pol, context_id="ctx-A")
    r2 = await _run_job(tmp_path, monkeypatch, [{"content": "second"}], pol,
                        context_id="ctx-A", session_id=None)
    assert r1["session_id"] == r2["session_id"]
    mapping = json.loads((paths.a2a_dir() / "inbound-contexts.json").read_text())
    assert mapping["ctx-A"] == r1["session_id"]


async def test_deleted_session_self_heals(tmp_path, monkeypatch):
    from ginno_runtime.api.sessions import delete_session

    pol = _policy(tmp_path)
    r1 = await _run_job(tmp_path, monkeypatch, [{"content": "first"}], pol, context_id="ctx-B")
    sid = r1["session_id"]
    await delete_session(sid)  # 用户删了入站会话
    # 远端再派活（映射仍指旧 id）→ 自愈重开新会话（#19）。
    r2 = await _run_job(tmp_path, monkeypatch, [{"content": "again"}], pol, context_id="ctx-B")
    assert r2["state"] == "completed"
    assert r2["session_id"] != sid


# --------------------------------------------------------------------------- #
# guard 接线（#31 / #32 / #33 / #8）
# --------------------------------------------------------------------------- #
async def test_readonly_write_rejected(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    target = ws / "x.txt"
    scripts = [
        {"content": "", "tool_calls": [{"name": "write_file", "args": {"path": str(target), "content": "hi"}}]},
        {"content": "cannot write in readonly"},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts,
        _policy(tmp_path, trust="readonly", caps=["L0", "L1", "L2", "L3"]),
    )
    assert "readonly_write_forbidden" in _denial_codes(result)
    assert not target.exists()  # 工具真没执行


async def test_caps_escalation_rejected(tmp_path, monkeypatch):
    # trust=normal 但 caps 只到 L0 → write_file 需 L2 → 越级拒绝。
    target = tmp_path / "ws" / "y.txt"
    scripts = [
        {"content": "", "tool_calls": [{"name": "write_file", "args": {"path": str(target), "content": "hi"}}]},
        {"content": "no capability"},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts, _policy(tmp_path, trust="normal", caps=["L0"]),
    )
    assert "capability_not_granted" in _denial_codes(result)
    assert not target.exists()


async def test_exec_root_violation_rejected_fail_loud(tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"  # exec_root 之外
    scripts = [
        {"content": "", "tool_calls": [{"name": "write_file", "args": {"path": str(outside), "content": "escape"}}]},
        {"content": "blocked"},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts,
        _policy(tmp_path, trust="normal", caps=["L0", "L1", "L2", "L3"]),
    )
    assert "exec_root_violation" in _denial_codes(result)
    assert not outside.exists()  # 越界绝不静默修正/放行（#32）


async def test_full_trust_still_rejects_config_write(tmp_path, monkeypatch):
    # #33：full ≠ 可改配置。配置写工具任何 trust 都拒。
    scripts = [
        {"content": "", "tool_calls": [{"name": "update_settings", "args": {"key": "x", "value": "y"}}]},
        {"content": "config write forbidden"},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts,
        _policy(tmp_path, trust="full", caps=["L0", "L1", "L2", "L3"]),
    )
    assert "config_write_forbidden" in _denial_codes(result)


async def test_readonly_shell_metachar_rejected(tmp_path, monkeypatch):
    # readonly 下 `ls; rm` 链式注入 → 元字符纵深防御拒绝（#34 诚实警告）。
    scripts = [
        {"content": "", "tool_calls": [{"name": "bash", "args": {"command": "ls; rm x"}}]},
        {"content": "blocked"},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts,
        _policy(tmp_path, trust="readonly", caps=["L0", "L1"]),
    )
    assert "readonly_shell_forbidden" in _denial_codes(result)


# --------------------------------------------------------------------------- #
# 权限 fail-closed（#7）：guard 放行但策略 ask → 挂起转人工，无法自批
# --------------------------------------------------------------------------- #
async def test_permission_ask_parks_input_required(tmp_path, monkeypatch):
    # normal + L2 + exec_root 内 → guard 放行；但 write_file 未预授权 → 策略 ask
    # → interrupt 挂起 → input-required（等本机人工批准，远端无自批路径）。
    target = tmp_path / "ws" / "needs_approval.txt"
    scripts = [
        {"content": "", "tool_calls": [{"name": "write_file", "args": {"path": str(target), "content": "hi"}}]},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts,
        _policy(tmp_path, trust="normal", caps=["L0", "L1", "L2", "L3"]),
    )
    assert result["parked"] is True
    assert result["state"] == "input-required"
    assert not target.exists()  # 未批准 → 绝不执行


# --------------------------------------------------------------------------- #
# 产物 Artifact（§2.5：路径 + 元信息，不传内容）
# --------------------------------------------------------------------------- #
async def test_artifact_returns_path_metadata_not_content(tmp_path, monkeypatch):
    # 预授权 write_file（操作员授予）→ guard 放行 + 策略 allow → 真写 → 产物回报。
    sp = paths.settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps({"permissions": {"allow": ["write_file", "read_file"]}}))
    ws = tmp_path / "ws"
    target = ws / "report.md"
    secret_content = "TOP-SECRET-FILE-BODY-DO-NOT-TRANSFER"
    scripts = [
        {"content": "", "tool_calls": [{"name": "write_file", "args": {"path": str(target), "content": secret_content}}]},
        {"content": "created report.md"},
    ]
    result = await _run_job(
        tmp_path, monkeypatch, scripts,
        _policy(tmp_path, trust="normal", caps=["L0", "L1", "L2", "L3"]),
    )
    assert result["state"] == "completed"
    assert target.exists()  # 产物留在 exec_root
    assert target.read_text() == secret_content
    arts = result["artifacts"]
    assert len(arts) == 1
    a = arts[0]
    assert a["name"] == "report.md"
    assert a["metadata"]["path"] == str(target)
    assert a["metadata"]["size_bytes"] == len(secret_content)
    assert a["metadata"]["exec_root"] == str(ws)
    # 回报只含路径+元信息，绝不回传文件内容（§2.5）。
    blob = json.dumps(a, ensure_ascii=False)
    assert secret_content not in blob
    assert str(target) in blob


# --------------------------------------------------------------------------- #
# 状态机：ExecResult 映射 + 终态判定
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("state", ["completed", "failed", "input-required", "canceled", "rejected"])
def test_exec_result_state_mapping(state):
    r = inbound_exec.ExecResult.from_dict({"state": state, "text": "t"})
    assert r.state == TaskState.parse(state)


def test_exec_result_artifacts_roundtrip():
    raw = {
        "state": "completed",
        "artifacts": [{"artifactId": "a1", "name": "f.txt", "parts": [{"kind": "text", "text": "path: /x"}],
                       "metadata": {"path": "/x", "size_bytes": 3}}],
        "denials": [{"tool": "write_file", "code": "exec_root_violation", "reason": "r"}],
    }
    r = inbound_exec.ExecResult.from_dict(raw)
    assert isinstance(r.artifacts[0], Artifact)
    assert r.artifacts[0].metadata["path"] == "/x"
    assert r.denials[0]["code"] == "exec_root_violation"


# --------------------------------------------------------------------------- #
# server 接线状态机（桩执行器——被测对象是 server，执行器是依赖）
# --------------------------------------------------------------------------- #
class _StubExecutor:
    """确定性执行器桩：仅测 server 的 Task 状态 plumbing（执行层本身另有真测）。"""

    def __init__(self, result: inbound_exec.ExecResult):
        self.result = result
        self.calls: list[dict] = []

    async def run(self, **kw):
        self.calls.append(kw)
        return self.result


@pytest.fixture()
def stub_deps(tmp_path):
    return server.A2ADeps(
        tokens=auth.TokenStore(tmp_path / "tokens.json"),
        audit=server.AuditLog(tmp_path / "audit.jsonl"),
        limiter=server.TokenBucketLimiter(1000),
    )


async def _wait_terminal(deps, task_id, timeout=5.0):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        t = deps.tasks.get(task_id)
        if t.is_terminal or t.state == TaskState.INPUT_REQUIRED:
            return t
        await asyncio.sleep(0.02)
    raise AssertionError("task did not settle")


async def test_server_send_runs_to_terminal_with_artifacts(stub_deps):
    art = Artifact(artifact_id="a1", name="out.txt", parts=[Part(text="path: /ws/out.txt")],
                   metadata={"path": "/ws/out.txt", "size_bytes": 5})
    stub = _StubExecutor(inbound_exec.ExecResult(
        state=TaskState.COMPLETED, text="done", session_id="sid-1", artifacts=[art],
        denials=[{"tool": "bash", "code": "readonly_shell_forbidden", "reason": "r"}],
    ))
    stub_deps.executor = stub
    app = server.build_a2a_app(stub_deps)
    token = stub_deps.tokens.issue()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/a2a/{token}/", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "go"}]}},
        })
        task = resp.json()["result"]
        assert task["status"]["state"] == "working"  # 不再 submitted 占位
        settled = await _wait_terminal(stub_deps, task["id"])
        assert settled.state == TaskState.COMPLETED
        assert settled.artifacts[0].metadata["path"] == "/ws/out.txt"
        assert settled.metadata["session_id"] == "sid-1"
        assert settled.metadata["guard_denials"][0]["code"] == "readonly_shell_forbidden"
        assert stub.calls[0]["instruction"] == "go"


async def test_server_terminal_task_not_restartable(stub_deps):
    stub = _StubExecutor(inbound_exec.ExecResult(state=TaskState.COMPLETED, text="done"))
    stub_deps.executor = stub
    app = server.build_a2a_app(stub_deps)
    token = stub_deps.tokens.issue()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/a2a/{token}/", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "go"}]}},
        })
        tid = resp.json()["result"]["id"]
        await _wait_terminal(stub_deps, tid)
        # 终态不可取消（→ 不可重启）；追问须开新 task（新 id）。
        cancel = await c.post(f"/a2a/{token}/tasks/cancel", json={
            "jsonrpc": "2.0", "id": 2, "method": "tasks/cancel", "params": {"id": tid},
        })
        assert cancel.json()["error"]["code"] == -32002  # TaskNotCancelable
        again = await c.post(f"/a2a/{token}/", json={
            "jsonrpc": "2.0", "id": 3, "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "follow"}],
                                  "contextId": resp.json()["result"]["contextId"]}},
        })
        assert again.json()["result"]["id"] != tid  # 新 task，非重启旧 task


async def test_server_inbound_disabled_keeps_placeholder(stub_deps):
    # 门控关闭（无 executor、无 inbound_enabled）→ P0 submitted 占位（#14 默认关）。
    app = server.build_a2a_app(stub_deps)
    token = stub_deps.tokens.issue()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        resp = await c.post(f"/a2a/{token}/", json={
            "jsonrpc": "2.0", "id": 1, "method": "message/send",
            "params": {"message": {"role": "user", "parts": [{"kind": "text", "text": "go"}]}},
        })
        assert resp.json()["result"]["status"]["state"] == "submitted"


# --------------------------------------------------------------------------- #
# 超时 killpg（#40）：SIGTERM→2s→SIGKILL，孤儿进程不留存
# --------------------------------------------------------------------------- #
async def test_terminate_group_kills_parent_and_children(tmp_path):
    pidfile = tmp_path / "child.pid"
    code = (
        "import subprocess, time\n"
        f"p = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(60)\n"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code, start_new_session=True,
    )
    try:
        for _ in range(200):
            if pidfile.exists():
                break
            await asyncio.sleep(0.02)
        assert pidfile.exists(), "child never started"
        child_pid = int(pidfile.read_text())
        os.kill(child_pid, 0)  # 子进程活着
        await inbound_exec._terminate_group(proc)
        assert proc.returncode is not None  # 父进程已收
        await asyncio.sleep(0.2)
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)  # 孙进程也被 killpg 清理 → 无孤儿
    finally:
        if proc.returncode is None:
            await inbound_exec._terminate_group(proc)


async def test_terminate_group_idempotent_on_dead_process(tmp_path):
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", "pass", start_new_session=True,
    )
    await proc.wait()
    await inbound_exec._terminate_group(proc)  # 已退 → 静默幂等，不抛


# ── 权限放行面按 trust 派生（2026-11-16 修 headless 死锁）──────────────────
#
# 原实现不论 trust 一律只放行 3 个读工具，写/shell 一律 ask。node 无 UI 且
# A2A input-required 远程批准闭环未实现 → trust=normal 的节点连
# `pwd && ls -la` 都挂起等一个永远不来的人工批准（远端 VPS 实测卡死）。
# 现按 trust 派生；guard 仍在每次工具调用前强制 trust×caps×exec_root。


def _read_perms() -> dict:
    import json as _json

    from ginno_runtime import paths

    return _json.loads(paths.settings_path().read_text())


def test_readonly_only_allows_read_tools():
    """readonly 仍 fail-closed：写/shell 不得进 allow。"""
    from ginno_runtime.a2a.inbound_exec import ExecPolicy, _write_inbound_settings

    _write_inbound_settings(ExecPolicy(trust="readonly"))
    cfg = _read_perms()
    allow = set(cfg["permissions"]["allow"])
    assert {"read_file", "glob_files", "grep_files"} <= allow
    assert not ({"write_file", "bash", "edit_file", "repl"} & allow)
    assert cfg["bypass_permissions"] is False


def test_normal_allows_write_and_shell():
    """trust=normal 必须能在 exec_root 内真干活（否则 headless 死锁）。"""
    from ginno_runtime.a2a.inbound_exec import ExecPolicy, _write_inbound_settings

    _write_inbound_settings(ExecPolicy(trust="normal", caps=["L0", "L1", "L2", "L3"]))
    cfg = _read_perms()
    allow = set(cfg["permissions"]["allow"])
    assert {"read_file", "write_file", "edit_file", "bash"} <= allow
    # 绝不 bypass——guard 才是权威闸
    assert cfg["bypass_permissions"] is False


def test_full_allows_write_but_guard_still_rejects_config():
    """full 放行写/shell，但 guard 对配置写恒拒（#33 不受权限层影响）。"""
    from ginno_runtime.a2a import guard as g
    from ginno_runtime.a2a.inbound_exec import ExecPolicy, _write_inbound_settings

    _write_inbound_settings(ExecPolicy(trust="full", caps=["L0", "L1", "L2", "L3"]))
    allow = set(_read_perms()["permissions"]["allow"])
    assert "write_file" in allow and "bash" in allow
    d = g.check_tool("full", ["L0", "L1", "L2", "L3"], "update_config")
    assert d.allowed is False


def test_existing_deny_and_ask_are_respected():
    """操作员显式收紧优先：已在 deny/ask 的工具不得被自动加进 allow。"""
    import json as _json

    from ginno_runtime import paths
    from ginno_runtime.a2a.inbound_exec import ExecPolicy, _write_inbound_settings

    sp = paths.settings_path()
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(_json.dumps({"permissions": {"deny": ["bash"], "ask": ["write_file"]}}))
    _write_inbound_settings(ExecPolicy(trust="normal", caps=["L0", "L1", "L2", "L3"]))
    allow = set(_read_perms()["permissions"]["allow"])
    assert "bash" not in allow, "已在 deny 的 bash 不得被自动放行"
    assert "write_file" not in allow, "已在 ask 的 write_file 不得被自动放行"


def test_guard_still_blocks_readonly_write_even_if_permission_allows():
    """纵深防御：即便权限层误放行，guard 仍拒 readonly 写。"""
    from ginno_runtime.a2a import guard as g

    d = g.check_tool(
        "readonly", ["L0", "L1", "L2", "L3"], "write_file",
        exec_root="/tmp/ws", target_path="/tmp/ws/x.txt",
    )
    assert d.allowed is False
    assert d.code == "readonly_write_forbidden"


# ── _sniff_kind 多字节截断（2026-11-16 修中文产物误判 binary）──────────────
#
# 事故：远端 node 写出的中文 markdown 报告被标成 "binary"。根因是读首 256
# 字节后裸 head.decode()，截断点落在汉字中间（0xe8 是 3 字节序列首字节）
# → UnicodeDecodeError "unexpected end of data" → 误判。全文 decode 正常。


def test_sniff_kind_chinese_text_truncated_mid_char(tmp_path):
    """截断点落在多字节字符中间仍须判 text（真实事故场景）。"""
    from ginno_runtime.a2a.inbound_exec import _SNIFF_BYTES, _sniff_kind

    # 确定性构造：ASCII 前缀填到 _SNIFF_BYTES-1，再放一个 3 字节汉字，
    # 于是第 _SNIFF_BYTES 字节正好是该汉字首字节 → 裸 decode 必炸。
    p = tmp_path / "report.md"
    prefix = "a" * (_SNIFF_BYTES - 1)
    p.write_text(prefix + "结论：全部实测执行。" * 20, encoding="utf-8")
    head = p.read_bytes()[:_SNIFF_BYTES]
    try:
        head.decode("utf-8")
        truncated = False
    except UnicodeDecodeError:
        truncated = True
    assert truncated, "用例构造失败：截断处未落在多字节字符中间，抓不到原 bug"
    assert _sniff_kind(p) == "text"


def test_sniff_kind_emoji_truncated(tmp_path):
    """emoji（4 字节）截断同样判 text。"""
    from ginno_runtime.a2a.inbound_exec import _sniff_kind

    p = tmp_path / "emoji.md"
    p.write_text("# 标题\n" + "🎉" * 200, encoding="utf-8")
    assert _sniff_kind(p) == "text"


def test_sniff_kind_real_binary_still_rejected(tmp_path):
    """真二进制不得被放宽：含 NUL 判 binary。"""
    from ginno_runtime.a2a.inbound_exec import _sniff_kind

    p = tmp_path / "b.bin"
    p.write_bytes(bytes(range(256)) * 2)
    assert _sniff_kind(p) == "binary"


def test_sniff_kind_invalid_utf8_still_rejected(tmp_path):
    """中间出现非法 UTF-8 字节仍判 binary（增量解码不放宽这个）。"""
    from ginno_runtime.a2a.inbound_exec import _sniff_kind

    p = tmp_path / "bad.bin"
    p.write_bytes(b"\xff\xfe\xfd\xfc" * 80)
    assert _sniff_kind(p) == "binary"


def test_sniff_kind_ascii_and_missing_file(tmp_path):
    """纯 ASCII 判 text；不存在的路径 fail-safe 判 binary 不抛。"""
    from ginno_runtime.a2a.inbound_exec import _sniff_kind

    p = tmp_path / "a.txt"
    p.write_text("hello world\n" * 30)
    assert _sniff_kind(p) == "text"
    assert _sniff_kind(tmp_path / "nope.md") == "binary"
