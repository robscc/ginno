"""_resolve_session_meta 内存条目 → meta 形状投影（2026-11-21 模型 chip 修复）。

根因：内存条目带 model 对象（ChatOpenAI dump）与内部键名
（model_provider/model_name/session_id），磁盘 meta 是 provider/model/id
字符串——同一 API 出口两种形状。活跃会话（running subagent / 换绑后）
点开时 GET 返回对象形状，前端 merge 后 ModelChipMenu 的
label = session.model（对象）→ 模型名渲染异常。
"""

from __future__ import annotations

import ginno_runtime.session_meta as sm


class _FakeModel:
    """最小模型对象替身：不可 JSON 序列化、dump 后是对象不是名字。"""


def test_memory_entry_projected_to_meta_shape(monkeypatch):
    entry = {
        "session_id": "sid-1",
        "project_slug": "default",
        "workspace": "/w",
        "agent_id": "dev",
        "title": "t",
        "model_provider": "prov_a",
        "model_name": "zhipu/glm-5.3",
        "thinking_effort": "auto",
        "graph": object(),
        "model": _FakeModel(),
        "type": "subagent",
    }
    monkeypatch.setitem(sm._SESSIONS, "sid-1", entry)
    m = sm._resolve_session_meta("sid-1")
    assert m is not None
    # meta 键形状：字符串模型、provider、id
    assert m["model"] == "zhipu/glm-5.3"
    assert m["provider"] == "prov_a"
    assert m["id"] == "sid-1"
    # 对象键不外泄
    assert "graph" not in m
    assert not hasattr(m.get("model"), "invoke")
    # 其余字段原样保留
    assert m["agent_id"] == "dev"
    assert m["type"] == "subagent"


def test_disk_fallback_unchanged(monkeypatch, tmp_path):
    # 清空内存账本，指向 tmp home 的磁盘索引
    monkeypatch.clearitem if False else None
    monkeypatch.setattr(sm._SESSIONS, "clear", sm._SESSIONS.clear, raising=False)
    sm._SESSIONS.clear()
    monkeypatch.setattr(sm.paths, "home", lambda: tmp_path)
    proj = tmp_path / "projects" / "default" / "sessions"
    proj.mkdir(parents=True)
    (proj / "_index.json").write_text(
        '[{"id": "disk-1", "provider": "prov_b", "model": "m-b", "title": "d"}]'
    )
    m = sm._resolve_session_meta("disk-1")
    assert m is not None
    assert m["model"] == "m-b"
    assert m["provider"] == "prov_b"
    assert sm._resolve_session_meta("nope-404") is None
