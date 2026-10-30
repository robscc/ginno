"""``/fork`` — 渠道（钉钉/企微）对话 → 桌面普通会话复制（im-fork-to-desktop P0）。

* 注册与解析：BUILTINS["fork"] 存在；resolve_turn 命中 builtin_async 路径
  （与 /clear 同口径）；/help 自动列出；
* gate：非渠道会话（无 meta / type != "channel"）→ 提示已是桌面会话；
* busy-gate（/clear 同款三查）：turn 在跑 / 停在确认步骤 → 拒绝（防止
  复制到写了一半的 checkpoint）；
* 幂等（fork_run 同款「一会话一副本」）：源 meta ``forked_to_session``
  存活 → 返回既有副本，不新建；副本被删 → 覆盖死引用重新 fork；
* 成功路径：create_session（agent 沿源）+ _seed_fork_history（brief 含
  「续自钉钉对话」「当前记录」——/clear 边界语义：checkpoint 是唯一复制
  源，clear 前内容已不存在，故用「当前记录」不用「完整记录」）+ 双向
  标记（新会话 forked_from_channel / 源会话 forked_to_session，
  touch=False 不顶源会话）+ 回执带消息数；
* seed 失败：显式报错回执，源 meta 不留 forked_to_session（下次重试
  重新 fork；半成品空会话与 fork_run 先例一致，不做清理）。

/clear 边界的端到端契约不在本文件锁——它由两段既有事实合成：
/clear 真删 checkpoint（RemoveMessage，test_commands.py 覆盖 handler）+
_seed_fork_history 只从 parent checkpoint 读（test_subagent_p3 fork 测试
覆盖复制源）。两段各自有测试，链路即闭环。
"""

from __future__ import annotations

import pytest

from ginno_runtime.commands.registry import BUILTINS
from ginno_runtime.commands.resolver import resolve_turn
from ginno_runtime.commands.registry import _fork_async_handler
from ginno_runtime.session_meta import (
    _find_meta,
    _session_meta_list,
    _session_meta_upsert,
)


# --------------------------------------------------------------------------- #
# 注册与解析
# --------------------------------------------------------------------------- #
def test_fork_registered_and_resolved():
    """BUILTINS 有 fork（async 路径）；resolve_turn 命中并提取附带消息。"""
    cmd = BUILTINS.get("fork")
    assert cmd is not None and cmd.async_handler is not None
    plan = resolve_turn({"message": "/fork"}, {"session_id": "s1"})
    assert plan.builtin_async == "fork"
    plan2 = resolve_turn({"message": "/fork 帮我总结上面的方案"}, {"session_id": "s1"})
    assert plan2.builtin_async == "fork"
    assert plan2.builtin_args == "帮我总结上面的方案"


def test_fork_listed_in_help(isolated_home):
    """/help 自动列出 /fork（_help_handler 遍历 BUILTINS，免费获得）。"""
    from ginno_runtime.commands.registry import _help_handler

    out = _help_handler("default", None, None)
    assert "/fork" in out


# --------------------------------------------------------------------------- #
# gate：仅渠道会话可用
# --------------------------------------------------------------------------- #
async def test_fork_requires_channel_session(isolated_home):
    """无 meta（桌面普通会话/野 id）→ 提示已是桌面会话，不建副本。"""
    n_before = len(_session_meta_list("default"))
    reply = await _fork_async_handler("default", {"session_id": "no-meta-id"})
    assert "已经是桌面会话" in reply or "already a desktop session" in reply
    assert len(_session_meta_list("default")) == n_before


async def test_fork_gate_allows_channel_session(isolated_home):
    """渠道会话（type=channel）过 gate——本例用 busy-gate 先拦，证明 gate
    顺序：channel 检查在 busy-gate 之前通过。"""
    _session_meta_upsert("default", {
        "id": "ch-1", "title": "钉钉问答", "type": "channel",
        "channel": "dingtalk", "created": 1.0, "updated": 1.0,
    })
    from ginno_runtime import server_shared as shared

    shared._RUNNING_TURNS["ch-1"] = "turn-1"
    try:
        reply = await _fork_async_handler("default", {"session_id": "ch-1"})
        assert "⏳" in reply  # 到了 busy-gate（channel gate 已过）
    finally:
        shared._RUNNING_TURNS.pop("ch-1", None)


# --------------------------------------------------------------------------- #
# busy-gate
# --------------------------------------------------------------------------- #
async def test_fork_busy_gate_rejects(isolated_home):
    """turn 在跑 → 拒绝复制（checkpoint 正被写）。"""
    _session_meta_upsert("default", {
        "id": "ch-2", "title": "钉钉问答", "type": "channel",
        "channel": "dingtalk", "created": 1.0, "updated": 1.0,
    })
    from ginno_runtime import server_shared as shared

    shared._RUNNING_TURNS["ch-2"] = "turn-1"
    try:
        reply = await _fork_async_handler("default", {"session_id": "ch-2"})
        assert "结束后再发" in reply or "after it finishes" in reply
        # 未产生副本
        meta, _ = _find_meta("ch-2")
        assert not meta.get("forked_to_session")
    finally:
        shared._RUNNING_TURNS.pop("ch-2", None)


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #
async def test_fork_idempotent_existing_copy(isolated_home):
    """副本存活 → 返回既有副本提示，不新建（一会话一副本）。"""
    _session_meta_upsert("default", {
        "id": "child-1", "title": "桌面副本", "created": 1.0, "updated": 1.0,
    })
    _session_meta_upsert("default", {
        "id": "ch-3", "title": "钉钉问答", "type": "channel",
        "channel": "dingtalk", "forked_to_session": "child-1",
        "created": 1.0, "updated": 1.0,
    })
    n_before = len(_session_meta_list("default"))
    reply = await _fork_async_handler("default", {"session_id": "ch-3"})
    assert "已复制过" in reply or "Already copied" in reply
    assert len(_session_meta_list("default")) == n_before  # 没新建


# --------------------------------------------------------------------------- #
# 成功路径
# --------------------------------------------------------------------------- #
async def test_fork_success_end_to_end(isolated_home, patch_build_model, monkeypatch):
    """渠道会话 /fork → 新普通会话 + 双向标记 + 回执带消息数；
    brief 溯源行含「续自钉钉对话」与「当前记录」（/clear 边界文案）。"""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    _session_meta_upsert("default", {
        "id": "ch-4", "title": "钉钉问答", "type": "channel",
        "channel": "dingtalk", "created": 1.0, "updated": 1.0,
    })

    seen: dict = {}

    async def fake_seed(parent_id, child_id, slug, brief):
        seen.update(parent=parent_id, child=child_id, slug=slug, brief=brief)
        return 3

    import ginno_runtime.subagent_scheduler as sched

    monkeypatch.setattr(sched, "_seed_fork_history", fake_seed)

    reply = await _fork_async_handler("default", {"session_id": "ch-4"})
    # isolated_home 把 locale pin 到 en——回执断言一律双语 or。
    assert (
        "已复制到桌面普通对话《钉钉问答》" in reply
        or 'Copied to desktop session "钉钉问答"' in reply
    )
    assert "3 条消息" in reply or "(3 messages)" in reply
    # seed 调用参数：源/目标/slug/brief
    assert seen["parent"] == "ch-4"
    assert seen["slug"] == "default"
    assert (
        "续自钉钉对话" in seen["brief"]
        or "Continued from a DingTalk conversation" in seen["brief"]
    )
    assert (
        "当前记录" in seen["brief"]
        or "current transcript" in seen["brief"]
    )  # 非「完整记录」（/clear 边界文案）
    # 双向标记
    child_id = seen["child"]
    child_meta, slug2 = _find_meta(child_id)
    assert child_meta["forked_from_channel"] == "dingtalk"
    assert child_meta.get("type") != "channel"  # 新会话是普通会话
    src_meta, _ = _find_meta("ch-4")
    assert src_meta["forked_to_session"] == child_id


async def test_fork_success_deleted_copy_reforks(isolated_home, patch_build_model, monkeypatch):
    """副本被删（死引用）→ 覆盖引用重新 fork 出新副本。"""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    _session_meta_upsert("default", {
        "id": "ch-5", "title": "钉钉问答", "type": "channel",
        "channel": "dingtalk", "forked_to_session": "dead-child",
        "created": 1.0, "updated": 1.0,
    })  # dead-child 无 meta = 已被删除

    async def fake_seed(parent_id, child_id, slug, brief):
        return 1

    import ginno_runtime.subagent_scheduler as sched

    monkeypatch.setattr(sched, "_seed_fork_history", fake_seed)

    reply = await _fork_async_handler("default", {"session_id": "ch-5"})
    assert (
        "已复制到桌面普通对话" in reply
        or "Copied to desktop session" in reply
    )
    src_meta, _ = _find_meta("ch-5")
    assert src_meta["forked_to_session"] != "dead-child"  # 死引用已被覆盖


# --------------------------------------------------------------------------- #
# seed 失败
# --------------------------------------------------------------------------- #
async def test_fork_seed_failure(isolated_home, patch_build_model, monkeypatch):
    """seed 抛错 → 显式报错回执；源 meta 不留 forked_to_session（重试可
    重新 fork）。半成品空会话残留与 fork_run 先例一致，不做清理。"""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    _session_meta_upsert("default", {
        "id": "ch-6", "title": "钉钉问答", "type": "channel",
        "channel": "wecom", "created": 1.0, "updated": 1.0,
    })

    async def boom(parent_id, child_id, slug, brief):
        raise RuntimeError("disk gone")

    import ginno_runtime.subagent_scheduler as sched

    monkeypatch.setattr(sched, "_seed_fork_history", boom)

    reply = await _fork_async_handler("default", {"session_id": "ch-6"})
    assert "复制失败" in reply or "Fork failed" in reply
    src_meta, _ = _find_meta("ch-6")
    assert not src_meta.get("forked_to_session")


# --------------------------------------------------------------------------- #
# 附带消息（P1 预告）
# --------------------------------------------------------------------------- #
async def test_fork_with_attached_message_notes_limit(
    isolated_home, patch_build_model, monkeypatch
):
    """/fork <消息>：P0 忽略附带内容但明确告知（防静默丢内容）。"""
    from ginno_runtime.testing.fake_model import ScriptedChatModel, script

    patch_build_model(ScriptedChatModel(scripts=[script(text="ok")]))
    _session_meta_upsert("default", {
        "id": "ch-7", "title": "钉钉问答", "type": "channel",
        "channel": "dingtalk", "created": 1.0, "updated": 1.0,
    })

    async def fake_seed(parent_id, child_id, slug, brief):
        return 1

    import ginno_runtime.subagent_scheduler as sched

    monkeypatch.setattr(sched, "_seed_fork_history", fake_seed)

    reply = await _fork_async_handler(
        "default", {"session_id": "ch-7"}, args="帮我总结上面的方案"
    )
    assert "暂不支持带走" in reply or "not carried over yet" in reply
