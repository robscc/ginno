"""Playwright e2e: 历史编辑 diff 默认收起，仅最新 turn 展开（2026-11-21 用户需求）against the PACKAGED sidecar.

Scenario: two consecutive turns each perform a real file edit (scripted
tool_calls, bypass_permissions) — turn 1 write_file (new-file diff), turn 2
edit_file (modify diff). After both turns settle:

- the LATEST turn's diff is expanded by default (`.diff` visible);
- the OLDER turn's diff is collapsed (no `.diff` rendered), its title row
  still shows the +N −M badge;
- clicking the old tool row expands it on demand (`.diff` appears).

Anchors: DiffView root carries the stable ``diff`` css class; every ToolBlock
title button carries ``title="<tool> — …"``.
"""

from __future__ import annotations

import os
import socket
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
PORT = 8891


def _port_open(port: int) -> bool:
    s = socket.socket()
    s.settimeout(0.3)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _wait_health(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"sidecar on :{port} did not become healthy")


def _settings() -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "zh-CN",
        "providers": {
            "custom": {
                "enabled": True, "protocol": "openai-compatible", "name": "t",
                "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m",
                "max_tokens": 100, "temperature": 0.7, "timeout_s": 5,
            },
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


@pytest.fixture
def app(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    scripts = tmp_path / "scripts.json"
    # 4 scripted turns: tool call → summary → tool call → summary. Both edits
    # target the same file inside the session cwd (write_file then edit_file),
    # each producing a real diff via editdiff.py.
    scripts.write_text(
        json.dumps(
            [
                {"content": "", "tool_calls": [
                    {"name": "write_file", "args": {"path": "diff-demo.txt",
                     "content": "alpha\nbeta\ngamma\n"}},
                ]},
                {"content": "第一轮改好了。"},
                {"content": "", "tool_calls": [
                    {"name": "edit_file", "args": {"path": "diff-demo.txt",
                     "old": "beta", "new": "BETA-2"}},
                ]},
                {"content": "第二轮也改好了。"},
            ]
        ),
        encoding="utf-8",
    )
    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(PORT),
    )
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)
            yield page
            browser.close()
    finally:
        sc.stop_sidecar(proc)


def test_old_turn_diffs_collapse_latest_expands(app):
    page = app
    ta = page.locator("textarea").first
    ta.click(); ta.fill("第一轮"); ta.press("Enter")
    page.wait_for_timeout(3000)
    ta.click(); ta.fill("第二轮"); ta.press("Enter")
    page.wait_for_timeout(3500)
    page.reload(wait_until="load")
    page.wait_for_timeout(1800)

    edit_rows = page.locator('[title^="edit_file"], [title^="write_file"]')
    assert edit_rows.count() >= 2, f"expected ≥2 edit tool rows, got {edit_rows.count()}"

    diffs = page.locator("div.diff")
    assert diffs.count() == 1, (
        f"expected exactly 1 expanded diff (latest turn only), got {diffs.count()}"
    )

    # the collapsed (older) row still shows its +N −M badge
    body = page.locator("body").inner_text()
    assert "+3" in body, "older turn should still carry the +N badge"

    # expand the older row on demand → second .diff appears
    edit_rows.first.click()
    page.wait_for_timeout(400)
    assert page.locator("div.diff").count() == 2, "on-demand expand should reveal old diff"
