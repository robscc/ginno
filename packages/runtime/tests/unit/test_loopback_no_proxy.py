"""Loopback no_proxy seeding (2026-08-19 502 incident).

httpx reads the macOS system proxy (getproxies) but ignores the system
exception list, so local provider base_urls got routed through Clash and
502'd. ginno_runtime's import seeds no_proxy with loopback hosts.
"""

from __future__ import annotations

import os

import ginno_runtime


def test_import_seeds_loopback_hosts():
    # Package already imported by test collection -> env must carry the bypass
    assert "127.0.0.1" in os.environ.get("no_proxy", "")
    assert "localhost" in os.environ.get("no_proxy", "")
    assert "127.0.0.1" in os.environ.get("NO_PROXY", "")


def test_seed_merges_over_existing_and_is_idempotent(monkeypatch):
    monkeypatch.setenv("no_proxy", "corp.example.com")
    monkeypatch.setenv("NO_PROXY", "")
    ginno_runtime._ensure_loopback_no_proxy()
    val = os.environ["no_proxy"]
    assert val.startswith("corp.example.com")  # existing entries preserved
    assert "127.0.0.1" in val and "localhost" in val and "::1" in val
    before_no, before_NO = os.environ["no_proxy"], os.environ["NO_PROXY"]
    ginno_runtime._ensure_loopback_no_proxy()
    assert os.environ["no_proxy"] == before_no  # idempotent
    assert os.environ["NO_PROXY"] == before_NO


def test_seed_noop_when_already_present(monkeypatch):
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost,::1")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost,::1")
    ginno_runtime._ensure_loopback_no_proxy()
    assert os.environ["no_proxy"] == "127.0.0.1,localhost,::1"
    assert os.environ["NO_PROXY"] == "127.0.0.1,localhost,::1"
