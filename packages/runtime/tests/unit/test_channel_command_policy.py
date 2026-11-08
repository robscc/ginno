"""G3 渠道命令权限闸（docs/design/channel-devswap-unattended-design.md §2）。

矩阵：admin 命令（/dev-swap）在 channel 上下文按 userid ∈ admin_users 判定
（fail-closed：名单空=全拒）；无 channel_ctx（桌面 WS 形态）永不闸。
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from ginno_runtime.channels import base as ch_base

pytestmark = pytest.mark.unit


class _RecordingTap:
    """记录 token.delta 的最小 tap——拒绝回执走与放行相同的三帧微 turn。"""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.events: list[dict] = []

    async def send_text(self, raw: str) -> None:
        self.events.append(json.loads(raw))

    async def close(self) -> None:
        pass

    @property
    def reply_text(self) -> str:
        return "".join(
            e.get("content", "") for e in self.events if e.get("event") == "token.delta"
        )


def _session(sid: str) -> dict:
    return {"session_id": sid, "project_slug": "p", "graph": None, "agent_id": None}


def _ctx(userid: str, admins: list[str] | None) -> dict:
    return {"kind": "wecom", "userid": userid, "admin_users": admins or []}


@pytest.fixture
def fake_dev_swap(monkeypatch):
    """替身 dev-swap async handler：记录调用、绝不真起构建进程。"""
    from ginno_runtime.commands.registry import BUILTINS

    calls: list[tuple] = []

    async def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return "swap started (fake)"

    cmd = BUILTINS["dev-swap"]
    assert cmd.channel_policy == "admin"
    monkeypatch.setitem(
        BUILTINS, "dev-swap", dataclasses.replace(cmd, async_handler=_fake)
    )
    return calls


async def test_admin_cmd_rejected_for_non_admin(isolated_home, fake_dev_swap):
    tap = _RecordingTap("s1")
    await ch_base.run_channel_turn(
        _session("s1"), "/dev-swap", tap,
        channel_ctx=_ctx("alice", ["bob"]),
    )
    assert fake_dev_swap == []  # handler 未被调
    assert "admin-only" in tap.reply_text or "仅管理员" in tap.reply_text
    assert "alice" not in tap.reply_text  # 不回显身份细节，只指路配置


async def test_admin_cmd_passes_for_admin(isolated_home, fake_dev_swap):
    tap = _RecordingTap("s2")
    await ch_base.run_channel_turn(
        _session("s2"), "/dev-swap", tap,
        channel_ctx=_ctx("alice", ["alice"]),
    )
    assert len(fake_dev_swap) == 1
    assert "fake" in tap.reply_text


async def test_admin_cmd_fail_closed_when_no_admins(isolated_home, fake_dev_swap):
    """admin_users 未配置/为空 → admin 级命令对 channel 全拒。"""
    tap = _RecordingTap("s3")
    await ch_base.run_channel_turn(
        _session("s3"), "/dev-swap", tap, channel_ctx=_ctx("alice", None)
    )
    assert fake_dev_swap == []
    assert "admin-only" in tap.reply_text or "仅管理员" in tap.reply_text


async def test_no_ctx_never_gated(isolated_home, fake_dev_swap):
    """桌面 WS 形态（不传 channel_ctx）：闸门整段跳过，命令照常执行。"""
    tap = _RecordingTap("s4")
    await ch_base.run_channel_turn(_session("s4"), "/dev-swap", tap)
    assert len(fake_dev_swap) == 1


async def test_policy_all_cmd_not_gated(isolated_home, monkeypatch):
    """channel_policy=all（缺省）的 async 命令不受闸门影响：/compact 走起。"""
    from ginno_runtime.commands.registry import BUILTINS

    called: list[str] = []

    async def _fake_compact(*a, **k):
        called.append("compact")
        return "compacted (fake)"

    cmd = BUILTINS["compact"]
    assert cmd.channel_policy == "all"
    monkeypatch.setitem(
        BUILTINS, "compact", dataclasses.replace(cmd, async_handler=_fake_compact)
    )
    tap = _RecordingTap("s5")
    await ch_base.run_channel_turn(
        _session("s5"), "/compact", tap, channel_ctx=_ctx("nobody", [])
    )
    assert called == ["compact"]


async def test_plain_text_unaffected(isolated_home, monkeypatch):
    """非命令文本照常进 turn（闸门只作用于 builtin 短路分支）。"""
    from ginno_runtime.api import stream as stream_api

    ran: list[str] = []

    async def fake_run_stream(*args, **kwargs):
        ran.append("turn")

    monkeypatch.setattr(stream_api, "_run_stream", fake_run_stream)
    tap = _RecordingTap("s6")
    await ch_base.run_channel_turn(
        _session("s6"), "普通消息", tap, channel_ctx=_ctx("nobody", [])
    )
    assert ran == ["turn"]
