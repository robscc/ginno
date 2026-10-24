"""Runtime-update detection (P0-2)."""

from __future__ import annotations

import os
import sys
import time

import pytest

from ginno_runtime import runtime_watch

pytestmark = pytest.mark.unit


def _reset():
    runtime_watch._cache.update({"at": 0.0, "stale": False, "mtime": None})


def test_dev_mode_never_stale(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    _reset()
    assert runtime_watch.status()["runtime_stale"] is False


def test_bundle_replaced_after_startup_is_stale(tmp_path, monkeypatch):
    binfile = tmp_path / "ginno-runtime"
    binfile.write_bytes(b"x")
    # bundle mtime = now; pretend the process started an hour ago
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(binfile))
    monkeypatch.setattr(runtime_watch, "PROCESS_START", time.time() - 3600)
    _reset()
    st = runtime_watch.status()
    assert st["runtime_stale"] is True
    assert st["bundle_mtime"] == pytest.approx(os.path.getmtime(binfile), abs=1)


def test_bundle_older_than_process_is_fine(tmp_path, monkeypatch):
    binfile = tmp_path / "ginno-runtime"
    binfile.write_bytes(b"x")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(binfile))
    monkeypatch.setattr(runtime_watch, "PROCESS_START", time.time() + 60)
    _reset()
    assert runtime_watch.status()["runtime_stale"] is False
