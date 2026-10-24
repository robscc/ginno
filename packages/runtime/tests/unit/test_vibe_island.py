"""Vibe Island 连接器的事件映射契约(vibe_island.py,指南 custom-agents.md)。

只测纯逻辑:Ginno 事件 → VI 事件的映射、子会话过滤、cwd 解析、开关闸门。
不 spawn bridge(那属于 Step 6 的真机验证),_send 一律被 monkeypatch 捕获。
"""

from __future__ import annotations

import pytest

from ginno_runtime import vibe_island as vi
from ginno_runtime.server_shared import _SESSIONS

# 绑定原始函数对象:autouse fixture 会把模块属性 vi._send 换成捕获器,
# 而这里拿到的是补丁前的真身(测 _send 自己的前置闸门用)。
from ginno_runtime.vibe_island import _send as _real_send

pytestmark = pytest.mark.unit

SID = "sess-vi-1"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """每个用例从干净的模块状态开始:开关关、缓存空、_send 被捕获。"""
    sent: list[dict] = []
    monkeypatch.setattr(vi, "_enabled", None)
    monkeypatch.setattr(vi, "_cwds", {})
    monkeypatch.setattr(vi, "_tool_names", {})
    monkeypatch.setattr(vi, "_send", sent.append)
    monkeypatch.setattr(vi, "report_status", lambda force=False: None)
    _SESSIONS.pop(SID, None)
    yield sent
    _SESSIONS.pop(SID, None)


def _register(entry: dict | None) -> None:
    if entry is not None:
        _SESSIONS[SID] = {"session_id": SID, **entry}


# --------------------------------------------------------------------------- #
# 映射:Ginno 事件 → Vibe Island 事件
# --------------------------------------------------------------------------- #
def test_tool_start_maps_to_pretooluse_and_remembers_name(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(SID, "tool.start", {"name": "Bash", "id": "t1"})
    (ev,) = _clean_state
    assert ev["hook_event_name"] == "PreToolUse"
    assert ev["tool_name"] == "Bash"
    assert ev["session_id"] == SID and ev["cwd"] == "/tmp/ws"


def test_tool_end_maps_to_posttooluse_with_remembered_name(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(SID, "tool.start", {"name": "read_file", "id": "t9"})
    vi.on_turn_event(SID, "tool.end", {"id": "t9", "content": "…"})
    start, end = _clean_state
    assert start["hook_event_name"] == "PreToolUse"
    assert end["hook_event_name"] == "PostToolUse" and end["tool_name"] == "read_file"
    # id→name 记账已消费,同 id 再来一次 tool.end 不带名字也不炸
    vi.on_turn_event(SID, "tool.end", {"id": "t9"})
    assert _clean_state[-1]["hook_event_name"] == "PostToolUse"
    assert "tool_name" not in _clean_state[-1]


def test_permission_request_maps_to_approval_reminder(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(
        SID, "permission.request",
        {"tool": "Bash", "args": {"command": "rm -rf build"}},
    )
    (ev,) = _clean_state
    assert ev["hook_event_name"] == "Notification"
    assert ev["notification_type"] == "permission_prompt"
    assert ev["tool_name"] == "Bash"
    assert ev["tool_input"] == {"command": "rm -rf build"}


def test_large_tool_input_is_dropped(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(
        SID, "permission.request",
        {"tool": "write_file", "args": {"content": "x" * 600}},
    )
    (ev,) = _clean_state
    assert "tool_input" not in ev


def test_user_question_maps_to_ask_user_reminder(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(
        SID, "user.question",
        {"id": "call_1", "question": "装到哪?", "header": "选择位置",
         "options": ["a", "b"], "allow_free_text": True},
    )
    (ev,) = _clean_state
    assert ev["hook_event_name"] == "Notification"
    assert ev["notification_type"] == "permission_prompt"
    assert ev["tool_name"] == "ask_user"
    assert "选择位置" in ev["message"] and "装到哪?" in ev["message"]
    # 刘海端无法代答(bridge 单向,答题还是 Planned),选项拼进 message
    # 至少让用户在 notch 上读得到再点回来选。
    assert "a / b" in ev["message"]


def test_user_question_options_are_listed_and_bounded(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(
        SID, "user.question",
        {"question": "q", "header": "", "options": [
            {"label": "x" * 40}, "o2", "o3", "o4", "o5", "o6"]},
    )
    (ev,) = _clean_state
    msg = ev["message"]
    assert "x" * 24 in msg and "x" * 25 not in msg  # 单选项截到 24 字符
    assert "o5" in msg and "o6" not in msg          # 最多列 5 个选项
    assert len(msg) <= 200                          # 总长兜底


def test_message_end_maps_to_stop_with_reply(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(SID, "message.end", {"text": "修好了登录测试。"})
    (ev,) = _clean_state
    assert ev["hook_event_name"] == "Stop"
    assert ev["last_assistant_message"] == "修好了登录测试。"


def test_turn_stopped_maps_to_bare_stop(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(SID, "turn.stopped", {})
    (ev,) = _clean_state
    assert ev == {"hook_event_name": "Stop", "session_id": SID, "cwd": "/tmp/ws"}


def test_error_maps_to_stop_failure(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(SID, "error", {"message": "model API 500"})
    (ev,) = _clean_state
    assert ev["hook_event_name"] == "StopFailure"
    assert ev["last_assistant_message"] == "model API 500"


@pytest.mark.parametrize("event", ["message.delta", "keepalive", "notice",
                                   "turn.state", "steer.absorbed", "token"])
def test_hot_path_events_are_ignored(_clean_state, event):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(SID, event, {"text": "…"})
    assert _clean_state == []


# --------------------------------------------------------------------------- #
# 闸门:开关 / 子会话 / bridge
# --------------------------------------------------------------------------- #
def test_disabled_switch_sends_nothing(_clean_state, monkeypatch):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(False)
    vi.on_turn_event(SID, "message.end", {"text": "hi"})
    vi.user_prompt(SID, "hi")
    vi.session_started(SID)
    assert _clean_state == []


def test_subagent_sessions_never_reach_the_notch(_clean_state):
    """指南「Not supported: sub-sessions」:每个 subagent 一张卡只会是噪音。"""
    _register({"workspace": "/tmp/ws", "parent_session_id": "main-1",
               "type": "subagent", "depth": 1})
    vi.set_enabled(True)
    vi.session_started(SID)
    vi.user_prompt(SID, "干活")
    vi.on_turn_event(SID, "tool.start", {"name": "Bash", "id": "t1"})
    assert _clean_state == []
    # 但删除时的 SessionEnd 照发(对从未入卡的 id 无害,防真卡残留)
    vi.session_ended(SID)
    assert [e["hook_event_name"] for e in _clean_state] == ["SessionEnd"]


def test_real_send_short_circuits_when_bridge_missing(monkeypatch):
    """_send 的前置闸门:开关开但 bridge 不存在 → 连队列都不创建。"""
    vi.set_enabled(True)
    monkeypatch.setattr(vi, "bridge_available", lambda: False)
    monkeypatch.setattr(vi, "_queue", None)
    _real_send({"hook_event_name": "Stop", "session_id": SID, "cwd": "/tmp"})
    assert vi._queue is None


def test_real_send_short_circuits_when_disabled(monkeypatch):
    vi.set_enabled(False)
    monkeypatch.setattr(vi, "bridge_available", lambda: True)
    monkeypatch.setattr(vi, "_queue", None)
    _real_send({"hook_event_name": "Stop", "session_id": SID, "cwd": "/tmp"})
    assert vi._queue is None


# --------------------------------------------------------------------------- #
# cwd / 生命周期
# --------------------------------------------------------------------------- #
def test_cwd_prefers_primary_path_over_workspace(_clean_state):
    _register({"workspace": "/tmp/ws", "primary_path": "/Users/me/project"})
    vi.set_enabled(True)
    vi.session_started(SID)
    (ev,) = _clean_state
    assert ev["hook_event_name"] == "SessionStart"
    assert ev["cwd"] == "/Users/me/project"


def test_session_started_recomputes_cwd(_clean_state):
    _register({"workspace": "/tmp/ws1"})
    vi.set_enabled(True)
    vi.session_started(SID)
    assert _clean_state[-1]["cwd"] == "/tmp/ws1"
    _SESSIONS[SID]["workspace"] = "/tmp/ws2"
    vi.session_started(SID)
    assert _clean_state[-1]["cwd"] == "/tmp/ws2"


def test_session_ended_clears_caches(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.on_turn_event(SID, "tool.start", {"name": "Bash", "id": "t1"})
    vi.session_ended(SID)
    assert _clean_state[-1]["hook_event_name"] == "SessionEnd"
    assert SID not in vi._cwds and SID not in vi._tool_names


def test_enabled_flag_lazy_loads_from_registry(_clean_state, monkeypatch):
    """runtime 重启后 apply_config 不会再被调:开关必须能从 settings 懒加载。"""
    # 注意:connectors/__init__.py 把 `registry` 重绑成了 getter 函数,
    # 必须走子模块路径才能拿到 ConnectorRegistry 类。
    from ginno_runtime.connectors.builtin import VibeIslandConnector
    from ginno_runtime.connectors.registry import ConnectorRegistry, registry

    # is_enabled 有注册闸门(未注册 = 关,防 pytest 误发真事件):
    # 先把连接器注册进单例,再 patch read_config 提供假 settings。
    if registry().get("vibe-island") is None:
        registry().register(VibeIslandConnector())
    fake = {"enabled": True}
    monkeypatch.setattr(
        ConnectorRegistry, "read_config",
        lambda self, cid: dict(fake) if cid == "vibe-island" else {},
    )
    assert vi._enabled is None  # fixture 已复位成未加载态
    assert vi.is_enabled() is True
    # 模拟下一次重启:未加载态 + settings 里开关已关
    fake["enabled"] = False
    monkeypatch.setattr(vi, "_enabled", None)
    assert vi.is_enabled() is False


def test_permission_result_clears_approval(_clean_state):
    _register({"workspace": "/tmp/ws"})
    vi.set_enabled(True)
    vi.permission_result(SID)
    (ev,) = _clean_state
    assert ev["hook_event_name"] == "PermissionResult"
