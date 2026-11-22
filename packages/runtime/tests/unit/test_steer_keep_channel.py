# ⏹ 打断带未吸收插播的确定性回归（2026-11-22 巡检 P1，用户拍板必测）。
#
# 事故形状：运行中 composer 插话（interactive steer，无 origin）→ 点 ⏹
# → 段末清场 steer_keep_channel 把无 origin 条目静默丢弃 → 前端
# steer.recall 落空（stash 已空，永远等不到 steer.recalled）→ 输入既不在
# 历史也不回 composer，reload 后仍丢失（用户数据丢失级）。
#
# 修复契约：keep_channel 把 interactive 条目挪进 recall buffer（不被下个
# turn 排水误吸收）；steer_recall 能从 stash **和** buffer 领回；渠道来
# 源（channel/task/worker/team）语义不变（仍留 stash 等各自补投方）。

from __future__ import annotations

import pytest

from ginno_runtime import server_shared as shared

pytestmark = pytest.mark.unit


def _enq(session: str, steer_id: str, text: str, origin: str | None = None):
    entry = {"steer_id": steer_id, "turn_id": "t1", "text": text}
    if origin is not None:
        entry["origin"] = origin
    shared.steer_enqueue(session, entry)


def _stash_ids(session: str):
    return [e.get("steer_id") for e in shared._STEER_STASH.get(session) or []]


def _buf_ids(session: str):
    return [e.get("steer_id") for e in shared._STEER_RECALL_BUFFER.get(session) or []]


@pytest.fixture(autouse=True)
def _clean():
    yield
    shared._STEER_STASH.clear()
    shared._STEER_RECALL_BUFFER.clear()


def test_stop_with_unabsorbed_steer_text_is_recoverable():
    """拍板指定用例：插话 + 停止后输入可恢复（composer 领回）。"""
    s = "steer-stop-1"
    _enq(s, "m1", "插播一条待回收的消息")  # 用户插话：无 origin
    shared.steer_keep_channel(s)  # ⏹ 段末清场
    # 条目不能凭空消失：在 buffer，不在排水路径（stash）
    assert _stash_ids(s) == []
    assert _buf_ids(s) == ["m1"]
    # 前端 turn-end 发 steer.recall → 服务端领回
    removed = shared.steer_recall(s, ["m1"])
    assert removed == ["m1"]
    assert _buf_ids(s) == []
    assert shared.steer_recall(s, ["m1"]) == []  # 幂等：二次领回为空


def test_interactive_never_absorbed_by_next_turn():
    """修复的另一面：清场后的插话不得被下个 turn 排水误吸收。"""
    s = "steer-stop-2"
    _enq(s, "m2", "别自动注进下一轮")
    shared.steer_keep_channel(s)
    # 下一轮开跑前的排水：吸收到的应为空（条目在 buffer 不在 stash）
    assert shared.steer_drain(s) == []
    assert _buf_ids(s) == ["m2"]


def test_channel_origins_keep_stash_semantics():
    """渠道来源语义不变：仍留 stash 等各自补投方，不被挪进 buffer。"""
    s = "steer-stop-3"
    for i, origin in enumerate(("channel", "task", "worker", "team")):
        _enq(s, f"c{i}", f"渠道 {origin}", origin=origin)
    shared.steer_keep_channel(s)
    assert sorted(_stash_ids(s)) == ["c0", "c1", "c2", "c3"]
    assert _buf_ids(s) == []


def test_recall_works_from_both_stash_and_buffer():
    s = "steer-stop-4"
    _enq(s, "s1", "还在 stash 的渠道件", origin="task")
    _enq(s, "u1", "已在 buffer 的插话")
    shared.steer_keep_channel(s)
    removed = shared.steer_recall(s, ["s1", "u1"])
    assert sorted(removed) == ["s1", "u1"]
    assert _stash_ids(s) == [] and _buf_ids(s) == []


def test_mixed_keep_keeps_only_interactive_in_buffer():
    s = "steer-stop-5"
    _enq(s, "t1", "task 件", origin="task")
    _enq(s, "u1", "插话")
    _enq(s, "u2", "插话2")
    shared.steer_keep_channel(s)
    assert _stash_ids(s) == ["t1"]
    assert _buf_ids(s) == ["u1", "u2"]
