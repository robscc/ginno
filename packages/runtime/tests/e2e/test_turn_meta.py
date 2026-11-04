"""E2E: turn 级耗时+token（turn 右下角小字）全链路。

engine 收尾把 elapsed/tokens 追加到 session 的 turns.jsonl（按 bubble 内
最后一条 AIMessage 的 message_id 对齐），history 投影据此给 assistant
bubble 附 kind="turn_meta" block——reload 后仍在。message.end 的 payload
同时带 meta（前端 live 值定格；本测试在 WS 帧上断言）。
"""

from __future__ import annotations

import json

import pytest
from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.e2e


def test_turn_meta_jsonl_projection_and_end_payload(client, create_session, ws_conv):
    sid = create_session([script(text="done and dusted", usage={"input_tokens": 120, "output_tokens": 35, "total_tokens": 155})])
    with ws_conv(sid) as conv:
        conv.invoke("do it")
        events = conv.recv_until("message.end", "error")

    # 1) message.end payload 带权威 meta（前端 live 定格源）
    end = [e for e in events if e.get("event") == "message.end"]
    assert end, "no message.end frame"
    meta = end[0].get("meta") or {}
    assert isinstance(meta.get("elapsed_s"), (int, float))
    assert meta.get("input_tokens") == 120 and meta.get("output_tokens") == 35
    assert meta.get("message_id"), "message_id is the projection alignment key"

    # 2) turns.jsonl 落盘（持久化源），字段与 payload 一致
    tf = paths.session_files_dir("default", sid) / "turns.jsonl"
    assert tf.exists(), "turns.jsonl not written"
    rec = json.loads(tf.read_text(encoding="utf-8").splitlines()[-1])
    assert rec["message_id"] == meta["message_id"]
    assert rec["elapsed_s"] == meta["elapsed_s"]
    assert rec["input_tokens"] == 120

    # 3) history 投影：assistant bubble 尾部带 turn_meta block
    hist = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    blocks = [
        b for m in hist for b in (m.get("blocks") or []) if isinstance(b, dict) and b.get("kind") == "turn_meta"
    ]
    assert blocks, "no turn_meta block in history projection"
    assert blocks[0]["elapsedS"] == rec["elapsed_s"]
    assert blocks[0]["inputTokens"] == 120 and blocks[0]["outputTokens"] == 35


def test_turn_meta_absent_for_old_sessions(client, create_session):
    """没有 turns.jsonl 的会话（老数据/工具轮）投影不带 turn_meta——静默降级。"""
    sid = create_session([script(text="hi")])
    hist = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    assert not any(
        isinstance(b, dict) and b.get("kind") == "turn_meta" for m in hist for b in (m.get("blocks") or [])
    )
