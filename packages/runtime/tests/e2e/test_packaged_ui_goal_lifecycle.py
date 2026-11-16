"""Packaged-UI e2e: goal lifecycle finish（CLAUDE.md 红线：前端交互改动必须
配 packaged UI 用例——这里守 GoalChip 的 archived 收口渲染）。

覆盖（2026-11-16）：
1. complete goal 的 chip 可见（data-status=complete）
2. 任意 turn 正常结束 → complete goal 自动归档：GET /goal 返回 null、
   goals-history.jsonl 追加完整记录（含用量）
3. 前端 goal.archived 事件 → chip 消失，回到「设置目标」按钮
4. 未完成（active）goal 不被归档（turn 结束后仍在）

数据面走捷径：goal 快照直写 store 落盘路径，不依赖 fake LLM 调 goal_update
（工具面有 test_goal_tools.py 单测覆盖）；本用例验 turn 终态钩子 + 渲染收口。
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
PORT = 8902


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


def _api(port: int, path: str, payload: dict | None = None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _goal(status: str, objective: str) -> dict:
    now = time.time()
    return {
        "goal_id": "g-" + status,
        "objective": objective,
        "status": status,
        "time_used_seconds": 12.5,
        "turns_used": 2,
        "agent_id": "dev",
        "created_at": now,
        "updated_at": now,
    }


def test_packaged_ui_goal_auto_archive(tmp_path):
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
        sess = _api(PORT, "/api/sessions", {
            "project_slug": "default",
            "workspace": str(tmp_path),
            "title": "Goal Lifecycle E2E",
        })
        sid = sess.get("session_id") or sess.get("id")
        assert sid, sess

        goals_path = home / "projects" / "default" / "goals.json"
        goals_path.parent.mkdir(parents=True, exist_ok=True)
        goals_path.write_text(json.dumps({sid: _goal("complete", "archive me after turn")}))

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(2000)
            page.get_by_text("Goal Lifecycle E2E", exact=False).first.click()

            # ── 1. complete chip 可见 ──
            chip = page.locator('[data-testid="goal-chip"][data-status="complete"]')
            chip.wait_for(timeout=10000)

            # ── 2. 跑一个 turn（fake LLM 即答）→ 终态钩子归档 ──
            ta = page.locator("textarea").first
            ta.fill("hello lifecycle")
            ta.press("Enter")

            # GET /goal 变 null（归档完成）——轮询等待，不裸查一次
            deadline = time.time() + 20
            archived = False
            while time.time() < deadline:
                r = _api(PORT, f"/api/sessions/{sid}/goal")
                if r.get("ok") and r.get("goal") is None:
                    archived = True
                    break
                time.sleep(0.5)
            assert archived, "complete goal must auto-archive at turn end"

            # history 追加完整记录（含用量）
            hp = goals_path.with_name("goals-history.jsonl")
            assert hp.exists(), "goals-history.jsonl must be written"
            rec = json.loads(hp.read_text().strip().splitlines()[-1])
            assert rec["objective"] == "archive me after turn"
            assert rec["turns_used"] == 2
            assert rec["time_used_seconds"] == 12.5
            assert rec["archived_at"] > 0

            # ── 3. 前端 chip 消失，回到设置按钮 ──
            page.locator('[data-testid="goal-set-button"]').wait_for(timeout=10000)
            assert page.locator('[data-testid="goal-chip"]').count() == 0

            browser.close()

        # ── 4. active goal 不被归档（新会话，跑 turn 后仍在）──
        sess2 = _api(PORT, "/api/sessions", {
            "project_slug": "default",
            "workspace": str(tmp_path),
            "title": "Goal Active E2E",
        })
        sid2 = sess2.get("session_id") or sess2.get("id")
        goals = json.loads(goals_path.read_text())
        goals[sid2] = _goal("active", "still open, do not archive")
        goals_path.write_text(json.dumps(goals))
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)
            page.get_by_text("Goal Active E2E", exact=False).first.click()
            page.locator('[data-testid="goal-chip"][data-status="active"]').wait_for(timeout=10000)
            ta = page.locator("textarea").first
            ta.fill("hello active")
            ta.press("Enter")
            page.wait_for_timeout(4000)
            r = _api(PORT, f"/api/sessions/{sid2}/goal")
            assert r.get("goal") and r["goal"]["status"] == "active", (
                "an open goal must survive turn end"
            )
            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()