"""a2a.fake 剧本驱动单测 —— 纯逻辑部分（剧本解析 + handle_rpc 响应组装）。

不真起 server：FakeA2APeer.handle_rpc 是 dict → dict 的零 IO 分发，
这里直接驱动它覆盖剧本语义。真 listener 的回环在 tests/e2e/test_a2a_loopback.py。
"""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a.errors import TASK_NOT_FOUND
from ginno_runtime.a2a.fake import (
    FakeA2APeer,
    fake_peer_enabled,
    parse_scripts,
    retention_error,
    scripts_from_env,
)

pytestmark = pytest.mark.unit


def _send_req(text: str = "hi", context_id: str = "", req_id: int = 1) -> dict:
    message: dict = {"role": "user", "parts": [{"kind": "text", "text": text}]}
    if context_id:
        message["contextId"] = context_id
    return {"jsonrpc": "2.0", "id": req_id, "method": "message/send",
            "params": {"message": message}}


def _result(resp: dict) -> dict:
    assert "error" not in resp, resp
    return resp["result"]


# --------------------------------------------------------------------------- #
# 剧本解析
# --------------------------------------------------------------------------- #

def test_parse_scripts_defaults_and_shapes():
    assert parse_scripts(None) == {"send": [], "get": [], "cancel": []}
    assert parse_scripts("garbage") == {"send": [], "get": [], "cancel": []}
    s = parse_scripts({
        "send": [{"result": "x"}],
        "cancel": {"state": "canceled"},  # 单 dict → 单条泳道
    })
    assert s["send"][0]["reply_kind"] == "task"
    assert s["send"][0]["states"] == ["completed"]  # 缺省状态序列
    assert s["send"][0]["result"] == "x"
    assert isinstance(s["cancel"], list) and len(s["cancel"]) == 1
    assert s["get"] == []


def test_parse_scripts_tolerates_garbage_entries():
    s = parse_scripts({
        "send": ["plain string", 42, {"states": "not-a-list", "reply_kind": "message"}],
        "get": [None],
        "cancel": [None],
    })
    assert s["send"][0]["result"] == "plain string"  # 字符串条目 → result
    assert s["send"][1]["states"] == ["completed"]  # 畸形条目归一为默认
    assert s["send"][2]["reply_kind"] == "message"
    assert s["send"][2]["states"] == ["completed"]
    assert s["get"][0]["error"] is None
    assert s["cancel"][0]["state"] == "canceled"


def test_scripts_from_env_file_inline_and_invalid(tmp_path, monkeypatch):
    monkeypatch.delenv("GINNO_FAKE_A2A_SCRIPTS", raising=False)
    assert scripts_from_env() == {}

    scripts_file = tmp_path / "scripts.json"
    scripts_file.write_text(json.dumps({"send": [{"result": "from file"}]}),
                            encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_A2A_SCRIPTS", str(scripts_file))
    assert scripts_from_env()["send"][0]["result"] == "from file"

    monkeypatch.setenv("GINNO_FAKE_A2A_SCRIPTS", '{"send": [{"result": "inline"}]}')
    assert scripts_from_env()["send"][0]["result"] == "inline"

    monkeypatch.setenv("GINNO_FAKE_A2A_SCRIPTS", "{not json")
    assert scripts_from_env() == {}  # 非法 → 空剧本，绝不抛


def test_fake_peer_enabled_truthy(monkeypatch):
    monkeypatch.delenv("GINNO_FAKE_A2A_PEER", raising=False)
    assert fake_peer_enabled() is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("GINNO_FAKE_A2A_PEER", value)
        assert fake_peer_enabled() is True
    monkeypatch.setenv("GINNO_FAKE_A2A_PEER", "0")
    assert fake_peer_enabled() is False


# --------------------------------------------------------------------------- #
# handle_rpc：send
# --------------------------------------------------------------------------- #

def test_send_task_shape_and_id_echo():
    peer = FakeA2APeer({"send": [{"result": "done text"}]})
    resp = peer.handle_rpc(_send_req(req_id=7))
    assert resp["jsonrpc"] == "2.0" and resp["id"] == 7
    task = _result(resp)
    assert task["id"] and task["contextId"]
    assert task["status"]["state"] == "completed"
    assert task["artifacts"][0]["parts"][0]["text"] == "done text"
    # 请求被记录（供 e2e 断言 contextId/referenceTaskIds 透传）
    assert peer.requests[0]["method"] == "message/send"


def test_send_message_reply_kind():
    peer = FakeA2APeer({"send": [{"reply_kind": "message", "result": "plain reply"}]})
    result = _result(peer.handle_rpc(_send_req()))
    assert result["kind"] == "message"
    assert result["role"] == "agent"
    assert result["parts"][0]["text"] == "plain reply"
    assert result["contextId"]
    assert "status" not in result  # Message 无 status——客户端 union 判别键


def test_send_error_entry_passthrough():
    peer = FakeA2APeer({"send": [{"error": retention_error("task_purged")}]})
    resp = peer.handle_rpc(_send_req())
    assert resp["error"]["code"] == TASK_NOT_FOUND
    assert resp["error"]["data"]["ginno:retention"]["reason"] == "task_purged"
    assert "result" not in resp


def test_send_context_reuse_and_generation():
    peer = FakeA2APeer({"send": [{"result": "a"}, {"result": "b"}, {"result": "c"}]})
    t1 = _result(peer.handle_rpc(_send_req(context_id="ctx-abc")))
    assert t1["contextId"] == "ctx-abc"  # 带 contextId → 透传复用
    t2 = _result(peer.handle_rpc(_send_req()))
    t3 = _result(peer.handle_rpc(_send_req()))
    assert t2["contextId"] and t3["contextId"]
    assert t2["contextId"] != t3["contextId"]  # 不带 → 各开新 context
    assert len({t1["id"], t2["id"], t3["id"]}) == 3  # taskId 唯一


def test_send_exhaustion_marker():
    peer = FakeA2APeer({})  # 空剧本
    task = _result(peer.handle_rpc(_send_req()))
    assert task["status"]["state"] == "completed"
    assert "script exhausted" in task["artifacts"][0]["parts"][0]["text"]


def test_send_working_has_no_artifact_yet():
    peer = FakeA2APeer({"send": [{"states": ["working", "completed"], "result": "later"}]})
    task = _result(peer.handle_rpc(_send_req()))
    assert task["status"]["state"] == "working"
    assert task["artifacts"] == []  # 终态才出产物


# --------------------------------------------------------------------------- #
# handle_rpc：get / cancel
# --------------------------------------------------------------------------- #

def test_get_unknown_task_is_not_found():
    peer = FakeA2APeer({})
    resp = peer.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tasks/get",
                            "params": {"id": "nope"}})
    assert resp["error"]["code"] == TASK_NOT_FOUND


def test_get_advances_state_sequence_then_stops_at_terminal():
    peer = FakeA2APeer({"send": [{"states": ["working", "completed"], "result": "final"}]})
    tid = _result(peer.handle_rpc(_send_req()))["id"]
    get = {"jsonrpc": "2.0", "id": 2, "method": "tasks/get", "params": {"id": tid}}
    t1 = _result(peer.handle_rpc(get))
    assert t1["status"]["state"] == "completed"
    assert t1["artifacts"][0]["parts"][0]["text"] == "final"
    t2 = _result(peer.handle_rpc(get))
    assert t2["status"]["state"] == "completed"  # 终态不再前进


def test_get_explicit_entry_overrides_state_machine():
    peer = FakeA2APeer({
        "send": [{"states": ["working"], "result": "orig"}],
        "get": [{"state": "failed", "result": "boom"}],
    })
    tid = _result(peer.handle_rpc(_send_req()))["id"]
    t = _result(peer.handle_rpc({"jsonrpc": "2.0", "id": 2, "method": "tasks/get",
                                 "params": {"id": tid}}))
    assert t["status"]["state"] == "failed"
    assert t["artifacts"][0]["parts"][0]["text"] == "boom"


def test_cancel_default_error_and_unknown():
    peer = FakeA2APeer({
        "send": [{"result": "r"}],
        "cancel": [{"error": retention_error("task_purged")}],
    })
    tid = _result(peer.handle_rpc(_send_req()))["id"]
    cancel = {"jsonrpc": "2.0", "id": 2, "method": "tasks/cancel", "params": {"id": tid}}
    # 第一条 cancel 是 error 条目 → 原样透传（幂等路径的 server 侧形态）
    resp = peer.handle_rpc(cancel)
    assert resp["error"]["code"] == TASK_NOT_FOUND
    # 泳道耗尽 → 默认取消成功
    t = _result(peer.handle_rpc(cancel))
    assert t["status"]["state"] == "canceled"
    # 未知 taskId → -32001
    resp = peer.handle_rpc({"jsonrpc": "2.0", "id": 3, "method": "tasks/cancel",
                            "params": {"id": "ghost"}})
    assert resp["error"]["code"] == TASK_NOT_FOUND


def test_malformed_rpc_requests():
    peer = FakeA2APeer({})
    assert peer.handle_rpc("not a dict")["error"]["code"] == -32600
    assert peer.handle_rpc({"jsonrpc": "2.0", "id": 1})["error"]["code"] == -32600
    resp = peer.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tasks/reply"})
    assert resp["error"]["code"] == -32601  # P0 不支持 reply
    resp = peer.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "message/send",
                            "params": {}})
    assert resp["error"]["code"] == -32602  # 缺 message
