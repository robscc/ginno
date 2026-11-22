"""Packaged-UI e2e：Studio NodeInspector 保存被拒时透传服务端 detail（Q6）。

现象：节点编辑（dry-run 预览 → Apply）保存失败时只显示泛型
「Save failed (the server rejected this DSL)」，服务端 400 body.detail
（DSL 校验的具体原因）被丢弃，用户无从知道改什么。

契约：
* PUT /api/workflows/{id} 400 + {"detail": "..."} → 行内错误显示
  「Save failed: <detail>」（detail 原文可见，selectable）；
* 409（将来 store 加 version CAS 的并发冲突）同样透传——分型依据是
  jsonStrict 抛错的 ginnoStatus，而不是笼统吞掉；
* 断连（route.abort → TypeError）→ 仍显示中性的
  「Unable to connect to runtime」，不漏浏览器技术串。

复用 tasks_overview 的 source/strict harness（GINNO_E2E_RUNTIME=source 时
spawn 源码 sidecar + repo 内 apps/web/out；随机空闲端口 + 临时 GINNO_HOME，
不碰 8787 与真实设置）。创建工作流/dry-run 走真实服务端，只有被测的
PUT 用 page.route mock 故障态。
"""

from __future__ import annotations

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


@pytest.fixture
def app(tmp_path):
    smoke._require_runtime()
    from playwright.sync_api import sync_playwright

    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                yield page, port, f"http://127.0.0.1:{port}"
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)


def _two_llm_workflow(port: int) -> str:
    dsl = {
        "entry": "s1",
        "nodes": [
            {"id": "s1", "type": "llm", "prompt": "first"},
            {"id": "s2", "type": "llm", "prompt": "second"},
        ],
        "edges": [{"from": "s1", "to": "s2"}],
    }
    wf = smoke._api(port, "POST", "/api/workflows", {"name": "q6 save detail", "dsl": dsl})["workflow"]
    return wf["id"]


def test_node_save_rejection_shows_server_detail(app):
    from playwright.sync_api import expect

    page, port, base = app
    wid = _two_llm_workflow(port)
    page.goto(f"{base}/workflows#wf={wid}&node=s1&tab=design")

    # Inspector 挂载（节点 id 标题出现）。
    expect(page.get_by_text("s1", exact=True).first).to_be_visible(timeout=20_000)

    # 改 goal（第一个 textarea）→ dirty → "Save as new version" 可点。
    goal = page.locator('label:text-is("goal") + textarea')
    goal.fill("edited goal for q6")
    save = page.get_by_role("button", name="Save as new version")
    expect(save).to_be_enabled()
    # dry-run 走真实服务端（2 节点合法 DSL 必过）→ diff 确认卡出现。
    save.click()
    apply = page.get_by_role("button", name=re_apply())
    expect(apply).to_be_visible(timeout=15_000)

    # Mock 三种故障态：400（detail）→ 409（detail）→ 断连。
    state = {"mode": "400"}
    detail_400 = "node s2: 'prompt' must be a non-empty string"
    detail_409 = "version conflict: base 3 != 2"

    def intercept(route):
        if route.request.method != "PUT":
            route.continue_()
            return
        if state["mode"] == "abort":
            route.abort()
            return
        status = 400 if state["mode"] == "400" else 409
        body = detail_400 if status == 400 else detail_409
        route.fulfill(status=status, content_type="application/json", body=f'{{"detail": "{body}"}}')

    page.route(f"**/api/workflows/{wid}", intercept)

    # ① 400：行内错误必须含服务端 detail 原文，而不是泛型 saveRejected。
    apply.click()
    expect(page.get_by_text(f"Save failed: {detail_400}", exact=False)).to_be_visible(timeout=10_000)
    # 泛型文案不应再出现（透传成功即取代）。
    expect(page.get_by_text("the server rejected this DSL", exact=False)).to_have_count(0)

    # ② 409：diff 确认卡仍在（保存未成功不退出编辑态），再点 Apply。
    state["mode"] = "409"
    apply.click()
    expect(page.get_by_text(f"Save failed: {detail_409}", exact=False)).to_be_visible(timeout=10_000)

    # ③ 断连：中性 runtimeUnreachable，不漏 "Failed to fetch"。
    state["mode"] = "abort"
    apply.click()
    expect(page.get_by_text("Unable to connect to runtime", exact=False)).to_be_visible(timeout=10_000)
    expect(page.get_by_text("Failed to fetch", exact=False)).to_have_count(0)

    # 服务端终态：三次失败的 PUT 都被 mock 拦截，真实 DSL 未被改动。
    wf = smoke._api(port, "GET", f"/api/workflows/{wid}")["workflow"]
    goals = [n.get("goal") for n in wf["dsl"]["nodes"]]
    assert goals == [None, None], f"mock 拦截期间服务端不得收到写入: {goals}"


def re_apply() -> str:
    import re

    return re.compile(r"Apply.*v2")


def test_node_save_success_path_unmocked(app):
    """对照：无 mock 时真实 PUT 成功——onSaved 生效、编辑卡收起（不回归）。"""
    from playwright.sync_api import expect

    page, port, base = app
    wid = _two_llm_workflow(port)
    page.goto(f"{base}/workflows#wf={wid}&node=s1&tab=design")
    expect(page.get_by_text("s1", exact=True).first).to_be_visible(timeout=20_000)

    page.locator('label:text-is("goal") + textarea').fill("edited goal for q6 success")
    page.get_by_role("button", name="Save as new version").click()
    apply = page.get_by_role("button", name=re_apply())
    expect(apply).to_be_visible(timeout=15_000)
    apply.click()
    # diff 确认卡收起 = commit 成功（失败时卡片保留并显示错误）。
    expect(apply).to_have_count(0, timeout=10_000)

    wf = smoke._api(port, "GET", f"/api/workflows/{wid}")["workflow"]
    assert [n.get("goal") for n in wf["dsl"]["nodes"]] == ["edited goal for q6 success", None]
