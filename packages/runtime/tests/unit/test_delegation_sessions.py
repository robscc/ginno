"""Delegation sub-sessions（可回放的外部委托子会话）测试。

conftest 的 isolated_home 已把 GINNO_HOME 指向每测试独立的 tmp_path。
"""
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from ginno_runtime.checkpointer import FileCheckpointer
from ginno_runtime.delegation_sessions import _ir_to_messages, record_delegation
from ginno_runtime.session_meta import _session_meta_list
from ginno_runtime.tools.external_agents.base import DelegationResult


def _result(**kw):
    base = dict(
        output="done",
        stop_reason="success",
        usage={},
        meta={"model": "pi"},
        transcript=[],
    )
    base.update(kw)
    return DelegationResult(**base)


def test_ir_pairing_and_orphan_tool_result():
    ir = [
        {"role": "assistant", "text": "I will read", "tool_calls": [{"id": "c1", "name": "read", "args": {"path": "a.py"}}]},
        {"role": "tool_result", "id": "c1", "name": "read", "content": "print(1)", "is_error": False},
        # codex 孤儿 tool_result：无配对 tool_call —— 需补空 AIMessage 钉 id
        {"role": "tool_result", "id": "", "name": "bash", "content": "ls\na.py", "is_error": False},
        {"role": "assistant", "text": "All done", "tool_calls": []},
    ]
    msgs = _ir_to_messages(ir, "check a.py")
    kinds = [type(m).__name__ for m in msgs]
    assert kinds == ["HumanMessage", "AIMessage", "ToolMessage", "AIMessage", "ToolMessage", "AIMessage"]
    assert msgs[1].tool_calls[0]["id"] == "c1"
    assert msgs[2].tool_call_id == "c1"
    assert msgs[4].name == "bash"  # 孤儿结果照常渲染


def test_record_delegation_end_to_end():
    res = _result(
        transcript=[
            {"role": "assistant", "text": "reading", "tool_calls": [{"id": "c1", "name": "read", "args": {}}]},
            {"role": "tool_result", "id": "c1", "name": "read", "content": "x", "is_error": False},
        ],
        meta={"model": "pi", "_stdout_tail": '{"type":"session"}\n'},
    )
    sid = record_delegation(
        parent_session_id=None, project_slug="default", backend="pi",
        mode="read-only", prompt="check a.py", workspace="/tmp",
        result=res, timeout_s=60,
    )
    assert sid
    # meta 索引：type=delegation + 溯源字段
    entry = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert entry["type"] == "delegation" and entry["backend"] == "pi" and entry["mode"] == "read-only"
    # checkpoint 回读：消息类型序列保持（history 端点的渲染入口）
    tup = FileCheckpointer("default").get_tuple({"configurable": {"thread_id": sid}})
    msgs = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    assert [type(m).__name__ for m in msgs] == ["HumanMessage", "AIMessage", "ToolMessage"]
    # 原始事件流归档
    from ginno_runtime import paths

    raw = paths.session_files_dir("default", sid) / "delegation-events.jsonl"
    assert raw.exists() and "session" in raw.read_text()


def test_record_delegation_error_note_and_never_raises():
    res = _result(stop_reason="error", diagnostic="boom", output="")
    sid = record_delegation(
        parent_session_id=None, project_slug="default", backend="codex",
        mode="edit", prompt="p", workspace="/tmp", result=res, timeout_s=10,
    )
    assert sid  # 失败 run 也落子会话（含 [error] 注记）
    tup = FileCheckpointer("default").get_tuple({"configurable": {"thread_id": sid}})
    msgs = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    assert any(isinstance(m, AIMessage) and "boom" in (m.content or "") for m in msgs)
    # 契约：任何异常都不上抛；缺字段的鸭子类型 result 静默降级为空转录照常归档
    assert record_delegation(None, "default", "pi", "edit", "p", "/tmp", object(), 5)


def test_create_then_finalize_lifecycle():
    """开始即建骨架（running + prompt 转录）→ 完成回填（终态 + 完整转录）。"""
    from ginno_runtime.delegation_sessions import (
        create_delegation,
        finalize_delegation,
    )

    sid = create_delegation(None, "default", "pi", "edit", "生命周期", "/tmp")
    assert sid
    entry = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert entry["stop_reason"] == "running" and entry["type"] == "delegation"
    tup = FileCheckpointer("default").get_tuple({"configurable": {"thread_id": sid}})
    msgs = (tup.checkpoint.get("channel_values") or {}).get("messages") or []
    assert [type(m).__name__ for m in msgs] == ["HumanMessage"]  # 运行中回放非空屏
    assert msgs[0].content == "生命周期"

    res = _result(transcript=[{"role": "assistant", "text": "done", "tool_calls": []}])
    finalize_delegation(sid, "default", res, "生命周期", 30)
    entry2 = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert entry2["stop_reason"] == "success"
    tup2 = FileCheckpointer("default").get_tuple({"configurable": {"thread_id": sid}})
    msgs2 = (tup2.checkpoint.get("channel_values") or {}).get("messages") or []
    # history 读最后一条 checkpoint：回填后的完整转录生效
    assert [type(m).__name__ for m in msgs2] == ["HumanMessage", "AIMessage"]
    assert msgs2[1].content == "done"


def test_reconcile_running_delegations():
    """启动对账：running 存量 → error（runtime 重启后协程已消亡）。"""
    from ginno_runtime.delegation_sessions import (
        create_delegation,
        reconcile_running_delegations,
    )

    sid = create_delegation(None, "default", "pi", "edit", "对账", "/tmp")
    assert sid
    entry = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert entry["stop_reason"] == "running"
    n = reconcile_running_delegations()
    assert n >= 1
    entry2 = next(m for m in _session_meta_list("default") if m["id"] == sid)
    assert entry2["stop_reason"] == "error"
    # 幂等：第二次跑不再计数
    assert reconcile_running_delegations() == 0


def test_prune_skips_pinned_delegation_sessions(monkeypatch):
    """置顶的委托归档永不被自动清理，且不占用数量上限。"""
    import ginno_runtime.delegation_sessions as ds
    from ginno_runtime.session_meta import _session_meta_patch, _session_meta_upsert

    monkeypatch.setattr(ds, "_MAX_DELEGATION_SESSIONS", 3)
    for i in range(5):  # d0 最旧 → d4 最新
        _session_meta_upsert(
            "default",
            {
                "id": f"d{i}",
                "type": "delegation",
                "stop_reason": "success",
                "title": f"d{i}",
                "created": i,
                "updated": i,
            },
        )
    _session_meta_patch("default", "d0", {"pinned": True})  # 置顶最旧的一条

    ds._prune_delegation_sessions("default")

    kept = {m["id"] for m in _session_meta_list("default")}
    assert "d0" in kept  # 置顶豁免：不被清掉
    # 未置顶的按上限只留最近 3 条（d4/d3/d2），置顶行不占额度
    assert kept == {"d0", "d4", "d3", "d2"}
