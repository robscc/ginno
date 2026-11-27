"""Packaged-UI e2e: 会话切换首屏「时间分片挂载」（CLAUDE.md 红线：前端交互
改动必须配 packaged UI 用例）。

背景：窗口化（首屏 ≤80 条）之后，重会话窗口内 ~2400 blocks 仍是一次性同步
React mount，构成切换剩余 max longtask 的主体。治理：尾部先挂（block 预算
160），更旧消息经 setTimeout 逐片 prepend（每片 ≤120 blocks，~1 片/帧），
片间让出主线程；滚动补偿（贴底续贴 / 上翻按高度差平移）+ ⌘K/⌘F 锚点立即
挂到目标行（revealUpTo 扩窗管线不变）。

本用例在真实浏览器里验证四件事：
1. 首屏 80 行与较早的窗口均按历史顺序渲染，窗口移动后总挂载数保持 80；
2. **滚动位置稳定**：分片进行中把 scrollTop 置到中段，落定后视口中央的
   消息行位移 ≤40px（补偿生效，不跳动）；
3. **⌘K 搜索锚点**：命中窗口外消息时挂载目标并定位居中，显示 violet ring。
   **⌘F 会话内搜索**：计数全量，晚挂载行补刷高亮，Enter 遍历活动命中；
4. **max longtask 实测**：PerformanceObserver('longtask') 记录冷/热切换窗口
   内的最长任务，打印改前/改后对比数值（基线冷 ~341ms，见 PR）。

范式同 test_packaged_ui_idle_perf：独立端口 + 独立 GINNO_HOME，不碰真
运行时数据。重会话**直接合成 checkpoint**（测试进程内用 runtime 自己的
FileCheckpointer + JsonPlusSerializer 写 84 条 LangChain 消息：42 轮 ×
(30 个 read_file 工具步 + 终稿)，每轮合并气泡 ≈ 61 blocks → ~2400 blocks）
——比 fake-LLM 逐超步驱动快两个量级（后者 1300 超步的 checkpoint 写放大
要 ~9 分钟），且剖面确定。channel_values 镜像真实终态键集，WS 加载不炸。
总行数按 history 实际投影自适应。随后**重启 sidecar** 让启动期
search_index 线程索引该会话（索引只在启动构建），⌘K 消息命中才有数据。
"""

import os
import socket
import time
import urllib.request
import uuid
from pathlib import Path

import _sidecar as sc
import pytest

from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
PORT = 8907

TURNS = 42          # 84 条消息 > 窗口 80 → load-earlier 按钮出现
TOOL_STEPS = 30     # 每轮 30 工具步 → 30 tool + 31 text ≈ 61 blocks/轮
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


def _synth_history(sid: str, workspace: str) -> None:
    """直接合成重会话 checkpoint（秒级，剖面确定）。

    用 runtime 自己的 FileCheckpointer/serde 写盘——与真实 graph 产物同
    格式（v=4 full checkpoint，channel_values 镜像真实终态键集）。每轮：
    HumanMessage(marker) + 30×(AIMessage tool_call + ToolMessage) + 终稿
    AIMessage；_messages_to_ui 把连续 AI 步合并成单气泡（61 blocks）。
    """
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    from ginno_runtime.checkpointer import FileCheckpointer

    msgs: list = []
    for i in range(TURNS):
        msgs.append(HumanMessage(
            content=f"slice-marker-{i:02d} zqx needle turn {i}", id=f"turn-{i:02d}",
        ))
        for j in range(TOOL_STEPS):
            call_id = f"call-{i}-{j}"
            msgs.append(AIMessage(
                content=f"**Step {j}** — checking `payload-{i}-{j}`:\n- item one\n- item two\n",
                id=f"ai-{i}-{j}",
                tool_calls=[{"name": "read_file", "args": {"path": "payload.txt"},
                             "id": call_id, "type": "tool_call"}],
                additional_kwargs={"agent_id": "dev"},
            ))
            msgs.append(ToolMessage(
                content=f"tool payload {i}-{j}\nline two\n",
                tool_call_id=call_id, name="read_file",
            ))
        msgs.append(AIMessage(
            content=f"turn {i} complete: all thirty tool steps finished.",
            id=f"ai-{i}-final", additional_kwargs={"agent_id": "dev"},
        ))
    cp = FileCheckpointer("default")
    cid = str(uuid.uuid4())
    cp.put(
        {"configurable": {"thread_id": sid, "project_slug": "default"}},
        {
            "v": 4, "id": cid,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "channel_values": {
                "messages": msgs,
                "workspace": workspace,
                "project_slug": "default",
                "agent_id": "dev",
                "active_skills": [], "sticky_skills": [],
                "pending_tool_calls": [], "attached_files": [],
                "mention_context": [], "mcp_tool_names": [],
                "context_dirs": [], "primary_path": "",
            },
            "channel_versions": {"messages": 1},
            "versions_seen": {},
            "updated_channels": ["messages"],
        },
        {"source": "loop", "step": 1, "parents": {}},
        {"messages": 1},
    )


def _rows_js() -> str:
    return "document.querySelectorAll('[data-msg-id]').length"


def _install_longtask_observer(page) -> None:
    page.evaluate(
        """() => {
            window.__lt = [];
            if (window.__ltObs) window.__ltObs.disconnect();
            window.__ltObs = new PerformanceObserver((list) => {
                for (const e of list.getEntries()) {
                    window.__lt.push({ start: e.startTime, dur: e.duration, attribution: e.attribution ? Array.from(e.attribution).map(a => ({containerType: a.containerType, containerName: a.containerName, containerSrc: a.containerSrc})) : [] });
                }
            });
            window.__ltObs.observe({ type: 'longtask', buffered: false });
        }"""
    )


def _max_longtask(page) -> float:
    entries = page.evaluate("() => window.__lt || []")
    return max((e["dur"] for e in entries), default=0.0)


def _dump_longtasks(page, label: str, t_click: float) -> None:
    entries = page.evaluate(
        """() => (window.__lt || []).map(e => ({
            start: e.start, dur: e.dur,
            attr: (e.attribution || []).map(a => a.containerType + ':' + (a.containerName || a.containerSrc || '')).join('|'),
        }))"""
    )
    top = sorted(entries, key=lambda e: -e["dur"])[:5]
    print(f"[time-slice] {label} top tasks (offset:dur:attr): "
          + ", ".join(f"{e['start'] - t_click:.0f}:{e['dur']:.0f}:{e['attr'] or '-'}" for e in top))


def _marker_of(msg: dict) -> str | None:
    for b in msg.get("blocks") or []:
        if b.get("kind") == "text":
            t = b.get("text") or ""
            at = t.find("slice-marker-")
            if at >= 0:
                return t[at + len("slice-marker-"):at + len("slice-marker-") + 2]
    return None


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
    # 测试进程内合成 checkpoint 需要与 sidecar 同一个 GINNO_HOME。
    os.environ["GINNO_HOME"] = str(home)
    env = dict(os.environ, GINNO_RUNTIME_PORT=str(PORT))

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
        _synth_history(heavy_sid, str(tmp_path))
        hist = _api(PORT, f"/api/sessions/{heavy_sid}/history")
        msgs = hist.get("messages") or []
        # 84 条对话行 + 长会话中途可能注入的系统行（context/world-state）。
        assert TURNS * 2 <= len(msgs) <= TURNS * 2 + 2, f"unexpected message count: {len(msgs)}"
        roles: dict[str, int] = {}
        for m in msgs:
            roles[m["role"]] = roles.get(m["role"], 0) + 1
        print(f"[time-slice] seeded history: {len(msgs)} messages, roles={roles}")
        total = len(msgs)
        window_msgs = msgs[-WINDOW:]
        expected_markers = [mk for m in window_msgs if (mk := _marker_of(m))]
        n_hits = sum(1 for m in msgs if _marker_of(m))
        assert n_hits == TURNS, f"every turn must carry a unique marker, got {n_hits}"
        idx00 = next(i for i, m in enumerate(msgs) if _marker_of(m) == "00")
        idx01 = next(i for i, m in enumerate(msgs) if _marker_of(m) == "01")
        hidden_head = total - WINDOW
        assert idx00 < hidden_head and idx01 < hidden_head, (
            f"markers 00/01 must sit in the hidden head (idx {idx00}/{idx01} < {hidden_head})"
        )
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
            t_cold = page.evaluate("() => performance.now()")
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
            _dump_longtasks(page, "COLD", t_cold)

            # ── 完整渲染契约 ──────────────────────────────────────────────
            markers = page.evaluate(
                """() => Array.from(document.querySelectorAll('[data-msg-id]'))
                    .map(r => r.textContent || '')
                    .filter(t => t.includes('slice-marker-'))
                    .map(t => (t.match(/slice-marker-(\\d\\d)/) || [])[1])"""
            )
            assert markers == expected_markers, (
                f"window must render the tail-80 markers in order: "
                f"expected {expected_markers[:3]}..{expected_markers[-3:]}, "
                f"got {markers[:3]}..{markers[-3:]}"
            )
            assert page.locator('[data-testid="load-earlier"]').count() == 1

            # load-earlier shifts the window and keeps the overlapping rows.
            page.locator('[data-testid="load-earlier"]').click()
            page.locator('[data-msg-id="turn-00"]').wait_for()
            page.wait_for_function(f"{_rows_js()} === {WINDOW}", timeout=10000)
            older_ids = page.locator('[data-msg-id]').evaluate_all(
                "rows => rows.map(row => row.dataset.msgId)"
            )
            assert older_ids == [m["id"] for m in msgs[:WINDOW]]

            # ⌘K prioritizes the requested anchor over the saved reading position.
            page.get_by_text("Light Slice E2E", exact=False).first.click()
            page.wait_for_timeout(600)
            page.evaluate("() => { window.__lt = []; }")
            page.keyboard.press("Control+k")
            page.keyboard.type("slice-marker-00")
            hit = page.locator("button").filter(has_text="slice-marker-00").first
            hit.wait_for(timeout=15000)
            t_anchor = page.evaluate("() => performance.now()")
            hit.click()
            # 锚点行挂载 + violet ring + 居中一起轮询：ring 在首个成功 tick
            # 就出现，但 ChatStream 的 12 帧贴底环可能还在与锚点重定位打架
            # （基线整窗挂载时尤甚）——位置稳定后再断言，避免过早求值。
            page.wait_for_function(
                """() => Array.from(document.querySelectorAll('[data-msg-id]'))
                    .some(r => {
                        if (!(r.textContent || '').includes('slice-marker-00')) return false;
                        if (!r.className.includes('ring-2')) return false;
                        const b = r.getBoundingClientRect();
                        return b.top >= 0 && b.bottom <= 800;
                    })""",
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
            assert anchor["rows"] <= WINDOW, "finding an anchor must respect the mount budget"
            assert anchor["top"] >= 0 and anchor["bottom"] <= 800, (
                f"anchor row must be scrolled into viewport: {anchor}"
            )
            assert abs((anchor["top"] + anchor["bottom"]) / 2 - 400) < 300, (
                f"anchor row must be roughly centered: {anchor}"
            )
            assert anchor["ring"], "anchor row must carry the violet flash ring"
            page.wait_for_function(f"{_rows_js()} === {WINDOW}", timeout=10000)
            anchor_max = _max_longtask(page)
            print(f"[time-slice] WARM switch (⌘K anchor expand) max longtask = {anchor_max:.1f}ms")
            _dump_longtasks(page, "ANCHOR", t_anchor)

            # Leave from the tail so this warm switch exercises tail following.
            page.get_by_role("button", name="Jump to latest message", exact=True).click()
            page.locator('[data-msg-id="turn-41"]').wait_for(state="visible")
            page.get_by_text("Light Slice E2E", exact=False).first.click()
            page.wait_for_timeout(600)
            page.evaluate("() => { window.__lt = []; }")
            page.evaluate(
                """() => {
                    window.__rowsTl = [];
                    const t0 = performance.now();
                    if (window.__rowsIv) clearInterval(window.__rowsIv);
                    window.__rowsIv = setInterval(() => {
                        const n = document.querySelectorAll('[data-msg-id]').length;
                        const tl = window.__rowsTl;
                        if (!tl.length || tl[tl.length - 1][1] !== n) {
                            tl.push([Math.round(performance.now() - t0), n]);
                        }
                        if (performance.now() - t0 > 3000) clearInterval(window.__rowsIv);
                    }, 30);
                }"""
            )
            t_warm = page.evaluate("() => performance.now()")
            page.get_by_text("Heavy Slice E2E", exact=False).first.click()
            page.wait_for_function(f"{_rows_js()} === {WINDOW}", timeout=30000)
            page.wait_for_timeout(800)
            warm_max = _max_longtask(page)
            print(f"[time-slice] WARM switch max longtask = {warm_max:.1f}ms")
            _dump_longtasks(page, "WARM", t_warm)
            print("[time-slice] WARM rows timeline (offset:rows):",
                  page.evaluate("() => window.__rowsTl || []"))
            # 贴底恢复：热切换落定后必须停在底部（stickRef 重置语义不回归）。
            away = page.evaluate(
                """() => { const sc = document.querySelector('.overflow-y-auto.overscroll-contain');
                    return sc.scrollHeight - sc.scrollTop - sc.clientHeight; }"""
            )
            assert away < 80, f"warm switch must land pinned to bottom, away={away}px"

            # ── ⌘F 会话内搜索：计数全量 + 落定补刷 + 遍历定位 ─────────────
            page.keyboard.press("Control+f")
            # FindBar input（w-52）出现再打字；按键偶发被路由切换吞掉时重试一次。
            try:
                page.wait_for_selector("input.w-52", timeout=3000)
            except Exception:
                page.keyboard.press("Control+f")
                page.wait_for_selector("input.w-52", timeout=5000)
            page.keyboard.type("slice-marker")
            try:
                page.get_by_text(f"1/{n_hits}", exact=True).wait_for(timeout=10000)
            except Exception:
                print("[time-slice][debug] inputs:", page.evaluate(
                    "() => Array.from(document.querySelectorAll('input')).map(i => i.placeholder)"))
                print("[time-slice][debug] counters:", page.evaluate(
                    "() => Array.from(document.querySelectorAll('span')).map(s => s.textContent).filter(t => /\\d+\\/\\d+/.test(t || ''))"))
                print("[time-slice][debug] rows:", page.evaluate(_rows_js()))
                page.screenshot(path="/tmp/time-slice-find-fail.png")
                raise
            # The first match moves the window; wait for the new slice and its
            # highlight pass to settle before inspecting late-mounted rows.
            page.wait_for_function(
                f"{_rows_js()} === {WINDOW} && "
                f"(CSS.highlights.get('find-hit')?.size ?? 0) >= {len(expected_markers) - 2}",
                timeout=15000,
            )
            page.keyboard.press("Enter")
            page.keyboard.press("Enter")  # activeIdx=2 → turn 02（窗口头部）
            page.get_by_text(f"3/{n_hits}", exact=True).wait_for(timeout=5000)
            page.wait_for_function(
                "() => (CSS.highlights.get('find-hit-active') || { size: 0 }).size >= 1",
                timeout=5000,
            )
            # 遍历到窗口外命中（activeIdx=1 → turn 01）：扩窗 + 定位 + 高亮。
            page.keyboard.press("Shift+Enter")
            page.wait_for_function(f"{_rows_js()} === {WINDOW}", timeout=10000)
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

            # 数值 sanity：分片后不应出现秒级病态长任务（机器差异容忍）。
            assert max(cold_max, warm_max, anchor_max) < 2000, (
                f"pathological longtask after slicing: cold={cold_max} warm={warm_max} anchor={anchor_max}"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)