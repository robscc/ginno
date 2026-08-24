"""System-proxy switch (Settings → 模型 API → 使用系统代理).

Follow-up to the 2026-08-19 502 incident: honouring the OS proxy is now a
user-visible global switch (settings.json top-level ``use_system_proxy``,
default on; loopback bypass stays unconditional in ``__init__``). OFF swaps
``httpx._client.get_environment_proxies`` so newly built httpx clients
(everything under the anthropic/openai SDKs) get no OS/env proxy mounts;
ON restores the original. Toggling must also evict cached LLM clients,
which freeze their proxy map at init (``_SESSIONS`` + langchain_anthropic's
shared lru_cache'd httpx clients).
"""

from __future__ import annotations

import httpx
import httpx._client as _hx_client
import httpx._utils as _hx_utils
import pytest

from ginno_runtime import providers

pytestmark = pytest.mark.unit

_PROXY_ENV = {"HTTPS_PROXY": "http://proxy.example.com:8080"}


@pytest.fixture(autouse=True)
def _restore_proxy_mode():
    """apply_system_proxy mutates process-global httpx state — always
    restore the default (ON) mode, whatever the test did."""
    yield
    providers.apply_system_proxy(True)


def test_default_true_and_flag_reads():
    assert providers.use_system_proxy({}) is True
    assert providers.use_system_proxy({"use_system_proxy": True}) is True
    assert providers.use_system_proxy({"use_system_proxy": False}) is False


def test_off_new_clients_get_no_proxy_mounts(monkeypatch):
    for k, v in _PROXY_ENV.items():
        monkeypatch.setenv(k, v)
    providers.apply_system_proxy(False)
    assert _hx_client.get_environment_proxies() == {}
    c = httpx.Client(trust_env=True)
    try:
        assert c._mounts == {}  # env proxy ignored — direct connection
    finally:
        c.close()


def test_on_restores_env_proxy_mounts(monkeypatch):
    for k, v in _PROXY_ENV.items():
        monkeypatch.setenv(k, v)
    providers.apply_system_proxy(False)
    providers.apply_system_proxy(True)
    assert _hx_client.get_environment_proxies is _hx_utils.get_environment_proxies
    c = httpx.Client(trust_env=True)
    try:
        assert c._mounts  # HTTPS_PROXY honoured again
    finally:
        c.close()


def test_apply_clears_langchain_anthropic_shared_clients():
    """langchain_anthropic shares ONE lru_cache'd httpx client across all
    ChatAnthropic instances; a toggle that didn't drop it would leave every
    model on the stale proxy map."""
    from langchain_anthropic import _client_utils as cu

    cu._get_default_httpx_client(base_url="http://127.0.0.1:9", timeout=60.0)
    cu._get_default_async_httpx_client(base_url="http://127.0.0.1:9", timeout=60.0)
    assert cu._get_default_httpx_client.cache_info().currsize == 1
    assert cu._get_default_async_httpx_client.cache_info().currsize == 1

    providers.apply_system_proxy(False)
    assert cu._get_default_httpx_client.cache_info().currsize == 0
    assert cu._get_default_async_httpx_client.cache_info().currsize == 0


def test_put_settings_toggles_and_evicts_sessions(client):
    """PUT /api/settings applies a changed flag process-wide and drops the
    cached session graphs (their LLM clients froze the old proxy map)."""
    from ginno_runtime import server_shared

    r = client.put("/api/settings", json={"use_system_proxy": False})
    assert r.json()["ok"] is True
    assert _hx_client.get_environment_proxies() == {}

    server_shared._SESSIONS["s1"] = object()
    r = client.put("/api/settings", json={"use_system_proxy": True})
    assert r.json()["ok"] is True
    assert _hx_client.get_environment_proxies is _hx_utils.get_environment_proxies
    assert "s1" not in server_shared._SESSIONS  # evicted on change


def test_put_settings_unchanged_flag_keeps_sessions(client):
    """No flag change → no eviction (PUTs from other settings panels must
    not blow away live sessions)."""
    from ginno_runtime import server_shared

    server_shared._SESSIONS["s1"] = object()
    r = client.put("/api/settings", json={"unrelated": 1})
    assert r.json()["ok"] is True
    assert "s1" in server_shared._SESSIONS
