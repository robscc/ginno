"""Packaged-UI e2e: 设置 → 诊断 → 配置健康检查（Config Doctor）。

CLAUDE.md 红线：前端交互改动必须配 packaged UI 用例。vitest（doctor.test.ts +
api/sessions.test.ts）守纯逻辑——分组/计数/修复映射/i18n/降级 union；这里守
渲染层交互：

覆盖诊断页的 Doctor 交互：模拟旧 runtime 的 404，检查降级提示与页面可用性，
再重试并读取真包的检查结果。固定契约体覆盖分组、徽标、折叠和明细。
"""

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


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_health(port: int, timeout: float = 40) -> None:
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


def test_packaged_ui_doctor_degrades_gracefully(tmp_path):
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import expect, sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    port = _free_port()

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(port))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(locale="en-US")
            page.goto(f"http://127.0.0.1:{port}/settings/diagnostics", wait_until="load")

            panel = page.get_by_test_id("doctor-panel")
            expect(panel.get_by_role("heading", name="Config Doctor")).to_be_visible()
            run_btn = panel.get_by_test_id("doctor-run")
            expect(run_btn).to_be_enabled()

            doctor_url = f"http://127.0.0.1:{port}/api/doctor"

            def is_doctor_get(response):
                return response.url == doctor_url and response.request.method == "GET"

            # 在 HTTP 边界模拟旧 runtime 缺少端点，页面及其余 API 仍由真包提供。
            page.route(doctor_url, lambda route: route.fulfill(status=404))
            with page.expect_response(is_doctor_get) as failed:
                run_btn.click()
            assert failed.value.status == 404
            unavailable = panel.get_by_test_id("doctor-unavailable")
            expect(unavailable).to_contain_text("Engine unavailable")
            expect(page.get_by_role("heading", name="Diagnostics", exact=True)).to_be_visible()
            result = panel.get_by_test_id("doctor-result")
            expect(result).to_have_count(0)
            expect(run_btn).to_be_enabled()

            page.unroute(doctor_url)
            with page.expect_response(is_doctor_get) as recovered:
                run_btn.click()
            assert recovered.value.status == 200
            expect(result).to_be_visible()
            expect(unavailable).to_have_count(0)
            expect(run_btn).to_be_enabled()

            browser.close()
    finally:
        sc.stop_sidecar(proc)


def test_packaged_ui_doctor_renders_contract(tmp_path):
    """用固定的 HTTP 契约体验证分组、徽标、折叠和明细渲染。"""
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    port = _free_port()

    contract = {
        "ok": True,
        "ran_at": "2026-11-19T02:04:05+00:00",
        "summary": {"error": 2, "warn": 1, "total": 15},
        "checks": [
            {
                "surface": "mcp",
                "id": "mcp.command-missing",
                "level": "error",
                "items": [
                    {"name": "trending", "detail": "npx: command not found"},
                    {"name": "filesystem", "detail": "uvx: command not found"},
                ],
            },
            {"surface": "mcp", "id": "mcp.empty-config", "level": "ok", "items": []},
            {
                "surface": "skills",
                "id": "skills.frontmatter",
                "level": "warn",
                "items": [{"name": "foo/SKILL.md", "detail": "missing description"}],
            },
            {"surface": "runtime", "id": "runtime.homebrew-path", "level": "error", "items": []},
            {"surface": "hooks", "id": "hooks.script-missing", "level": "ok", "items": []},
        ],
    }

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(port))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(locale="en-US")
            page.route(
                "**/api/doctor",
                lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(contract)),
            )
            page.goto(f"http://127.0.0.1:{port}/settings/diagnostics", wait_until="load")
            page.locator('[data-testid="doctor-panel"]').wait_for(timeout=15000)
            page.locator('[data-testid="doctor-run"]').click()

            result = page.locator('[data-testid="doctor-result"]')
            result.wait_for(timeout=10000)
            text = result.inner_text()

            # ── summary 徽标（error 红 / warn 琥珀 / total）──
            assert "2 error(s)" in text and "1 warning(s)" in text and "15 total" in text

            # ── 按 surface 分组（固定顺序），组标题带计数 ──
            assert text.index("MCP servers") < text.index("Skills") < text.index("Hooks") < text.index("Runtime")
            assert "1 error(s)" in text.split("Skills")[0]  # MCP 组计数

            # ── ok 折叠为「✓ N 项通过」；error check 展开 items（name + detail）──
            assert "✓ 1 passed" in text
            assert "trending" in text and "npx: command not found" in text
            assert "foo/SKILL.md" in text and "missing description" in text

            # ── 空 items 的 error check 走「无明细」分支，不白屏 ──
            assert "(no details)" in text

            # ── 修复提示映射（check id → i18n 文案）+ ran_at ──
            assert "absolute path" in text  # mcp.command-missing 英文修复提示
            assert "Ran at" in page.locator('[data-testid="doctor-ran-at"]').inner_text()

            # ── level 徽标三色类（error / warn 卡片存在）──
            assert page.locator('[data-testid="doctor-result"] .bg-red\\/15').count() >= 1
            assert page.locator('[data-testid="doctor-result"] .bg-yellow\\/15').count() >= 1

            browser.close()
    finally:
        sc.stop_sidecar(proc)
