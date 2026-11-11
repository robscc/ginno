"""DeltaCoalescer 单测：合帧/保序/首片即时/禁用回退/close 幂等。"""

from __future__ import annotations

import asyncio
import json

import pytest

from ginno_runtime.api.stream.delta_coalesce import DeltaCoalescer


def _emit(event: str, data: dict) -> str:
    return json.dumps({"event": event, **data})


class Recorder:
    def __init__(self):
        self.frames: list[dict] = []

    async def send(self, raw: str) -> None:
        self.frames.append(json.loads(raw))

    def contents(self, kind: str) -> list[str]:
        return [f["content"] for f in self.frames if f["event"] == kind]


@pytest.mark.asyncio
async def test_coalesces_within_window():
    rec = Recorder()
    coal = DeltaCoalescer(rec.send, _emit, window_s=0.05)
    await coal.feed("token.delta", "a")  # 首片即时
    await coal.feed("token.delta", "b")
    await coal.feed("token.delta", "c")
    assert rec.contents("token.delta") == ["a"]  # b/c 还在缓冲
    await asyncio.sleep(0.08)
    assert rec.contents("token.delta") == ["a", "bc"]  # 窗口到，合帧
    await coal.close()


@pytest.mark.asyncio
async def test_flush_before_non_delta_preserves_order():
    rec = Recorder()
    coal = DeltaCoalescer(rec.send, _emit, window_s=1.0)  # 长窗口确保不自动 flush
    await coal.feed("token.delta", "hello ")  # 首片即时
    await coal.feed("token.delta", "world")  # 进缓冲
    await coal.flush()  # 模拟 tool.start 前的保序 flush
    await rec.send(_emit("tool.start", {"name": "bash"}))
    kinds = [f["event"] for f in rec.frames]
    assert kinds == ["token.delta", "token.delta", "tool.start"]
    assert rec.contents("token.delta") == ["hello ", "world"]
    await coal.close()


@pytest.mark.asyncio
async def test_first_fragment_immediate():
    rec = Recorder()
    coal = DeltaCoalescer(rec.send, _emit, window_s=1.0)
    await coal.feed("token.delta", "首")
    assert rec.contents("token.delta") == ["首"]  # 不等窗口
    await coal.close()


@pytest.mark.asyncio
async def test_kinds_independent():
    rec = Recorder()
    coal = DeltaCoalescer(rec.send, _emit, window_s=0.05)
    await coal.feed("token.delta", "t1")
    await coal.feed("thinking.delta", "k1")
    await coal.feed("token.delta", "t2")
    await coal.feed("thinking.delta", "k2")
    await asyncio.sleep(0.08)
    assert rec.contents("token.delta") == ["t1", "t2"]
    assert rec.contents("thinking.delta") == ["k1", "k2"]
    await coal.close()


@pytest.mark.asyncio
async def test_disabled_passthrough():
    rec = Recorder()
    coal = DeltaCoalescer(rec.send, _emit, window_s=0)
    await coal.feed("token.delta", "x")
    await coal.feed("token.delta", "y")
    assert rec.contents("token.delta") == ["x", "y"]  # 逐帧，现状行为
    await coal.close()


@pytest.mark.asyncio
async def test_close_flushes_and_idempotent():
    rec = Recorder()
    coal = DeltaCoalescer(rec.send, _emit, window_s=1.0)
    await coal.feed("token.delta", "a")
    await coal.feed("token.delta", "tail")
    await coal.close()
    assert rec.contents("token.delta") == ["a", "tail"]
    await coal.close()  # 幂等
    await coal.feed("token.delta", "after-close")  # close 后直发
    assert rec.contents("token.delta") == ["a", "tail", "after-close"]


@pytest.mark.asyncio
async def test_empty_text_ignored():
    rec = Recorder()
    coal = DeltaCoalescer(rec.send, _emit, window_s=0.05)
    await coal.feed("token.delta", "")
    await coal.close()
    assert rec.frames == []
