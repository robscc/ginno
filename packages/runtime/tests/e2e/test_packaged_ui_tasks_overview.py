"""Packaged-UI e2e: 运行中任务统一面板（tasks-overview，2026-11-21）。

两条真实 UI 流（真实 sidecar、真实 Chromium、无 mock——断言打在「UI 实际发出的
请求体」与「服务端最终状态」两层）：

1. 调度暂停：API 建一条 interval 调度任务 → UI「运行」tab 出现该行 →
   点「暂停」→ 薄确认（role=dialog，标题=Pause this schedule?）→
   捕获 POST /api/tasks/overview/stop 请求体（kind=schedule、id 正确、200）→
   服务端终态：GET /api/tasks/overview counts.scheduled==0 且任务 enabled=false。
2. 清单取消：API 建会话 + ``plan_store.create_plan``（生产写路径，落盘同一
   GINNO_HOME）→ UI「子代理」组出现该清单 → 点「取消」→ 薄确认
   （标题=Cancel this subagent task?）→ 请求体 kind=plan 且带 session_id →
   服务端终态：plan.status==cancelled、条目 stopped、counts.subagents==0。

确定性纪律（替代固定 sleep）：
* UI 等待一律 wait_for / expect_request——无 wait_for_timeout；
* 服务端终态用 HTTP 复核，不依赖渲染回弹。

运行模式（GINNO_E2E_RUNTIME）：
* ``packaged``（默认）：spawn dist PyInstaller bundle（与桌面版同路径）；
* ``source``：spawn ``uv run --no-sync ginno-runtime``（源码 sidecar +
  repo 内 apps/web/out 静态导出）——Linux CI 冒烟用，省 macOS 分钟。

防假绿：GINNO_E2E_STRICT=1 时，环境不满足（无 bundle / 无导出产物）从
skip 变 fail——CI 冒烟 job 用它保证「跑了」而不是「跳了也算绿」。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_REPO = Path(__file__).resolve().parents[2]  # packages/runtime
_RUNTIME_DIST = _REPO / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime"
WEB_OUT = _REPO.parents[1] / "apps" / "web" / "out"

RUNTIME_MODE = os.environ.get("GINNO_E2E_RUNTIME", "packaged").strip().lower()
STRICT = os.environ.get("GINNO_E2E_STRICT") == "1"

# 会话/清单用的 project slug（GINNO_HOME 整个是 tmp_path，天然隔离）。
SLUG = "overview-smoke"


def _skip_or_fail(msg: str):
    """CI（STRICT=1）里环境缺失是故障必须红；本地无 bundle 时保留 skip 习惯。"""
    if STRICT:
        pytest.fail(f"[GINNO_E2E_STRICT] {msg}")
    pytest.skip(msg)


def _require_runtime():
    """前置检查：runtime 可执行体 / 静态导出 / playwright 就位。"""
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        _skip_or_fail("playwright not installed")
    if RUNTIME_MODE == "source":
        if not (_REPO / "src" / "ginno_runtime" / "server.py").exists():
            _skip_or_fail("source mode needs the repo runtime sources")
        if not (WEB_OUT / "index.html").exists():
            _skip_or_fail(
                "source mode needs the web static export (pnpm build in apps/web)"
            )
        if not shutil.which("uv"):
            _skip_or_fail("source mode needs uv on PATH")
        return
    if not RUNTIME_BIN.is_file():
        # is_file 而非 exists：半截构建留下的 dist/ginno-runtime/ 目录不算数
        _skip_or_fail("packaged sidecar not built (run `make runtime`)")


def _free_port() -> int:
    """隔离端口：向内核要一个当前空闲的 ephemeral port，绝不硬编码。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_health(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.4)
    raise RuntimeError(f"sidecar on :{port} did not become healthy in {timeout}s")


def _settings() -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "en",
        "providers": {
            "anthropic": {"enabled": False, "protocol": "anthropic", "api_key": "", "default_model": "x", "base_url": "", "max_tokens": 1, "temperature": 0.7, "timeout_s": 5},
            "openai": {"enabled": False, "protocol": "openai", "api_key": "", "default_model": "x", "base_url": "", "org_id": "", "max_tokens": 1},
            "custom": {"enabled": True, "protocol": "openai-compatible", "name": "t", "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m", "max_tokens": 100, "temperature": 0.7, "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _api(port: int, method: str, path: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode()) if r.status != 204 else {}


def _spawn(tmp_path: Path, port: int, *, fake_llm: bool = True):
    """起 sidecar（packaged bundle 或源码），返回 (proc, stderr 日志句柄)。

    GINNO_HOME 直接用 tmp_path——与根 conftest 的 isolated_home 同一目录，
    测试进程内 plan_store 写盘 = sidecar 读盘，天然同源。
    """
    (tmp_path / "settings.json").write_text(json.dumps(_settings()), encoding="utf-8")
    env = dict(os.environ, GINNO_HOME=str(tmp_path), GINNO_RUNTIME_PORT=str(port))
    if fake_llm:
        env["GINNO_FAKE_LLM"] = "1"
    else:
        env.pop("GINNO_FAKE_LLM", None)
    err_log = (tmp_path / "sidecar-stderr.log").open("wb")
    if RUNTIME_MODE == "source":
        proc = subprocess.Popen(
            ["uv", "run", "--no-sync", "ginno-runtime"],
            cwd=str(_REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=err_log,
            start_new_session=True,
        )
        return proc, err_log
    return sc.spawn_sidecar(str(RUNTIME_BIN), env, err=err_log), err_log


def _stop(proc, err_log) -> None:
    try:
        sc.stop_sidecar(proc)
    finally:
        err_log.close()


def _open_running_tab(page) -> None:
    """打开右栏「运行」tab（role=tab / aria-label 含 running），等面板挂载。"""
    page.locator('[role="tab"]').first.wait_for(state="visible", timeout=20_000)
    tabs = page.locator('[role="tab"]')
    target = None
    for i in range(tabs.count()):
        label = (tabs.nth(i).get_attribute("aria-label") or "").lower()
        if "running" in label or "运行" in label:
            target = tabs.nth(i)
            break
    assert target is not None, f"未找到「运行」tab（tabs={tabs.count()}）"
    target.click()
    page.locator('[data-testid="tasks-overview-panel"]').wait_for(
        state="visible", timeout=10_000
    )


def _new_page(pw):
    browser = pw.chromium.launch()
    page = browser.new_page()
    # 钉死 en 文案（断言薄确认标题/按钮文案用；与 settings language 双保险）。
    page.add_init_script("window.localStorage.setItem('ginno.language', 'en')")
    return browser, page


def _is_stop_request(r) -> bool:
    """expect_request 谓词（Request：有 method）。"""
    return r.method == "POST" and r.url.endswith("/api/tasks/overview/stop")


def _is_stop_url(r) -> bool:
    """expect_response 谓词（Response：无 method，只看 URL）。"""
    return r.url.endswith("/api/tasks/overview/stop")


def test_packaged_ui_tasks_overview_pause_schedule(tmp_path):
    """调度暂停：UI 停止 → 请求体正确 → 服务端任务 enabled=false。"""
    _require_runtime()
    port = _free_port()
    proc, err_log = _spawn(tmp_path, port)
    try:
        _wait_health(port)
        # interval 60min：next fire 在一小时外，测试生命周期内绝不真的触发
        # （daily 08:30 这类会撞 catch-up 补跑窗口，制造噪音）。
        task = _api(port, "POST", "/api/schedule/tasks", {
            "name": "e2e overview pause",
            "target": {"type": "prompt", "prompt": "hi"},
            "schedule": {"kind": "interval", "minutes": 60},
        })
        tid = task.get("id")
        assert tid, task

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser, page = _new_page(pw)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                _open_running_tab(page)

                item = page.locator(
                    f'[data-testid="tasks-overview-item-schedule"][data-handle-id="{tid}"]'
                )
                item.wait_for(state="visible", timeout=10_000)
                assert "e2e overview pause" in item.inner_text()
                count = page.locator('[data-testid="tasks-overview-count-schedule"]')
                assert count.inner_text().strip() == "1"

                # 停止（暂停）→ 薄确认（schedule 的确认语义是可恢复 pause）
                item.locator('[data-testid="tasks-overview-stop-schedule"]').click()
                dialog = page.locator('[role="dialog"]')
                dialog.wait_for(state="visible", timeout=5_000)
                assert page.get_by_role("dialog", name="Pause this schedule?").is_visible()
                confirm_btn = dialog.locator("button").last
                assert confirm_btn.inner_text().strip() == "Pause"

                with page.expect_request(_is_stop_request, timeout=10_000) as ri, \
                        page.expect_response(_is_stop_url, timeout=10_000) as rs:
                    confirm_btn.click()
                req, resp = ri.value, rs.value
                body = req.post_data_json
                assert body.get("kind") == "schedule", body
                assert body.get("id") == tid, body
                assert resp.status == 200, resp.status

                # UI：乐观移除 → 空态（无固定 sleep，事件驱动）
                item.wait_for(state="detached", timeout=10_000)
                page.locator('[data-testid="tasks-overview-empty"]').wait_for(
                    state="visible", timeout=10_000
                )
            finally:
                browser.close()

        # 服务端终态复核（独立于浏览器）
        ov = _api(port, "GET", "/api/tasks/overview")
        assert ov["counts"]["scheduled"] == 0, ov["counts"]
        sched = _api(port, "GET", "/api/schedule")
        mine = next(t for t in sched["tasks"] if t["id"] == tid)
        assert mine["enabled"] is False, mine
        assert not mine.get("next_run_at"), mine
        assert sched["enabled"] is True, "任务级暂停不得误关全局调度"
    finally:
        _stop(proc, err_log)


def test_packaged_ui_tasks_overview_cancel_plan(tmp_path):
    """清单取消：UI 取消 → 请求体带 session_id → 服务端 plan=cancelled。"""
    _require_runtime()
    port = _free_port()
    proc, err_log = _spawn(tmp_path, port)
    try:
        _wait_health(port)
        sess = _api(port, "POST", "/api/sessions", {
            "project_slug": SLUG,
            "workspace": str(tmp_path / "ws"),
            "title": "e2e overview plan",
        })
        sid = sess.get("session_id") or sess.get("id")
        assert sid, sess
        # 生产写路径建清单（GINNO_HOME 由根 conftest 指向同一 tmp_path）；
        # 不走 LLM 工具链——watcher 只在 in-process notify_change 时调度，
        # 文件落盘的 open 清单保持静止，恰好是「子代理在编排中」的面板语义。
        from ginno_runtime.plans import store as plan_store

        plan = plan_store.create_plan(
            sid, slug=SLUG, title="e2e overview plan",
            items=[{"goal": "step one"}],
        )
        pid = plan["plan_id"]
        assert plan["status"] == "active", plan

        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser, page = _new_page(pw)
            try:
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                _open_running_tab(page)

                item = page.locator(
                    f'[data-testid="tasks-overview-item-plan"][data-handle-id="{pid}"]'
                )
                item.wait_for(state="visible", timeout=10_000)
                assert "e2e overview plan" in item.inner_text()
                assert "1 items" in item.inner_text(), item.inner_text()
                count = page.locator('[data-testid="tasks-overview-count-plan"]')
                assert count.inner_text().strip() == "1"

                # 取消 → 薄确认（plan 是不可撤销语义）
                item.locator('[data-testid="tasks-overview-stop-plan"]').click()
                dialog = page.locator('[role="dialog"]')
                dialog.wait_for(state="visible", timeout=5_000)
                assert page.get_by_role("dialog", name="Cancel this subagent task?").is_visible()
                confirm_btn = dialog.locator("button").last
                assert confirm_btn.inner_text().strip() == "Cancel"

                with page.expect_request(_is_stop_request, timeout=10_000) as ri, \
                        page.expect_response(_is_stop_url, timeout=10_000) as rs:
                    confirm_btn.click()
                req, resp = ri.value, rs.value
                body = req.post_data_json
                assert body.get("kind") == "plan", body
                assert body.get("id") == pid, body
                # 后端 plan 分支硬性要求 session_id（400 兜底），枚举句柄带、前端回传
                assert body.get("session_id") == sid, body
                assert resp.status == 200, (
                    f"stop facade returned {resp.status}: {resp.text()}"
                )

                item.wait_for(state="detached", timeout=10_000)
            finally:
                browser.close()

        # 服务端终态复核（独立于浏览器）：清单已取消、未启动条目 stopped
        ov = _api(port, "GET", "/api/tasks/overview")
        assert ov["counts"]["subagents"] == 0, ov["counts"]
        p = _api(port, "GET", f"/api/sessions/{sid}/plans/{pid}")
        assert p.get("status") == "cancelled", p
        assert all(it["status"] == "stopped" for it in p.get("items") or []), p
    finally:
        _stop(proc, err_log)
