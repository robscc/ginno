"""Verify recovery decisions in a browser across two isolated Sidecar processes."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

RUNTIME_DIR = Path(__file__).resolve().parents[2]
RUNTIME_BIN = RUNTIME_DIR / "dist" / "ginno-runtime" / "ginno-runtime"
WEB_OUT = RUNTIME_DIR.parents[1] / "apps" / "web" / "out"
SOURCE = os.environ.get("GINNO_E2E_RUNTIME") == "source"

HEAD_TEXT = "Summarize the quarterly report into slides"
USER_MSG = "quarterly report slides"

def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _wait_health(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.3)
    raise RuntimeError(f"sidecar on :{port} did not become healthy")


def _api(port: int, path: str, body: dict | None = None) -> dict | list:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


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


def _env(home: Path, port: int) -> dict:
    return dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_RUNTIME_PORT=str(port),
    )


def _seed_lastwords(home: Path, sid: str) -> None:
    """Seed the persisted snapshot and its background task references."""
    now = int(time.time())
    tasks = home / "tasks"
    tasks.mkdir(parents=True, exist_ok=True)
    (tasks / "index.json").write_text(json.dumps({
        "t-dead-1": {"id": "t-dead-1", "status": "interrupted", "origin_session": sid,
                      "command": "sleep 999", "title": "tunnel"},
        "t-dead-2": {"id": "t-dead-2", "status": "interrupted", "origin_session": sid,
                      "command": "sleep 999", "title": "ci watch"},
    }))
    lw = home / "lastwords"
    lw.mkdir(parents=True, exist_ok=True)
    snap = {
        "session_id": sid,
        "project": "default",
        "turn_id": "interrupted-test-turn",
        "registered_turn_id": "interrupted-test-turn",
        "reason": "signal",
        "user_request_head": HEAD_TEXT,
        "last_agent_message": "working on the slides…",
        "running_tools": [],
        "bg_tasks": [],
        "bg_interrupted": ["t-dead-1", "t-dead-2"],
        "ts": now,
        "effort": {"tool_calls": 6, "started_at": now - 420},
    }
    (lw / f"{sid}.json").write_text(json.dumps(snap))


def _require_runtime() -> None:
    missing = ""
    if SOURCE and not (WEB_OUT / "index.html").is_file():
        missing = "source mode needs the web static export"
    elif not SOURCE and not RUNTIME_BIN.is_file():
        missing = "packaged sidecar not built (run make runtime)"
    if missing:
        if os.environ.get("GINNO_E2E_STRICT") == "1":
            pytest.fail(missing)
        pytest.skip(missing)
    pytest.importorskip("playwright.sync_api")


def _spawn(home: Path, port: int, log_name: str):
    env = _env(home, port)
    with (home / log_name).open("wb") as log:
        if SOURCE:
            return subprocess.Popen(
                [sys.executable, "-m", "ginno_runtime"],
                cwd=RUNTIME_DIR,
                env={**env, "PYTHONPATH": str(RUNTIME_DIR / "src")},
                stdout=subprocess.DEVNULL,
                stderr=log,
                start_new_session=True,
            )
        return sc.spawn_sidecar(str(RUNTIME_BIN), env, err=log)


def _wait_pending(port: int, sid: str) -> dict:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        data = _api(port, "/api/lastwords/pending")
        for item in data["pending"]:
            if item["session_id"] == sid:
                return item
        time.sleep(0.1)
    raise AssertionError("startup did not register the recovery")


def _run_decision_flow(tmp_path, decision: str) -> None:
    """decision: 'resume' | 'dismiss'。两条链路共享同一套重启编排。"""
    _require_runtime()
    from playwright.sync_api import expect, sync_playwright

    port = _free_port()

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    # ── 阶段一：起 sidecar #1，造一个真实会话 ──────────────────────────────
    proc = _spawn(home, port, "first-stderr.log")
    sid = None
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_timeout(1500)
            ta = page.locator("textarea").first
            ta.click()
            ta.fill(USER_MSG)
            ta.press("Enter")
            expect(page.locator(".nav-item.group", has_text=USER_MSG).first).to_be_visible(timeout=15_000)
            sessions = _api(port, "/api/sessions")
            assert isinstance(sessions, list) and len(sessions) >= 1
            sid = str(sessions[0].get("session_id") or sessions[0].get("id"))
            assert sid
            browser.close()
    finally:
        sc.stop_sidecar(proc)

    # ── 阶段二：落盘遗言 + interrupted 任务，重启 sidecar #2 ───────────────
    _seed_lastwords(home, sid)
    proc = _spawn(home, port, "second-stderr.log")
    try:
        _wait_health(port)
        recovery_id = _wait_pending(port, sid)["recovery_id"]
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_timeout(1000)

            # 记录决策请求（真 fetch 的 body 是验收核心）
            reqs: list[dict] = []
            page.on(
                "request",
                lambda r: reqs.append({"url": r.url, "body": r.post_data})
                if r.url.endswith("/api/lastwords/resume") and r.method == "POST"
                else None,
            )

            # The decision was registered before the browser connected. This
            # exercises recovery through the real REST bootstrap without WS stubs.
            page.locator(".nav-item.group", has_text=USER_MSG).first.click(timeout=15_000)
            card = page.get_by_role("status").filter(has_text=HEAD_TEXT)
            expect(card).to_contain_text("2 background tasks killed")
            resume = card.get_by_role("button", name="Resume", exact=True)
            discard = card.get_by_role("button", name="Discard", exact=True)
            expect(resume).to_have_count(1)
            expect(discard).to_have_count(1)

            path = "/api/lastwords/resume"
            with page.expect_response(lambda response: response.url.endswith(path)
                                      and response.request.method == "POST") as decision_response:
                (resume if decision == "resume" else discard).click()
            response = decision_response.value.json()
            assert response["ok"] and response["recovery_id"] == recovery_id, response
            assert len(reqs) == 1, reqs
            expected = {"session_id": sid, "recovery_id": recovery_id}
            if decision == "resume":
                assert response["status"] == "queued", response
                feedback = "Recovery completed"
            else:
                expected["dismiss"] = True
                assert response["status"] == "dismissed", response
                feedback = "Recovery dismissed"
            assert json.loads(reqs[0]["body"]) == expected
            expect(page.get_by_role("status").filter(has_text=feedback)).to_be_visible(timeout=15_000)
            again = _api(port, path, expected | {"dismiss": False})
            assert again == {"ok": False, "error": "no_pending"}, again
            assert not (home / "lastwords" / f"{sid}.json").exists()
            browser.close()
    finally:
        sc.stop_sidecar(proc)


def test_packaged_ui_lastwords_resume_button_runs_real_resume(tmp_path):
    _run_decision_flow(tmp_path, "resume")


def test_packaged_ui_lastwords_dismiss_button_sends_dismiss_true(tmp_path):
    _run_decision_flow(tmp_path, "dismiss")
