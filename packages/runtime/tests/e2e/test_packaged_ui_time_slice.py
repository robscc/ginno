"""Packaged-UI e2e: 会话切换首屏「时间分片挂载」（CLAUDE.md 红线：前端交互
改动必须配 packaged UI 用例）。

背景：窗口化（首屏 ≤80 条）之后，重会话窗口内 ~1300+ blocks 仍是一次性同步
React mount，构成切换剩余 max longtask 的主体。治理：尾部先挂（block 预算
160），更旧消息经 requestIdleCallback 逐片 prepend（每片 ≤120 blocks），
片间让出主线程；滚动补偿（贴底续贴 / 上翻按高度差平移）+ ⌘K/⌘F 锚点立即
挂到目标行（revealUpTo 扩窗管线不变）。

本用例在真实浏览器里验证四件事：
1. 分片挂载后消息**完整渲染**（窗口 80 行 + load-earlier 扩到 84 行，首尾
   marker 齐全有序），且首帧确实是增量挂载（早期行数 < 80）；
2. **滚动位置稳定**：分片进行中把 scrollTop 置到中段，落定后视口中央的
   消息行位移 ≤40px（补偿生效，不跳动）；
3. **⌘K 搜索锚点**：命中窗口外消息 → 扩窗 + 定位居中 + violet ring；
   **⌘F 会话内搜索**：计数全量、落定后 CSS Custom Highlight 补刷覆盖晚挂载
   行、Enter 遍历 active 高亮 + 窗口外命中扩窗定位；
4. **max longtask 实测**：PerformanceObserver('longtask') 记录冷/热切换窗口
   内的最长任务，打印改前/改后对比数值（基线冷 ~341ms，见 PR）。

范式同 test_packaged_ui_idle_perf：独立端口 + 独立 GINNO_HOME +
GINNO_FAKE_LLM 脚本，不碰真运行时数据。重会话用 WS 无头驱动 42 轮（每轮
5 个工具步 × 6 bash + 终稿 ≈ 32 blocks/轮 → 84 条消息 / ~1300 blocks）；
随后**重启 sidecar** 让启动期 search_index 线程索引该会话（索引只在启动
构建），⌘K 消息命中才有数据。
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

TURNS = 42          # 84 条消息 > 窗口 80 → load-earlier 按钮出现
TOOL_STEPS = 5      # 每轮 5 个工具步（fake 脚本按会话连续消费）
CALLS_PER_STEP = 6  # 每步 6 个 bash → 30 tool blocks + 1 text ≈ 31 blocks/轮
WINDOW = 80         # TranscriptList WINDOW_INITIAL（首屏窗口条数）


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
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if payload is None else "POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _scripts() -> list[dict]:
    """每轮消费 TOOL_STEPS+1 个条目：5 工具步 + 1 终稿（模型实例按会话持久，
    脚本游标跨轮连续——条目数必须 = TURNS × (TOOL_STEPS+1)）。"""
    out: list[dict] = []
    for i in range(TURNS):
        for step in range(TOOL_STEPS):
            out.append({
                "content": "",
                "tool_calls": [
                    {"name": "bash", "args": {"command": f"echo tool-output-{i}-{step}-{j}"}}
                    for j in range(CALLS_PER_STEP)
                ],
            })
        out.append({"content": f"turn {i} complete: all five tool steps finished."})
    return out


def _seed_turns(port: int, sid: str) -> None:
    """WS 无头驱动 TURNS 轮（比 UI 逐轮打字快且稳）。"""
    from websockets.sync.client import connect

    with connect(f"ws://127.0.0.1:{port}/api/ws/sessions/{sid}") as ws:
        for i in range(TURNS):
            ws.send(json.dumps({
                "type": "invoke",
                "message": f"slice-marker-{i:02d} zqx needle turn {i}",
            }))
            deadline = time.time() + 90
            while time.time() < deadline:
                ev = json.loads(ws.recv(timeout=30))
                name = ev.get("event")
                if name == "message.end":
                    break
                if name == "error":
                    raise RuntimeError(f"turn {i} errored: {ev}")
            else:
                raise RuntimeError(f"turn {i} did not finish in 90s")


def _rows_js() -> str:
    return "document.querySelectorAll('[data-msg-id]').length"


def _install_longtask_observer(page) -> None:
    page.evaluate(
        """() => {
            window.__lt = [];
            if (window.__ltObs) window.__ltObs.disconnect();
            window.__ltObs = new PerformanceObserver((list) => {
                for (const e of list.getEntries()) {
                    window.__lt.push({ start: e.startTime, dur: e.duration });
                }
            });
            window.__ltObs.observe({ type: 'longtask', buffered: false });
        }"""
    )


def _max_longtask(page) -> float:
    entries = page.evaluate("() => window.__lt || []")
    return max((e["dur"] for e in entries), default=0.0)


def test_packaged_ui_time_slice_mount(tmp_path):
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
    scripts_file = tmp_path / "scripts.json"
    scripts_file.write_text(json.dumps(_scripts()))
    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts_file),
        GINNO_RUNTIME_PORT=str(PORT),
    )

    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        heavy = _api(PORT, "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Heavy Slice E2E",
        })
        heavy_sid = heavy.get("session_id") or heavy.get("id")
        assert heavy_sid, heavy
        light = _api(PORT, "/api/sessions", {
            "project_slug": "default", "workspace": str(tmp_path), "title": "Light Slice E2E",
        })
        light_sid = light.get("session_id") or light.get("id")
        assert light_sid, light
        _seed_turns(PORT, heavy_sid)
        hist = _api(PORT, f"/api/sessions/{heavy_sid}/history")
        assert len(hist.get("messages") or []) == TURNS * 2, "heavy session must have 84 messages"
    finally:
        sc.stop_sidecar(proc)

    # 重启 sidecar：search_index 只在启动期构建——重启后 ⌘K 消息命中才有数据。
    proc = sc.spawn_sidecar(str(RUNTIME_BIN), env)
    try:
        _wait_health(PORT)
        deadline = time.time() + 40
        indexed = False
        while time.time() < deadline:
            r = _api(PORT, "/api/search?q=slice-marker-00&scope=messages")
            if r.get("messages"):
                indexed = True
                break
            time.sleep(1)
        assert indexed, "search index did not pick up the heavy session after restart"

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)

            # ── 冷切换：light → heavy（history REST + 分片 mount）──────────
            page.get_by_text("Light Slice E2E", exact=False).first.click()
            page.wait_for_timeout(600)
            _install_longtask_observer(page)
            page.get_by_text("Heavy Slice E2E", exact=False).first.click()
            page.wait_for_function(f"{_rows_js()} >= 5", timeout=20000)

            # 滚动稳定探针：分片进行中把视口置到中段，记录中央消息行。
            probe = page.evaluate(
                """() => {
                    const sc = document.querySelector('.overflow-y-auto.overscroll-contain');
                    const rows = document.querySelectorAll('[data-msg-id]').length;
                    sc.scrollTop = 300;
                    // 同步触发 scroll 事件 → stickRef 立即翻 false：否则
                    // pinToBottom 的 12 帧贴底环可能在 scroll 事件异步到达前
                    // 把视口重新拽回底部，探针就被污染了。
                    sc.dispatchEvent(new Event('scroll'));
                    const r = sc.getBoundingClientRect();
                    const x = Math.round(r.left + r.width / 2);
                    let id = null, top = null;
                    for (const y of [400, 350, 450, 300, 500, 250, 550]) {
                        const el = document.elementFromPoint(x, y);
                        const row = el && el.closest('[data-msg-id]');
                        if (row) {
                            id = row.getAttribute('data-msg-id');
                            top = row.getBoundingClientRect().top;
                            break;
                        }
                    }
                    return { rows, id, top, scrollTop: sc.scrollTop };
                }"""
            )
            assert probe["id"], "scroll probe must find a message row at viewport center"
            # 落定：窗口 80 行全挂载（分片完成的 DOM 信号）。
            page.wait_for_function(f"{_rows_js()} === {WINDOW}", timeout=30000)
            page.wait_for_timeout(800)  # idle 高亮升级尾窗
            after = page.evaluate(
                """(id) => {
                    const row = document.querySelector('[data-msg-id="' + CSS.escape(id) + '"]');
                    const sc = document.querySelector('.overflow-y-auto.overscroll-contain');
                    return {
                        top: row ? row.getBoundingClientRect().top : null,
                        scrollTop: sc.scrollTop,
                        away: sc.scrollHeight - sc.scrollTop - sc.clientHeight,
                    };
                }""",
                probe["id"],
            )
            assert after["top"] is not None, "probe row must still be mounted after settle"
            drift = abs(after["top"] - probe["top"])
            print(f"[time-slice] scroll probe: rowsAtSet={probe['rows']} drift={drift:.1f}px")
            if probe["rows"] < WINDOW:
                # 分片确实与探针重叠 → 补偿必须把视口内容钉住。
                assert drift <= 40, (
                    f"scroll jumped during sliced mount: drift={drift:.1f}px "
                    f"(rowsAtSet={probe['rows']}, before={probe['top']}, after={after['top']})"
                )
                assert after["away"] > 80, "user scrolled up must NOT be yanked back to bottom"
            cold_max = _max_longtask(page)
            print(f"[time-slice] COLD switch max longtask = {cold_max:.1f}ms")

            # ── 完整渲染契约 ──────────────────────────────────────────────
            markers = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-msg-id]'))
                    .map(r => r.textContent || '')
                    .filter(t => t.includes('slice-marker-'))
                    .map(t => (t.match(/slice-marker-(\\d\\d)/) || [])[1])"""
            )
            assert markers == [f"{i:02d}" for i in range(2, TURNS)], (
                f"window must render turns 02..41 in order, got {markers[:5]}..{markers[-5:]}"
            )
            assert page.locator('[data-testid="load-earlier"]').count() == 1

            # ── ⌘F 会话内搜索：计数全量 + 落定补刷 + 遍历定位 ─────────────
            page.keyboard.press("Control+f")
            page.keyboard.type("slice-marker")
            page.get_by_text(f"1/{TURNS}", exact=True).wait_for(timeout=10000)
            page.wait_for_timeout(400)
            painted = page.evaluate(
                "() => (CSS.highlights.get('find-hit') || { size: 0 }).size"
            )
            # 窗口内 40 个用户行各含 1 处命中；晚挂载行由落定事件补刷。
            assert painted >= WINDOW // 2 - 2, (
                f"find-hit ranges must cover late-mounted rows after settle, got {painted}"
            )
            page.keyboard.press("Enter")
            page.keyboard.press("Enter")  # activeIdx=2 → turn 02（窗口头部）
            page.get_by_text(f"3/{TURNS}", exact=True).wait_for(timeout=5000)
            page.wait_for_function(
                "() => (CSS.highlights.get('find-hit-active') || { size: 0 }).size >= 1",
                timeout=5000,
            )
            # 遍历到窗口外命中（activeIdx=1 → turn 01）：扩窗 + 定位 + 高亮。
            page.keyboard.press("Shift+Enter")
            page.wait_for_function(f"{_rows_js()} === {TURNS * 2}", timeout=10000)
            page.wait_for_function(
                """() => {
                    const rows = Array.from(document.querySelectorAll('[data-msg-id]'));
                    const row = rows.find(r => (r.textContent || '').includes('slice-marker-01'));
                    if (!row) return false;
                    const b = row.getBoundingClientRect();
                    return b.top >= 0 && b.bottom <= 800
                        && (CSS.highlights.get('find-hit-active') || { size: 0 }).size >= 1;
                }""",
                timeout=10000,
            )
            page.keyboard.press("Escape")
            page.wait_for_timeout(300)

            # ── ⌘K 跨会话锚点：窗口外命中 → 扩窗 + 居中 + violet ring ─────
            page.get_by_text("Light Slice E2E", exact=False).first.click()
            page.wait_for_timeout(600)
            page.evaluate("() => { window.__lt = []; }")
            page.keyboard.press("Control+k")
            page.keyboard.type("slice-marker-00")
            hit = page.locator("button").filter(has_text="slice-marker-00").first
            hit.wait_for(timeout=15000)
            hit.click()
            # 锚点行挂载 + violet ring（flash 2s 窗口内轮询，避免一次性求值竞态）。
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('[data-msg-id]'))
                    .some(r => (r.textContent || '').includes('slice-marker-00')
                        && r.className.includes('ring-2'))""",
                timeout=15000,
            )
            anchor = page.evaluate(
                """() => {
                    const rows = Array.from(document.querySelectorAll('[data-msg-id]'));
                    const row = rows.find(r => (r.textContent || '').includes('slice-marker-00'));
                    const b = row.getBoundingClientRect();
                    return {
                        rows: rows.length, top: b.top, bottom: b.bottom,
                        ring: row.className.includes('ring-2'),
                    };
                }"""
            )
            assert anchor["rows"] == TURNS * 2, "anchor must expand the window to all 84 rows"
            assert anchor["top"] >= 0 and anchor["bottom"] <= 800, (
                f"anchor row must be scrolled into viewport: {anchor}"
            )
            assert abs((anchor["top"] + anchor["bottom"]) / 2 - 400) < 300, (
                f"anchor row must be roughly centered: {anchor}"
            )
            assert anchor["ring"], "anchor row must carry the violet flash ring"
            page.wait_for_function(f"{_rows_js()} === {TURNS * 2}", timeout=10000)
            page.wait_for_timeout(800)
            anchor_max = _max_longtask(page)
            print(f"[time-slice] WARM switch (⌘K anchor expand) max longtask = {anchor_max:.1f}ms")

            # ── 热切换（store 已缓存）：纯 mount 长任务 ───────────────────
            page.get_by_text("Light Slice E2E", exact=False).first.click()
            page.wait_for_timeout(600)
            page.evaluate("() => { window.__lt = []; }")
            page.get_by_text("Heavy Slice E2E", exact=False).first.click()
            page.wait_for_function(f"{_rows_js()} === {WINDOW}", timeout=30000)
            page.wait_for_timeout(800)
            warm_max = _max_longtask(page)
            print(f"[time-slice] WARM switch max longtask = {warm_max:.1f}ms")
            # 贴底恢复：热切换落定后必须停在底部（stickRef 重置语义不回归）。
            away = page.evaluate(
                """() => { const sc = document.querySelector('.overflow-y-auto.overscroll-contain');
                    return sc.scrollHeight - sc.scrollTop - sc.clientHeight; }"""
            )
            assert away < 80, f"warm switch must land pinned to bottom, away={away}px"

            # ── load-earlier 扩窗不回归 ───────────────────────────────────
            page.locator('[data-testid="load-earlier"]').click()
            page.wait_for_function(f"{_rows_js()} === {TURNS * 2}", timeout=10000)
            assert page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-msg-id]'))
                    .some(r => (r.textContent || '').includes('slice-marker-00'))"""
            ), "load-earlier must reveal the hidden head messages"

            # 数值 sanity：分片后不应出现秒级病态长任务（机器差异容忍）。
            assert max(cold_max, warm_max, anchor_max) < 2000, (
                f"pathological longtask after slicing: cold={cold_max} warm={warm_max} anchor={anchor_max}"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)