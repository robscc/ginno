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
5. 图、列表与浮层的状态文字一致，缩窄后仍可见
"""

import time

import pytest
import test_packaged_ui_tasks_overview as smoke

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e

def _item(item_id: str, deps: list[str], status: str = "pending") -> dict:
    return {
        "item_id": item_id,
        "goal": f"goal of {item_id}",
        "constraints": "",
        "acceptance": "",
        "agent_type": "",
        "backend": "ginno",
        "mode": "read-only",
        "depends_on": deps,
        "status": status,
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
    smoke._require_runtime()
    from playwright.sync_api import expect, sync_playwright

    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        sess = smoke._api(port, "POST", "/api/sessions", {
            "project_slug": "default",
            "workspace": str(tmp_path),
            "title": "Plan DAG E2E",
        })
        sid = sess.get("session_id") or sess.get("id")
        assert sid, sess

        # 直写 plan 快照（store 落盘格式）：菱形依赖 + 无依赖对照。
        plans_dir = tmp_path / "projects" / "default" / "sessions" / sid / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        diamond = _plan("pl_diamond", sid, "Diamond Plan", [
            _item("it_a", [], "done"),
            _item("it_b", ["it_a"], "failed"),
            _item("it_c", ["it_a"], "running"),
            _item("it_d", ["it_b", "it_c"]),
        ], "pending_confirm")
        flat = _plan("pl_flat", sid, "Flat Plan", [
            _item("it_x", []),
            _item("it_y", [], "stopped"),
        ], "active")
        (plans_dir / "pl_diamond.json").write_text(json.dumps(diamond), encoding="utf-8")
        (plans_dir / "pl_flat.json").write_text(json.dumps(flat), encoding="utf-8")

        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            page.add_init_script(f"localStorage.setItem('ginno-last-session', {json.dumps(sid)})")
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            expect(page.get_by_text("Diamond Plan", exact=True)).to_be_visible()

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
            statuses = {"it_a": "Completed", "it_b": "Failed", "it_c": "Running", "it_d": "Blocked"}
            for iid, status in statuses.items():
                node = dag.locator(f'g[data-id="{iid}"]')
                expect(node.get_by_text(iid, exact=True)).to_be_visible()
                expect(node.get_by_text(status, exact=True)).to_be_visible()

            # ── 2. 窄依赖图按容器缩放，状态标签留在节点内 ──
            graph_selector = '[data-plan-id="pl_diamond"] [role="img"]'
            narrow_style = page.add_style_tag(content=f"{graph_selector} {{ max-width: 310px; }}")
            page.wait_for_function(
                "selector => document.querySelector(`${selector} svg`).getBoundingClientRect().width <= 310",
                arg=graph_selector,
            )
            for iid, status in statuses.items():
                label = dag.locator(f'g[data-id="{iid}"]').get_by_text(status, exact=True)
                expect(label).to_be_visible()
                assert label.evaluate("""el => {
                    const box = el.getBoundingClientRect();
                    const node = el.closest('foreignObject').getBoundingClientRect();
                    return box.left >= node.left && box.right <= node.right + 1;
                }""")
            assert dag.evaluate("el => el.scrollWidth <= el.clientWidth + 1")
            narrow_style.evaluate("el => el.remove()")

            # ── 3. hover：依赖链高亮（SVG g opacity）+ 原始字号浮层 ──
            dag.get_by_text("it_b", exact=True).first.hover()
            expect(dag.locator('g[data-id="it_c"]')).to_have_attribute("opacity", "0.22")
            opacities = dag.evaluate(
                """(el) => Array.from(el.querySelectorAll('g[data-id]'))
                    .map(g => ({ id: g.getAttribute('data-id'), op: g.getAttribute('opacity') || '1' }))"""
            )
            dimmed = [o for o in opacities if float(o["op"]) < 0.9]
            assert dimmed, f"hover it_b should dim unrelated nodes, got {opacities}"
            tip = page.locator('[data-testid="plan-dag-tip"]')
            expect(tip).to_contain_text("goal of it_b")
            expect(tip.get_by_text("Failed", exact=True)).to_be_visible()

            # ── 4. 切清单视图：行 + ⇐ 依赖徽标 ──
            list_btn.click()
            expect(list_btn).to_have_attribute("aria-pressed", "true")
            for iid, status in statuses.items():
                row = diamond.get_by_role("button").filter(has_text=iid)
                expect(row.get_by_text(status, exact=True)).to_be_visible()
            assert diamond.locator('[role="img"][aria-label*="dependency graph" i]').count() == 0
            assert diamond.get_by_text("⇐", exact=False).count() >= 3, "list rows keep ⇐ badges"

            # ── 5. 切回 DAG ──
            dag_btn.click()
            expect(dag).to_be_visible()

            # ── 6. 无依赖清单默认清单视图 ──
            assert flat.count() >= 1
            flat_list = flat.locator('button[title="List view"]')
            assert flat_list.get_attribute("aria-pressed") == "true", (
                "plan without depends_on should default to list view"
            )

            expect(flat.get_by_role("button").filter(has_text="it_x").get_by_text("Pending", exact=True)).to_be_visible()
            expect(flat.get_by_role("button").filter(has_text="it_y").get_by_text("Stopped", exact=True)).to_be_visible()

            browser.close()
    finally:
        smoke._stop(proc, log)
