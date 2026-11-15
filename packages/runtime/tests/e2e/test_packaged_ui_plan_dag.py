"""Packaged-UI e2e: 任务清单 DAG 依赖图视图（CLAUDE.md 红线：前端交互改动
必须配 packaged UI 用例——vitest 守布局纯函数，这里守渲染层）。

数据面走捷径：plan 快照直写 store 落盘路径（plans/<plan_id>.json，GET /plans
每次读盘），不依赖 fake LLM 的工具调用行为——本用例验的是 PlanBoard/PlanDag
的渲染与切换，不是建单链路（建单校验有 runtime 单测覆盖）。

覆盖：
1. 有 depends_on 的清单默认 DAG 视图：节点数/边数与快照一致（菱形 4 节点 4 边）
2. 视图切换：DAG ↔ 清单，aria-pressed 与 DOM 形态同步
3. 无依赖清单默认清单视图（DAG 退化无意义）
4. hover 节点高亮依赖链（无关节点淡出 opacity < 1）
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
PORT = 8901


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


def _item(item_id: str, deps: list[str]) -> dict:
    return {
        "item_id": item_id,
        "goal": f"goal of {item_id}",
        "constraints": "",
        "acceptance": "",
        "agent_type": "",
        "backend": "ginno",
        "mode": "read-only",
        "depends_on": deps,
        "status": "pending",
        "child_session_id": None,
        "attempts": 0,
        "result_summary": "",
        "error": "",
        "settled_at": None,
    }


def _plan(plan_id: str, sid: str, title: str, items: list[dict], status: str) -> dict:
    now = time.time()
    return {
        "plan_id": plan_id,
        "session_id": sid,
        "project_slug": "default",
        "title": title,
        "status": status,
        "version": 1,
        "created": now,
        "updated": now,
        "author": "agent",
        "items": items,
    }


def test_packaged_ui_plan_dag_view(tmp_path):
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
            "title": "Plan DAG E2E",
        })
        sid = sess.get("session_id") or sess.get("id")
        assert sid, sess

        # 直写 plan 快照（store 落盘格式）：菱形依赖 + 无依赖对照。
        plans_dir = home / "projects" / "default" / "sessions" / sid / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        diamond = _plan("pl_diamond", sid, "Diamond Plan", [
            _item("it_a", []),
            _item("it_b", ["it_a"]),
            _item("it_c", ["it_a"]),
            _item("it_d", ["it_b", "it_c"]),
        ], "pending_confirm")
        flat = _plan("pl_flat", sid, "Flat Plan", [
            _item("it_x", []),
            _item("it_y", []),
        ], "active")
        (plans_dir / "pl_diamond.json").write_text(json.dumps(diamond), encoding="utf-8")
        (plans_dir / "pl_flat.json").write_text(json.dumps(flat), encoding="utf-8")

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(2000)
            page.get_by_text("Plan DAG E2E", exact=False).first.click()
            page.get_by_text("Diamond Plan", exact=False).first.wait_for(timeout=10000)

            diamond = page.locator('[data-plan-id="pl_diamond"]')
            flat = page.locator('[data-plan-id="pl_flat"]')
            dag_btn = diamond.locator('button[title="Dependency graph view"]')
            list_btn = diamond.locator('button[title="List view"]')

            # ── 1. 有依赖 → 默认 DAG：4 节点 4 边 ──
            assert dag_btn.get_attribute("aria-pressed") == "true", (
                "plan with depends_on should default to DAG view"
            )
            dag = diamond.locator('[role="img"][aria-label*="dependency graph" i]')
            dag.wait_for(timeout=5000)
            assert dag.locator("svg path[marker-end]").count() == 4, "diamond = 4 edges"
            for iid in ("it_a", "it_b", "it_c", "it_d"):
                assert dag.get_by_text(iid, exact=True).count() >= 1, f"node {iid} missing"

            # ── 2. 同步缩小不横滚（2026-11-15 用户约束）──
            assert dag.evaluate("(el) => el.scrollWidth <= el.clientWidth + 1"), (
                "DAG must scale to fit container width, no horizontal scroll"
            )

            # ── 3. hover：依赖链高亮（SVG g opacity）+ 原始字号浮层 ──
            dag.get_by_text("it_b", exact=True).first.hover()
            page.wait_for_timeout(300)
            opacities = dag.evaluate(
                """(el) => Array.from(el.querySelectorAll('g[data-id]'))
                    .map(g => ({ id: g.getAttribute('data-id'), op: g.getAttribute('opacity') || '1' }))"""
            )
            dimmed = [o for o in opacities if float(o["op"]) < 0.9]
            assert dimmed, f"hover it_b should dim unrelated nodes, got {opacities}"
            tip = page.locator('[data-testid="plan-dag-tip"]')
            assert tip.count() == 1, "hover must show the original-font-size tooltip"
            assert "goal of it_b" in tip.inner_text(), "tooltip shows full goal"

            # ── 4. 切清单视图：行 + ⇐ 依赖徽标 ──
            list_btn.click()
            page.wait_for_timeout(300)
            assert list_btn.get_attribute("aria-pressed") == "true"
            assert diamond.locator('[role="img"][aria-label*="dependency graph" i]').count() == 0
            assert diamond.get_by_text("⇐", exact=False).count() >= 3, "list rows keep ⇐ badges"

            # ── 5. 切回 DAG ──
            dag_btn.click()
            page.wait_for_timeout(300)
            assert diamond.locator('[role="img"][aria-label*="dependency graph" i]').count() == 1

            # ── 6. 无依赖清单默认清单视图 ──
            assert flat.count() >= 1
            flat_list = flat.locator('button[title="List view"]')
            assert flat_list.get_attribute("aria-pressed") == "true", (
                "plan without depends_on should default to list view"
            )

            browser.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()