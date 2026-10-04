"""delegate_agent 全链路接线（异步版）：WS turn → 回执 → 后台执行 → 子会话
回填 → 结果注入父会话 → history/级联 → 只读防护。

fake Popen 提供罐装 claude stream-json；CLI 桩注入 base._CLI_CACHE。
异步语义：工具立即返回回执（delegation=<id> started），后台跑完经
subagent 注入通道回流（前端 🧭 卡同款）。
"""
import json
import subprocess
import time as _time

import pytest

from ginno_runtime.testing.fake_model import script, script_tool_call
from ginno_runtime.tools.external_agents import base as _xa_base

pytestmark = pytest.mark.api

CLAUDE_STREAM = "\n".join([
    '{"type":"system","subtype":"init","session_id":"c1","model":"claude-x"}',
    '{"type":"assistant","message":{"role":"assistant","content":[{"type":"text","text":"will read"},{"type":"tool_use","id":"u1","name":"Read","input":{"path":"a.txt"}}]}}',
    '{"type":"user","message":{"role":"user","content":[{"type":"tool_result","tool_use_id":"u1","content":"hello","is_error":false}]}}',
    '{"type":"result","subtype":"success","result":"ok","is_error":false,"usage":{"input_tokens":10,"output_tokens":2},"session_id":"c1","num_turns":1,"duration_ms":50,"cost_usd":0.001}',
])


@pytest.fixture(autouse=True)
def _ext_and_cli_stubs(monkeypatch):
    monkeypatch.setenv("GINNO_EXTERNAL_AGENTS", "1")
    for n in ("claude", "codex", "pi"):
        _xa_base._CLI_CACHE[n] = "/opt/claude"
    yield


def _fake_popen(monkeypatch, stdout=CLAUDE_STREAM, rc=0):
    class _P:
        def __init__(self, argv, **kw):
            self.argv = list(argv)
            self.pid = 4242
            self.returncode = rc

        def communicate(self, timeout=None):
            return stdout, ""

        def poll(self):
            return self.returncode

        def wait(self, timeout=None):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", _P)


def _bypass_on():
    from ginno_runtime import paths

    sp = paths.settings_path()
    s = json.loads(sp.read_text() or "{}") if sp.exists() else {}
    s["bypass_permissions"] = True
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(s))


def _delegation_child(client, parent_sid):
    rows = client.get("/api/sessions").json()
    return next(
        (r for r in rows if r.get("type") == "delegation" and r.get("parent_session_id") == parent_sid),
        None,
    )


def _wait_terminal(client, parent_sid, secs=15):
    """轮询直到委托子会话脱离 running（bg 任务在 TestClient 的后台事件循环跑）。"""
    deadline = _time.time() + secs
    child = None
    while _time.time() < deadline:
        child = _delegation_child(client, parent_sid)
        if child and child.get("stop_reason") not in ("running", None):
            return child
        _time.sleep(0.2)
    return child


def _run_delegation_turn(client, create_session, ws_conv, monkeypatch, mode="read-only", stdout=CLAUDE_STREAM, rc=0):
    _bypass_on()
    _fake_popen(monkeypatch, stdout=stdout, rc=rc)
    model = [
        script(tool_calls=[script_tool_call("delegate_agent", {"backend": "claude-code", "prompt": "inspect a.txt", "mode": mode})]),
        script(text="done"),
        script(text="retold"),  # 注入唤醒的第三轮
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("delegate please")
        events = conv.recv_until("message.end", "error")
    assert "message.end" in [e.get("event") for e in events]
    return sid


def test_delegate_async_end_to_end(client, create_session, ws_conv, monkeypatch):
    sid = _run_delegation_turn(client, create_session, ws_conv, monkeypatch)
    # 回执立即回流（含 delegation= 溯源）
    hist0 = json.dumps(client.get(f"/api/sessions/{sid}/history").json(), ensure_ascii=False)
    assert "delegation=" in hist0 and "started" in hist0

    child = _wait_terminal(client, sid)
    assert child, "delegation child missing"
    assert child["backend"] == "claude-code" and child["mode"] == "read-only"
    assert child["stop_reason"] == "success"
    did = child["id"]

    # 后台完成 → 结果注入父会话（stop=success 的结果串）
    deadline = _time.time() + 15
    injected = ""
    while _time.time() < deadline:
        flat = json.dumps(client.get(f"/api/sessions/{sid}/history").json(), ensure_ascii=False)
        if "stop=success" in flat:
            injected = flat
            break
        _time.sleep(0.3)
    assert injected and did in injected, "injected result missing"
    # 注入必须走 <ginno_subagent_result> 信封（前端折成结果卡的契约）；
    # 机器头 [delegate …] 留在正文里——它是模型侧的溯源头（设计 §7）。
    assert "ginno_subagent_result" in injected and "stop=success" in injected

    # 子会话回放：转换后的 claude 事件以原生块渲染
    dhist = client.get(f"/api/sessions/{did}/history").json()
    blocks = [b for m in dhist["messages"] for b in m.get("blocks", [])]
    kinds_b = [b.get("kind") for b in blocks]
    assert "tool" in kinds_b and any("will read" in (b.get("text") or "") for b in blocks)

    # 用量 external 行 + 检测端点
    req = client.get(f"/api/usage/requests?session_id={sid}").json()
    assert any(r.get("source") == "external" for r in req["rows"])
    ag = client.get("/api/external-agents").json()
    assert {a["name"] for a in ag} == {"claude-code", "codex", "pi"}

    # 级联删除
    assert client.delete(f"/api/sessions/{sid}").json()["ok"] is True
    assert all(r.get("id") != did for r in client.get("/api/sessions").json())


def test_delegation_session_ws_silent(client, create_session, ws_conv, monkeypatch):
    """回放会话 WS：连接被接受且保持打开、消息被静默吞掉——不发 error
    （那是重连风暴的燃料）、不产生 turn。"""
    sid = _run_delegation_turn(client, create_session, ws_conv, monkeypatch)
    child = _wait_terminal(client, sid)
    assert child
    with ws_conv(child["id"]) as conv2:
        conv2.invoke("hello?")  # 应被吞掉：无事件、无 turn
        _time.sleep(1.0)
    # 没有为回放会话开过 turn（checkpoint 不新增消息）
    hist = client.get(f"/api/sessions/{child['id']}/history").json()
    flat = json.dumps(hist, ensure_ascii=False)
    assert "hello?" not in flat and "read-only" not in flat
    assert len(hist["messages"]) >= 1  # 回放内容完好


def test_delegation_error_run_still_archives(client, create_session, ws_conv, monkeypatch):
    _bypass_on()
    _fake_popen(monkeypatch, stdout="garbage not json", rc=1)
    model = [
        script(tool_calls=[script_tool_call("delegate_agent", {"backend": "claude-code", "prompt": "p", "mode": "edit"})]),
        script(text="noted"),
        script(text="retold"),
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("go")
        conv.recv_until("message.end", "error")
    child = _wait_terminal(client, sid)
    assert child and child["stop_reason"] == "error"
    dhist = json.dumps(client.get(f"/api/sessions/{child['id']}/history").json(), ensure_ascii=False)
    assert "rc=1" in dhist
