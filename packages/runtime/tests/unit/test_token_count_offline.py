"""Exercise the real loader without contacting the vocabulary server."""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import tiktoken

from ginno_runtime import token_count

pytestmark = pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")


@pytest.fixture(autouse=True)
def _fresh_encoder_state(monkeypatch: pytest.MonkeyPatch, tmp_path):
    monkeypatch.setattr(token_count, "_encoder", token_count._REAL_ENCODER)
    monkeypatch.setattr(token_count, "_encoder_loaded", False)
    monkeypatch.setattr(token_count, "_cached_encoder", None)
    monkeypatch.setattr(token_count, "_offline_until", 0.0)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path / "explicit-cache"))
    clock = SimpleNamespace(now=1000.0)
    monkeypatch.setattr(token_count, "time", SimpleNamespace(monotonic=lambda: clock.now))
    return clock


@pytest.fixture
def blocked_loader(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    started = threading.Event()
    release = threading.Event()
    workers: list[threading.Thread] = []
    encoder = object()

    def track_worker(*args, **kwargs):
        worker = threading.Thread(*args, **kwargs)
        workers.append(worker)
        return worker

    def load(name: str):
        started.set()
        release.wait()
        if getattr(request, "param", False):
            raise OSError("network unreachable after timeout")
        return encoder

    # Keep the actual thread and event implementations. Track each launch so
    # even a regression that duplicates loaders can be cleaned up on failure.
    monkeypatch.setattr(token_count, "threading", SimpleNamespace(Thread=track_worker, Event=threading.Event))
    monkeypatch.setattr(tiktoken, "get_encoding", load)
    monkeypatch.setattr(token_count, "_LOAD_TIMEOUT_S", 0.05)
    try:
        yield started, release, workers, encoder
    finally:
        release.set()
        for worker in workers:
            worker.join(timeout=5)
            assert not worker.is_alive()


def test_concurrent_timeouts_share_one_loader_across_cooldowns(blocked_loader, _fresh_encoder_state):
    started, _, workers, _ = blocked_loader
    callers = threading.Barrier(4)

    def count():
        callers.wait(timeout=5)
        return token_count.count_tokens("你好hello")

    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(count) for _ in range(4)]
        assert started.wait(timeout=5)
        assert [call.result(timeout=5) for call in pending] == [3] * 4

    for _ in range(3):
        _fresh_encoder_state.now += token_count._OFFLINE_COOLDOWN_S
        assert token_count.counter_name() == "estimate"

    assert len(workers) == 1
    assert workers[0].is_alive()


def test_late_success_is_cached_during_cooldown(blocked_loader):
    started, release, workers, encoder = blocked_loader
    assert token_count._encoder() is None
    assert started.wait(timeout=5)
    assert token_count.counter_name() == "estimate"

    release.set()
    workers[0].join(timeout=5)
    assert not workers[0].is_alive()
    assert token_count._encoder() is encoder
    assert token_count.counter_name() == "tiktoken"
    assert len(workers) == 1


@pytest.mark.parametrize("blocked_loader", [True], indirect=True)
def test_late_failure_starts_cooldown_from_completion(blocked_loader, _fresh_encoder_state):
    started, release, workers, _ = blocked_loader
    assert token_count._encoder() is None
    assert started.wait(timeout=5)

    half_window = token_count._OFFLINE_COOLDOWN_S / 2
    _fresh_encoder_state.now += half_window
    release.set()
    workers[0].join(timeout=5)
    assert not workers[0].is_alive()

    # The original timeout's cooldown has expired, but the failure's has not.
    _fresh_encoder_state.now += half_window
    assert token_count.counter_name() == "estimate"
    assert len(workers) == 1


@pytest.mark.parametrize("error", [OSError("network unreachable"), ValueError("invalid vocabulary")])
def test_failure_cools_down_and_success_is_cached(monkeypatch, _fresh_encoder_state, error):
    calls = []
    cache = os.environ["TIKTOKEN_CACHE_DIR"]

    def load(name: str):
        calls.append((name, os.environ["TIKTOKEN_CACHE_DIR"]))
        if len(calls) == 1:
            raise error
        return SimpleNamespace(encode=encode)

    def encode(text, *, disallowed_special):
        assert text == "<|endoftext|>"
        assert disallowed_special == ()
        return [1, 2, 3, 4, 5, 6]

    monkeypatch.setattr(tiktoken, "get_encoding", load)
    assert token_count.count_tokens("hello") == 1
    assert token_count.counter_name() == "estimate"
    assert calls == [("o200k_base", cache)]

    _fresh_encoder_state.now += token_count._OFFLINE_COOLDOWN_S
    assert token_count.count_tokens("<|endoftext|>") == 6
    assert token_count.counter_name() == "tiktoken"
    assert calls == [("o200k_base", cache)] * 2


def test_bundle_cache_selected_without_explicit_override(monkeypatch, tmp_path):
    bundled = tmp_path / "tiktoken_cache"
    bundled.mkdir()
    monkeypatch.setattr(token_count.sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.delenv("TIKTOKEN_CACHE_DIR")
    seen = []
    encoder = object()

    def load(name: str):
        seen.append((name, os.environ["TIKTOKEN_CACHE_DIR"]))
        return encoder

    monkeypatch.setattr(tiktoken, "get_encoding", load)
    assert token_count._encoder() is encoder
    assert seen == [("o200k_base", str(bundled))]
