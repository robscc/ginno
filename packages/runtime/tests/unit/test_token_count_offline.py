"""tiktoken 离线行为回归（2026-11-24 P0）。

背景：``token_count._encoder`` 直调 ``tiktoken.get_encoding``，冷缓存时
会从 openaipublic 下载词表；在无外网（TUN 全接管）的环境里 socket 永久
阻塞，拖死整个 pytest worker。这两条用例钉住：

a) 下载阻塞时 ``_encoder`` 必须在秒级超时返回 None，``count_tokens``
   回退到固定估算而不是卡死；
b) 即使底层网络直接抛错，``count_tokens`` 也永不抛（调用方约定）。

本文件不依赖真实 o200k 词表，CI 冷机器同样可跑。
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from ginno_runtime import token_count


@pytest.fixture(autouse=True)
def _fresh_encoder_state(monkeypatch: pytest.MonkeyPatch):
    """每个用例从干净的缓存/标记位开始，并还原 conftest 换掉的离线
    lambda——本文件就是要测真实的（加固版）加载路径。"""
    monkeypatch.setattr(token_count, "_encoder", token_count._REAL_ENCODER)
    monkeypatch.setattr(token_count, "_encoder_loaded", False)
    monkeypatch.setattr(token_count, "_cached_encoder", None)
    monkeypatch.setattr(token_count, "_offline_until", 0.0)
    yield


def test_encoder_timeout_returns_none(monkeypatch: pytest.MonkeyPatch):
    """get_encoding 永久阻塞 → _encoder 秒级返回 None（不卡 120s）。"""
    import tiktoken

    def _hang(name: str):  # pragma: no cover - deliberately hangs
        event = threading.Event()
        event.wait(60)
        raise AssertionError("should have timed out before reaching here")

    # conftest 的离线编码器把 _encoder 换成了 lambda；本用例要测真实的
    # 加载路径，先还原成加固版实现。
    monkeypatch.setattr(token_count, "_encoder", token_count._REAL_ENCODER)
    monkeypatch.setattr(tiktoken, "get_encoding", _hang)
    monkeypatch.setattr(token_count, "_LOAD_TIMEOUT_S", 2.0)
    t0 = time.monotonic()
    assert token_count._encoder() is None
    assert time.monotonic() - t0 < 30, "_encoder must not block on network"
    # 回退估算：纯英文 5 字符 → ceil(5/3.6) = 2（int 截断）
    assert token_count.count_tokens("hello") == int(5 / 3.6)
    assert token_count.counter_name() == "estimate"


def test_encoder_exception_returns_none(monkeypatch: pytest.MonkeyPatch):
    """socket 直接抛错 → 同样返回 None，count_tokens 永不 raise。"""

    def _boom(*args, **kwargs):
        raise OSError("network unreachable")

    monkeypatch.setattr(socket.socket, "connect", _boom)
    # 不 patch get_encoding：走真实的冷缓存路径（conftest 已指向空 home）
    n = token_count.count_tokens("hello")
    assert n > 0
