"""Packaged-UI e2e: 超限上传必须给可见报错（R3 巡检 Q1）。

修复前：>100MB 文件后端返回 ok:false，前端 catch 只 debugLog + 芯片静默
移除——用户以为没拖上。修复后超限走两条路径，都要弹可见 error toast
（文件名+大小+上限全呈现，en catalog 文案）：

1. 本地预检：set_input_files 一个 101MB sparse 文件（accept 里的 .json），
   toast 可见且**没有** POST /api/files 发出（超限不白传一遍）。
2. 后端拒绝兜底：route mock /api/files 返回 code=file_too_large + 小文件
   ——本地常量与后端漂移时由这条路径接住。

复用 test_packaged_ui_tasks_overview 的 source/packaged harness（随机端口
+ 临时 GINNO_HOME，不碰 8787 与真实设置）；GINNO_E2E_RUNTIME=source 跑
源码 sidecar + repo 内静态导出。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e

TOO_LARGE_SNIPPET = "exceeds the 100.0 MB upload limit"


@pytest.fixture
def app(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import sync_playwright

    scripts = tmp_path / "scripts.json"
    scripts.write_text(json.dumps([{"content": "e2e-upload-session-ready"}]), encoding="utf-8")
    monkeypatch.setenv("GINNO_FAKE_LLM_SCRIPTS", str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                yield page, port
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)


def _enter_session(page, port: int) -> None:
    """发一条消息懒建会话并等回复落位（welcome 态选文件只进 pending 缓冲，
    不触发上传路径——必须先进会话）。"""
    page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
    ta = page.locator("textarea").first
    ta.wait_for(state="visible", timeout=20_000)
    ta.click()
    ta.fill("hi")
    ta.press("Enter")
    page.locator("text=e2e-upload-session-ready").first.wait_for(timeout=30_000)


def _pick_file(page, path: Path):
    """Composer 的 hidden file input（attach 按钮背后同一颗）。"""
    page.locator('input[type="file"]').first.set_input_files(str(path))


def _error_toast(page):
    return page.get_by_role("alert")


def test_oversized_local_precheck_rejects_without_upload(app, tmp_path):
    """① 本地预检：>100MB → 可见报错（名字+大小+上限），零网络流量。"""
    page, port = app
    _enter_session(page, port)

    big = tmp_path / "huge-payload.json"
    with big.open("wb") as f:
        f.truncate(101 * 1024 * 1024)  # sparse：磁盘不占，File.size 即 101MB
    posts: list[str] = []
    page.on("request", lambda r: posts.append(r.url) if r.method == "POST" and "/api/files" in r.url else None)

    _pick_file(page, big)

    toast = _error_toast(page).first
    toast.wait_for(state="visible", timeout=10_000)
    text = toast.inner_text()
    assert "huge-payload.json" in text, text
    assert "101.0 MB" in text, text
    assert TOO_LARGE_SNIPPET in text, text
    assert posts == [], f"超限文件不应发出上传请求，实际发了 {posts}"


def test_backend_too_large_code_surfaces_visible_error(app, tmp_path):
    """② 后端拒绝兜底：code=file_too_large → 同一可见文案（路由 mock）。"""
    page, port = app
    _enter_session(page, port)

    small = tmp_path / "note.json"
    small.write_text('{"k": 1}', encoding="utf-8")

    def _mock_too_large(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({
                "ok": False,
                "code": "file_too_large",
                "error": "File too large (max 100MB)",
            }),
        )

    page.route("**/api/files", _mock_too_large)
    try:
        _pick_file(page, small)
        toast = _error_toast(page).first
        toast.wait_for(state="visible", timeout=10_000)
        text = toast.inner_text()
        assert "note.json" in text, text
        assert TOO_LARGE_SNIPPET in text, text
        # 乐观芯片必须被移除（不能停在「上传中」）——toast 自身也含文件名，
        # 所以按「上传中」标记定位芯片，而不是按文件名。
        page.wait_for_timeout(300)
        assert page.locator("text=Uploading").count() == 0
    finally:
        page.unroute("**/api/files")
