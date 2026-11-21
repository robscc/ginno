"""Playwright e2e: 编辑 diff 显示（2026-11-21 P0/P1）在 REAL browser 对
PACKAGED sidecar 的端到端验证——scripted turn 让 agent 跑一次 edit_file，
断言工具气泡：① 标题行 +N −M 红绿徽标；② 展开态 unified diff 行渲染
（红删绿增）；③ hunk 头可见。

确定性判据：徽标/差分行是渲染事实（不依赖视觉回弹）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import _sidecar as sc
from test_packaged_ui_ask_user import (  # noqa: F401 — shared boot helpers
    _send,
    RUNTIME_BIN,
    _launch_chromium,
    _port_open,
    _settings,
    _wait_health,
)

PORT = 8893


def _edit_scripts(path: Path, target_rel: str, old: str, new: str) -> None:
    """One scripted turn: edit_file → done. The fake model emits the tool
    call; the REAL builtin edit_file runs against the fixture workspace."""
    path.write_text(
        json.dumps(
            [
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "args": {"path": "demo.py", "content": "x=1\ny=2\n"},
                        }
                    ],
                },
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "edit_file",
                            "args": {"path": "demo.py", "old": "y=2", "new": "y=42"},
                        }
                    ],
                },
                {"content": "改好了。"},
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def app(tmp_path, request):
    port = request.param
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(port):
        pytest.skip(f"port {port} already in use")

    # tmp_path（/private/var/folders）会撞路径守卫的 deny 区——用 /tmp
    home = Path("/tmp/ginno-editdiff-e2e") / f"home-{port}"
    import shutil
    shutil.rmtree(home, ignore_errors=True)
    home.mkdir(parents=True)
    (home / "settings.json").write_text(json.dumps(_settings()))
    scripts = tmp_path / "scripts.json"
    _edit_scripts(scripts, "", "", "")

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(port),
    )
    _err = open("/tmp/editdiff_sidecar_err.log", "w")
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env, err=_err)
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            yield page, home
            browser.close()
    finally:
        sc.stop_sidecar(proc)


@pytest.mark.parametrize("app", [PORT], indirect=True)
def test_edit_diff_renders(app):
    page, _home = app
    # UI locale 为 en（packaged e2e 惯例）——气泡 label 是 i18n 标签
    # "Editing file"，非工具名 edit_file。
    _send(page, "fix demo.py please")
    page.wait_for_selector("text=Editing file", timeout=30_000)
    # 徽标：标题行 +1 −1（红绿两个计数 span）
    page.locator("span.font-semibold >> text=+1").first.wait_for(timeout=15_000)
    page.locator("span.font-semibold >> text=−1").first.wait_for(timeout=15_000)
    # 气泡默认展开（title=—Collapse）——无需点击，直接断言 diff 行
    page.wait_for_selector("text=@@ -1 +1 @@", timeout=10_000)
    page.wait_for_selector("text=−y=2", timeout=10_000)
    page.wait_for_selector("text=+y=42", timeout=10_000)
