"""Packaged-UI e2e: 移除运行中成员的防呆文案（R3 巡检 Q5，CLAUDE.md 红线）。

前端交互改动必须配 packaged UI 用例——vitest/tsc 抓不到渲染层断裂。
本文件与 test_packaged_ui_tasks_overview 同 harness（source/strict 双模），
真实 sidecar + 真实 Chromium，断言打在「确认框实际渲染的文案」上：

1. **运行中成员**（deliver 起了真 turn，fake-LLM 延迟让它保持 working）
   → ⋯ 菜单 → Remove member → 确认框必须出现防呆文案：
   "force-stop its turn and delete ALL of its sessions"。
2. **空闲成员**（从未跑过 turn）→ 同样路径 → 确认框保持既有文案
   （"and all of its sessions will be deleted"），不被防呆误伤。

不真删成员（Cancel 收场）——本用例验的是确认层文案，不是删除链路
（删除链路已有 API 级覆盖）。确定性纪律：working 态用 HTTP 轮询服务端
事实，不依赖渲染回弹。
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


def _skip_or_fail(msg: str):
    if STRICT:
        pytest.fail(f"[GINNO_E2E_STRICT] {msg}")
    pytest.skip(msg)


def _require_runtime():
    try:
        import playwright.sync_api  # noqa: F401
    except Exception:
        _skip_or_fail("playwright not installed")
    if RUNTIME_MODE == "source":
        if not (_REPO / "src" / "ginno_runtime" / "server.py").exists():
            _skip_or_fail("source mode needs the repo runtime sources")
        if not (WEB_OUT / "index.html").exists():
            _skip_or_fail("source mode needs the web static export (pnpm build in apps/web)")
        if not shutil.which("uv"):
            _skip_or_fail("source mode needs uv on PATH")
        return
    if not RUNTIME_BIN.is_file():
        _skip_or_fail("packaged sidecar not built (run `make runtime`)")


def _free_port() -> int:
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
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode()) if r.status != 204 else {}


def _spawn(tmp_path: Path, port: int):
    """起 sidecar。fake LLM 剧本第一步就 ask_user → 成员 turn parked 在
    interrupt（status=blocked，等用户回答）——**稳态**，不依赖任何时序：
    DELAY 方案实测不可靠（deliver 的 turn 会绕开模型延迟秒完）。"""
    (tmp_path / "settings.json").write_text(json.dumps(_settings()), encoding="utf-8")
    scripts = json.dumps([
        {"content": "", "tool_calls": [{
            "name": "ask_user",
            "args": {"question": "Proceed with the long task?", "options": ["yes", "no"]},
        }]},
    ])
    env = dict(
        os.environ,
        GINNO_HOME=str(tmp_path),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=scripts,
        GINNO_RUNTIME_PORT=str(port),
    )
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


def _member_status(port: int, team_id: str, display_name: str) -> str:
    t = _api(port, "GET", f"/api/teams/{team_id}")
    for m in (t.get("team") or {}).get("members") or []:
        if m.get("display_name") == display_name:
            return str(m.get("status") or "?")
    return "?"


def test_remove_running_member_shows_force_stop_warning(tmp_path):
    _require_runtime()
    port = _free_port()
    proc, err_log = _spawn(tmp_path, port)
    try:
        _wait_health(port)

        # 编制用 API 造：Runner（将被派活）+ Idler（始终空闲）。
        team = _api(port, "POST", "/api/teams", {"name": "GuardTeam", "project_slug": "default"})
        assert team.get("ok"), team
        team = team["team"]
        for name in ("Runner", "Idler"):
            r = _api(port, "POST", f"/api/teams/{team['id']}/members",
                     {"agent_id": "dev", "display_name": name})
            assert r.get("ok"), r

        # 派活给 Runner —— deliver 起真 turn（fake-LLM 每步 25s）。
        r = _api(port, "POST", f"/api/teams/{team['id']}/deliver",
                 {"instance_id": _iid(port, team["id"], "Runner"), "text": "long task"})
        assert r.get("receipt") == "delivered", r

        # 服务端事实：Runner 必须 blocked（ask_user parked；HTTP 轮询，
        # 不依赖渲染）。blocked 是活 turn 的 parked 形态——移除同样会强停。
        deadline = time.time() + 30
        while time.time() < deadline:
            st = _member_status(port, team["id"], "Runner")
            if st in ("working", "blocked"):
                break
            time.sleep(0.5)
        assert st in ("working", "blocked"), f"Runner 应处于活 turn 态，实际 {st}"

        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.add_init_script("window.localStorage.setItem('ginno.language', 'en')")
            page.goto(f"http://127.0.0.1:{port}/team", wait_until="load")
            page.wait_for_timeout(2500)

            row = page.get_by_text("GuardTeam", exact=False).first
            if row.count():
                row.click()
                page.wait_for_timeout(1500)

            def _open_remove_dialog(member_name: str) -> str:
                """打开指定成员的移除确认框，返回确认框文本。"""
                # 成员行 hover 让 ⋯ 按钮可见，点开菜单再点 Remove member。
                member = page.get_by_text(member_name, exact=True).first
                member.hover()
                page.wait_for_timeout(200)
                # 该成员行内的 ⋯（菜单按钮 title="Manage member"）：
                # 用 DOM 求值从成员名节点向上找到行容器内的菜单按钮，避免
                # 依赖成员渲染顺序。
                idx = page.evaluate(
                    """(name) => {
                        const els = Array.from(document.querySelectorAll('span,div'));
                        const hit = els.find(e => (e.textContent||'').trim() === name);
                        let row = hit;
                        for (let i=0;i<8 && row;i++) {
                            row = row.parentElement;
                            if (row && row.querySelector('button[title="Manage member"]')) break;
                        }
                        return row ? Array.from(
                            document.querySelectorAll('button[title="Manage member"]')
                        ).indexOf(row.querySelector('button[title="Manage member"]')) : -1;
                    }""",
                    member_name,
                )
                assert idx >= 0, f"未找到 {member_name} 行的管理菜单按钮"
                menu_btn = page.locator('button[title="Manage member"]').nth(idx)
                menu_btn.click()
                page.get_by_text("Remove member", exact=True).click()
                dialog = page.locator('[role="dialog"]')
                dialog.wait_for(state="visible", timeout=10_000)
                return dialog.inner_text()

            # ── 1) 运行中成员：防呆文案必须出现 ──
            txt = _open_remove_dialog("Runner")
            assert "force-stop its turn" in txt, f"运行中成员缺防呆文案: {txt!r}"
            assert "cannot be undone" in txt.lower(), txt
            page.get_by_text("Cancel", exact=True).click()
            page.wait_for_timeout(300)

            # ── 2) 空闲成员：保持既有文案，不被防呆误伤 ──
            txt2 = _open_remove_dialog("Idler")
            assert "force-stop" not in txt2, f"空闲成员不该用运行中防呆文案: {txt2!r}"
            assert "all of its sessions will be deleted" in txt2, txt2
            page.get_by_text("Cancel", exact=True).click()

            browser.close()
    finally:
        sc.stop_sidecar(proc)
        err_log.close()


def _iid(port: int, team_id: str, display_name: str) -> str:
    t = _api(port, "GET", f"/api/teams/{team_id}")
    for m in (t.get("team") or {}).get("members") or []:
        if m.get("display_name") == display_name:
            return str(m["instance_id"])
    raise AssertionError(f"member {display_name} not found")
