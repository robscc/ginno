"""agent_node 的限流退避(graph._ainvoke_rate_limited)。

429 类异常不该杀死 turn:退避后重试;非限流异常照抛;超过次数上限照抛。
"""

import pytest

from ginno_runtime.graph import _ainvoke_rate_limited


class _FlakyBound:
    def __init__(self, failures, exc):
        self.failures = failures
        self.exc = exc
        self.calls = 0

    async def ainvoke(self, msgs):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return f"ok-{self.calls}"


async def test_rate_limit_retries_then_succeeds(monkeypatch):
    monkeypatch.setattr("ginno_runtime.graph.asyncio.sleep", _no_sleep)
    bound = _FlakyBound(2, RuntimeError("APIError: Backend returned 429"))
    out = await _ainvoke_rate_limited(bound, ["m"])
    assert out == "ok-3"


async def test_non_rate_limit_raises_immediately(monkeypatch):
    monkeypatch.setattr("ginno_runtime.graph.asyncio.sleep", _no_sleep)
    bound = _FlakyBound(5, ValueError("bad schema"))
    with pytest.raises(ValueError):
        await _ainvoke_rate_limited(bound, ["m"])
    assert bound.calls == 1


async def test_rate_limit_gives_up_after_cap(monkeypatch):
    monkeypatch.setattr("ginno_runtime.graph.asyncio.sleep", _no_sleep)
    bound = _FlakyBound(99, RuntimeError("429 rate limit exceeded"))
    with pytest.raises(RuntimeError):
        await _ainvoke_rate_limited(bound, ["m"])
    # 首调 + 5 次退避重试
    assert bound.calls == 6


async def _no_sleep(_s):
    return None
