"""Unit tests for the transient-error classifier behind turn auto-retry.

Incident (2026-09-29, turn f50a6304): ssl SSLV3_ALERT_BAD_RECORD_MAC mid-stream
+ a 180s prefill stall on the checkpoint retry killed a healthy turn twice.
`_is_transient_model_error` decides which turn failures are worth an automatic
backoff retry from the last checkpoint.
"""

from __future__ import annotations

import ssl

from ginno_runtime.api.stream import _is_transient_model_error


# The classifier matches by exact CLASS NAME (no hard imports of openai/
# anthropic/httpx), so the stand-in must literally be named APIConnectionError.
APIConnectionError = type("APIConnectionError", (Exception,), {})


class _FakeAPIStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


def test_stall_watchdog_runtime_error():
    assert _is_transient_model_error(
        RuntimeError("model/stream stall: no chunk for 180s")
    )


def test_plain_runtime_error_not_transient():
    assert not _is_transient_model_error(RuntimeError("some graph bug"))


def test_connection_error_by_name():
    assert _is_transient_model_error(APIConnectionError("Connection error."))


def test_ssl_error():
    assert _is_transient_model_error(
        ssl.SSLError("ssl/tls alert bad record mac (_ssl.c:2580)")
    )


def test_builtin_connection_errors():
    assert _is_transient_model_error(ConnectionResetError("reset by peer"))


def test_bare_timeout_error_not_transient():
    # asyncio.TimeoutError == TimeoutError: usually a tool-internal wait_for,
    # not a network failure — must NOT trigger a whole-turn retry. Network
    # timeouts surface as httpx ReadTimeout / SDK APITimeoutError instead.
    assert not _is_transient_model_error(TimeoutError())
    ReadTimeout = type("ReadTimeout", (Exception,), {})
    assert _is_transient_model_error(ReadTimeout())


def test_status_code_errors():
    assert _is_transient_model_error(_FakeAPIStatusError(429))
    assert _is_transient_model_error(_FakeAPIStatusError(500))
    assert _is_transient_model_error(_FakeAPIStatusError(503))
    assert _is_transient_model_error(_FakeAPIStatusError(408))
    assert not _is_transient_model_error(_FakeAPIStatusError(400))
    assert not _is_transient_model_error(_FakeAPIStatusError(401))


def test_nested_cause_chain():
    # langgraph/wrapper exceptions can nest the real transport failure
    inner = APIConnectionError("Connection error.")
    outer = RuntimeError("task failed")
    try:
        try:
            raise inner
        except Exception as e:
            raise outer from e
    except RuntimeError as caught:
        assert _is_transient_model_error(caught)


def test_business_error_not_transient():
    assert not _is_transient_model_error(
        ValueError("provider custom is disabled (enable it in Settings)")
    )
    assert not _is_transient_model_error(KeyError("agent"))
