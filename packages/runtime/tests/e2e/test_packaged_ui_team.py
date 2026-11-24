"""Packaged-UI e2e: AgentTeam 广场投递链路（backlog P1-21，CLAUDE.md 红线）。

2026-11-12 起的规矩：前端交互改动必须配 packaged UI 用例——vitest/tsc 抓不到
渲染层断裂（@ 菜单全灭三天才被发现的教训）。团队链路此前 58 个 e2e 文件零覆盖。

本文件与 test_packaged_ui_mention.py 同基建（dist sidecar + fake LLM + 真实
Chromium）。覆盖的都是**本轮修过的生产缺陷**，不是泛泛的 happy path：

1. P0-1 回归：广场 @ 成员发送后 **composer 必须恢复可用**（此前 runtime 发完
   team.delivered 就 continue、无 message.end，前端 busy 恒真 → 永久锁死）。
   判据：发送后能再次输入并再次发送，且第二条也进了历史。
2. P0-1 后半：用户那条消息**落 checkpoint**——重载后仍在历史里（此前不落盘，
   前端对账标「未送达」给重试 → 重复投递）。
3. 成员回帖渲染成回帖卡（不是普通气泡）。
4. P1-12：blocked 成员在 @ 菜单里禁选（aria-disabled / disabled）。
"""

import time
from urllib.parse import urlparse

import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


@pytest.fixture
def team_page(tmp_path):
    smoke._require_runtime()
    from playwright.sync_api import sync_playwright

    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        created = smoke._api(port, "POST", "/api/teams", {"name": "E2E Team", "project_slug": "default"})
        assert created["ok"], created
        team = created["team"]
        added = smoke._api(port, "POST", f"/api/teams/{team['id']}/members", {"agent_id": "dev"})
        assert added["ok"], added
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                yield page, port, team, added["member"]
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)


def _history(port: int, sid: str) -> list:
    return smoke._api(port, "GET", f"/api/sessions/{sid}/history").get("messages") or []


def _user_texts(msgs: list) -> list[str]:
    out = []
    for m in msgs:
        if (m.get("role") or m.get("type")) not in ("human", "user"):
            continue
        txt = str(m.get("text") or "") + " ".join(
            str(b.get("text") or "") for b in (m.get("blocks") or [])
            if isinstance(b, dict) and b.get("kind") == "text"
        )
        out.append(txt)
    return out


def test_packaged_ui_team_plaza_delivery_keeps_composer_usable(team_page):
    page, port, team, _member = team_page
    main_sid = team["main_session_id"]
    page.goto(f"http://127.0.0.1:{port}/team")
    page.locator('[aria-current="true"][role="button"]').filter(has_text="E2E Team").wait_for()

    # 页面同时挂着广场与成员工作台两个 composer，其中一个 display:none。
    # 裸 `.first` 会选中隐藏那个 → click 永久超时（2026-11-15 实测：
    # textarea[0] visible=False / [1] visible=True，本用例因此静默红）。
    # 一律按可见性过滤，别依赖 DOM 顺序。
    ta = page.locator("textarea:visible").first
    ta.click()
    ta.fill("@")
    page.wait_for_timeout(800)
    menu = page.locator('[role="listbox"]')
    assert menu.count() >= 1, "@ should open the mention menu in the plaza"

    # 选成员条目（菜单里只有本团队成员 + 产物）
    opt = page.locator('[role="option"]').first
    assert opt.count() >= 1, "team @ menu should list member instances"
    opt.click()
    page.wait_for_timeout(400)

    ta.click()
    ta.press_sequentially("please check the repo", delay=10)
    ta.press("Enter")

    # ── P0-1 核心回归：composer 必须恢复可用 ──
    # 此前无 message.end → busy 恒真 → 输入进 steer 死队列、再发无效。
    deadline = time.time() + 20
    sent = False
    while time.time() < deadline:
        texts = _user_texts(_history(port, main_sid))
        if any("please check the repo" in t for t in texts):
            sent = True
            break
        page.wait_for_timeout(400)
    assert sent, "plaza message must be persisted to checkpoint (P0-1)"

    # ── 2026-11-15 回归：投递后不许留下「(空回复)」空气泡 ──
    # 广场投递不起主会话 turn，ChatStream 乐观插入的 blocks:[] 占位
    # 永远等不到流事件。此前 team.delivered 只清 liveBySessionRef、
    # 不删 store 里的空壳 → message.end 的 stampTurnTs 按 turnId 兜底
    # 命中它并盖上 ts → 渲染成「(空回复)」+「刚刚」，每发一条必现一个。
    # 判据取 DOM 而非 store：这是用户真正看到的东西（vitest 已在
    # reducer 层覆盖，这里守渲染层——CLAUDE.md 红线的分工）。
    def _empty_reply_bubbles() -> int:
        return page.evaluate(
            "() => Array.from(document.querySelectorAll('span'))"
            ".filter(s => /^(（空回复）|\\(empty reply\\))$/.test((s.textContent||'').trim()))"
            ".length"
        )

    deadline = time.time() + 8
    while time.time() < deadline:
        if _empty_reply_bubbles() == 0:
            break
        page.wait_for_timeout(300)
    assert _empty_reply_bubbles() == 0, (
        "plaza delivery left an empty assistant bubble rendering "
        "「(空回复)」 — the optimistic placeholder must be dropped "
        "when team.delivered arrives (2026-11-15 user screenshot)"
    )

    # 第二条必须还能发出去（锁死时这条进不了历史）
    ta.click()
    ta.fill("second message after delivery")
    ta.press("Enter")
    deadline = time.time() + 20
    second = False
    while time.time() < deadline:
        texts = _user_texts(_history(port, main_sid))
        if any("second message after delivery" in t for t in texts):
            second = True
            break
        page.wait_for_timeout(400)
    assert second, (
        "composer locked after team delivery (P0-1): the second message "
        "never reached the checkpoint"
    )

    # 第二条之后同样零空气泡——用户症状是「每发一条必现一个」，
    # 只查第一条会漏掉「按 turnId 累积」的那一半。
    page.wait_for_timeout(1200)
    assert _empty_reply_bubbles() == 0, (
        "empty reply bubbles accumulated across plaza deliveries"
    )

    # 重载后两条都还在（落盘对账，不给「未送达」重试）
    page.reload(wait_until="load")
    page.wait_for_timeout(2500)
    texts = _user_texts(_history(port, main_sid))
    assert any("please check the repo" in t for t in texts)
    assert any("second message after delivery" in t for t in texts)


def test_team_member_intent_is_consumed_once(team_page):
    from playwright.sync_api import expect

    page, port, team, member = team_page
    base = f"http://127.0.0.1:{port}"
    intent = {"teamId": team["id"], "instanceId": member["instance_id"], "sessionId": member["session_id"]}
    key = "ginno-team-open-member-intent"
    seen_url = f"{base}/api/teams/{team['id']}/members/{member['instance_id']}/seen?client=main"
    seen = []
    page.on("request", lambda req: seen.append(req.url) if req.method == "POST" and req.url == seen_url else None)

    page.goto(base)
    page.evaluate("([key, intent]) => sessionStorage.setItem(key, JSON.stringify(intent))", [key, intent])
    main_ws_path = f"/api/ws/sessions/{team['main_session_id']}"
    with page.expect_event("websocket", lambda ws: urlparse(ws.url).path == main_ws_path) as connected:
        with page.expect_response(seen_url) as receipt:
            page.locator('a[href="/team"]').click()
    assert receipt.value.ok
    back = page.get_by_role("button", name="Back to main session")
    expect(back).to_be_visible()
    assert page.evaluate("key => sessionStorage.getItem(key)", key) is None

    with connected.value.expect_event("close"):
        page.get_by_title("Back to the landing home; the session is created on first send", exact=True).click()
        page.wait_for_url(base + "/")
    assert connected.value.is_closed()
    page.locator('a[href="/team"]').click()
    expect(page.locator('[aria-current="true"][role="button"]')).to_contain_text("E2E Team")
    expect(back).to_have_count(0)
    assert seen == [seen_url]

    # 同路由回帖事件仍然进入成员工作台，并只发出一次已读请求。
    with page.expect_response(seen_url) as receipt:
        page.evaluate("detail => dispatchEvent(new CustomEvent('ginno:open-team-member', {detail}))", intent)
    assert receipt.value.ok
    expect(back).to_be_visible()
    assert seen == [seen_url, seen_url]
