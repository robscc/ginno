"""Packaged-UI e2e: 设置 → 诊断 → 配置健康检查（Config Doctor）。

CLAUDE.md 红线：前端交互改动必须配 packaged UI 用例。vitest（doctor.test.ts +
api/sessions.test.ts）守纯逻辑——分组/计数/修复映射/i18n/降级 union；这里守
渲染层交互：

覆盖（2026-11-19）：
1. 诊断页出现「配置健康检查」区（doctor-panel）与运行按钮；
2. 点击运行 → 旧 runtime（未实现 GET /api/doctor，当前 main 构建）返回 404
   → 优雅降级为「引擎不可用」提示（doctor-unavailable），不白屏、
   页面其余部分（标题/体检按钮）仍在；
3. 降级后按钮回到可点状态（可重试）。

正向路径（契约体渲染五面分组/三色徽标）需要后端 /api/doctor 合入后再补；
届时 mock 响应由 dist sidecar 提供，本文件的降级用例继续有效（真实场景：
旧 runtime 配新前端）。
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
PORT = 8907


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
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/settings/diagnostics", wait_until="load")

            # ── 1. 配置健康检查区 + 运行按钮出现 ──
            panel = page.locator('[data-testid="doctor-panel"]')
            panel.wait_for(timeout=15000)
            assert "Config Doctor" in panel.inner_text()
            run_btn = page.locator('[data-testid="doctor-run"]')
            assert run_btn.is_enabled()

            # ── 2. 点击运行：旧 runtime 404 → 优雅降级（不白屏）──
            run_btn.click()
            unavailable = page.locator('[data-testid="doctor-unavailable"]')
            unavailable.wait_for(timeout=10000)
            assert "Engine unavailable" in unavailable.inner_text()

            # 页面其余部分仍在（降级不拖垮整页）
            assert "Diagnostics" in page.locator("h2").first.inner_text()

            # ── 3. 降级后按钮恢复可点（可重试）──
            page.wait_for_timeout(200)
            assert run_btn.is_enabled()

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def test_packaged_ui_doctor_renders_contract(tmp_path):
    """正向路径：后端 /api/doctor 未合入 main，用 playwright route 拦截回填
    契约体（goal 固定契约），在真 Chromium 里验证分组/徽标/折叠/明细渲染。
    后端合入后本用例继续有效（拦截优先于真实端点，mock 数据稳定）。"""
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

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

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.route(
                "**/api/doctor",
                lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps(contract)),
            )
            page.goto(f"http://127.0.0.1:{PORT}/settings/diagnostics", wait_until="load")
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
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()
