"""Browser + connector module unit tests (browser-companion / connector docs).

Covers the pure-logic halves of the two modules: registry state machine
(debounce, aggregate dot, idle semantics), the event bus, browser config
(sensitive domains), tool helpers (URL normalization, modifiers, handoff
release), the relay queue errors, JPEG size parsing, script generation, and
the native host protocol (port file + chunk reassembly).
"""

from __future__ import annotations

import asyncio
import struct

import pytest

from ginno_runtime import jsonx as json

# ---- helpers -------------------------------------------------------------


@pytest.fixture()
def ginno_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text("{}")
    return tmp_path


def _fresh_registry():
    # NOTE: ginno_runtime.connectors re-exports the registry() FUNCTION,
    # shadowing the submodule name — go through sys.modules directly.
    import importlib

    reg_mod = importlib.import_module("ginno_runtime.connectors.registry")
    reg = reg_mod.ConnectorRegistry()

    class _C(reg_mod.Connector):
        id = "t-conn"
        name = "Test"
        description = ""
        icon = "plug"
        order = 1

    reg.register(_C())
    return reg


# ---- registry: state machine, debounce, aggregate -------------------------


def test_registry_report_transitions(ginno_home):
    from ginno_runtime.connectors.registry import STATUS_CONNECTED

    reg = _fresh_registry()
    reg.report("t-conn", STATUS_CONNECTED, "up", version="1.0")
    snap = reg.status_of("t-conn")
    assert snap["status"] == "connected"
    assert snap["version"] == "1.0"


def test_registry_debounce_holds_brief_disconnect(ginno_home):
    """断连 3s 内:状态保持 connected,读时也不翻转(窗口未过)。"""
    from ginno_runtime.connectors.registry import (
        STATUS_CONNECTED,
        STATUS_DISCONNECTED,
    )

    reg = _fresh_registry()
    reg.report("t-conn", STATUS_CONNECTED, "up")
    reg.report("t-conn", STATUS_DISCONNECTED, "blip")  # 立刻断 → 窗口内挂起
    assert reg.status_of("t-conn")["status"] == "connected"
    # 窗口内重连 → 取消挂起
    reg.report("t-conn", STATUS_CONNECTED, "back")
    assert reg.status_of("t-conn")["status"] == "connected"


def test_registry_debounce_applies_after_window(ginno_home, monkeypatch):
    import time

    from ginno_runtime.connectors.registry import (
        STATUS_CONNECTED,
        STATUS_DISCONNECTED,
    )

    reg = _fresh_registry()
    reg.report("t-conn", STATUS_CONNECTED, "up")
    reg.report("t-conn", STATUS_DISCONNECTED, "gone")
    # 快进时间:窗口过后读取应应用挂起的断连
    st = reg._states["t-conn"]
    st._pending_at -= 10.0
    monkeypatch.setattr(time, "time", lambda: st._pending_at + 100.0)
    # 直接操作内部状态模拟窗口已过(status_of 内部用 time)
    st._pending_at = time.time() - 10.0
    assert reg.status_of("t-conn")["status"] == "disconnected"


def test_registry_force_bypasses_debounce(ginno_home):
    from ginno_runtime.connectors.registry import (
        STATUS_CONNECTED,
        STATUS_DISCONNECTED,
    )

    reg = _fresh_registry()
    reg.report("t-conn", STATUS_CONNECTED, "up")
    reg.report("t-conn", STATUS_DISCONNECTED, "real outage", force=True)
    assert reg.status_of("t-conn")["status"] == "disconnected"


def test_aggregate_dot_idle_and_error(ginno_home):
    from ginno_runtime.connectors.registry import STATUS_ERROR

    # idle 错误不点亮
    reg = _fresh_registry()
    reg.report("t-conn", STATUS_ERROR, "bad", extra={"idle": True})
    assert reg.aggregate_dot() == ""
    # 非 idle 错误 → 红点
    reg2 = _fresh_registry()
    reg2.report("t-conn", STATUS_ERROR, "bad")
    assert reg2.aggregate_dot() == "error"


def test_registry_config_roundtrip(ginno_home):
    reg = _fresh_registry()
    reg.write_config("t-conn", {"enabled": False})
    assert reg.read_config("t-conn")["enabled"] is False
    # 未写键回落默认
    assert "extra_missing" not in reg.read_config("t-conn")


# ---- event bus ------------------------------------------------------------


def test_events_bus_fanout_and_latest(ginno_home):
    from ginno_runtime.connectors.events import ConnectorEvents

    bus = ConnectorEvents()
    seen = []
    bus.subscribe(lambda t, d: seen.append((t, d)))
    bus.emit("page_pushed", {"url": "https://example.com", "title": "E"})
    assert seen == [("page_pushed", {"url": "https://example.com", "title": "E"})]
    assert bus.latest_page["url"] == "https://example.com"
    bus.emit("tool_progress", {"tool": "browser_computer", "stage": "done"})
    assert bus.latest_progress["stage"] == "done"
    # 坏监听者不影响其他监听者
    bus.subscribe(lambda t, d: 1 / 0)
    bus.emit("page_pushed", {"url": "https://x"})
    assert bus.latest_page["url"] == "https://x"


def test_events_bus_unsubscribe(ginno_home):
    from ginno_runtime.connectors.events import ConnectorEvents

    bus = ConnectorEvents()
    seen = []
    fn = lambda t, d: seen.append(t)  # noqa: E731
    bus.subscribe(fn)
    bus.unsubscribe(fn)
    bus.unsubscribe(fn)  # 幂等
    bus.emit("tool_progress", {})
    assert seen == []


# ---- browser config: sensitive domains -------------------------------------


def test_sensitive_match(ginno_home):
    from ginno_runtime.browser.config import BrowserConfig, load_browser_config

    cfg = BrowserConfig()
    assert cfg.sensitive_match("https://mail.google.com/u/0/") == "mail.google.com"
    assert cfg.sensitive_match("https://account.google.com") is None
    assert cfg.sensitive_match("https://sub.alipay.com/pay") == "alipay.com"
    assert cfg.sensitive_match("") is None
    # settings 持久化往返
    (ginno_home / "settings.json").write_text(json.dumps(
        {"browser": {"headless": True, "sensitive_domains": ["a.com"]}}))
    assert load_browser_config().sensitive_domains == ["a.com"]
    assert load_browser_config().headless is True


def test_fallback_mode_normalization(ginno_home):
    from ginno_runtime.browser.config import load_browser_config

    (ginno_home / "settings.json").write_text(json.dumps(
        {"browser": {"fallback_profile_mode": "weird"}}))
    assert load_browser_config().fallback_profile_mode == "ask"


# ---- browser tool helpers ----------------------------------------------------


def test_parse_modifiers(ginno_home):
    from ginno_runtime.tools.browser_tools import _parse_modifiers

    assert _parse_modifiers(None) == 0
    assert _parse_modifiers("ctrl") == 2
    assert _parse_modifiers("cmd+shift") == 4 | 8
    assert _parse_modifiers("CTRL+ALT") == 2 | 1


def test_normalize_url(ginno_home):
    from ginno_runtime.browser.cdp import CDPError
    from ginno_runtime.tools.browser_tools import _normalize_url

    assert _normalize_url("example.com") == "https://example.com"
    assert _normalize_url("http://a.b") == "http://a.b"
    assert _normalize_url("about:blank") == "about:blank"
    with pytest.raises(CDPError):
        _normalize_url("chrome://settings")
    with pytest.raises(CDPError):
        _normalize_url("javascript:alert(1)")
    with pytest.raises(CDPError):
        _normalize_url("")


def test_computer_action_validation(ginno_home):
    from ginno_runtime.tools.browser_tools import build_browser_tools

    tools = {t.name: t for t in build_browser_tools()}
    # 缺 coordinate/ref → 教学式错误,不发到后端
    r = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        tools["browser_computer"].ainvoke({"action": "left_click", "tabId": 1}))
    assert r.startswith("[error]") and "coordinate" in r
    r2 = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        tools["browser_computer"].ainvoke({"action": "unknown_x", "tabId": 1}))
    assert r2.startswith("[error]") and "unknown_x" in r2


def test_release_handoff_all_semantics(ginno_home):
    from ginno_runtime.tools import browser_tools as bt

    ev1, ev2 = asyncio.Event(), asyncio.Event()
    bt._handoff_events["1"] = ev1
    bt._handoff_events["2"] = ev2
    assert bt.release_handoff() is True  # release ALL
    assert ev1.is_set() and ev2.is_set()
    bt._handoff_events.clear()
    assert bt.release_handoff() is False
    assert bt.release_handoff("chrome-extension") is False  # 卡片无 tabId 路径


# ---- relay queue errors ------------------------------------------------------


@pytest.mark.asyncio()
async def test_relay_invoke_without_extension(ginno_home):
    from ginno_runtime.browser.relay import RelayError, invoke_tool, relay_state

    assert relay_state().connected is False
    with pytest.raises(RelayError) as ei:
        await invoke_tool("browser_tabs_context", {})
    assert "not connected" in str(ei.value)


# ---- relay endpoint: frame parse / response routing / disconnect cleanup ------
# (issue #19 — 可本地验证的扩展轨纯逻辑；真实 Chrome 端到端见
#  docs/browser-extension-manual-test-checklist.md)


class _FakeExtWS:
    """Duck-typed FastAPI WebSocket fed from an asyncio.Queue."""

    def __init__(self):
        self.incoming: asyncio.Queue[str] = asyncio.Queue()
        self.sent: list[dict] = []
        self.closed = False

    async def accept(self):
        pass

    async def send_text(self, raw: str):
        if self.closed:
            raise RuntimeError("connection closed")
        self.sent.append(json.loads(raw))

    async def receive_text(self) -> str:
        raw = await self.incoming.get()
        if raw == "__EOF__":
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return raw

    async def close(self):
        self.closed = True


@pytest.fixture()
def fresh_relay_state(monkeypatch):
    from ginno_runtime.browser import relay as relay_mod

    st = relay_mod.RelayState()
    monkeypatch.setattr(relay_mod, "_state", st, raising=False)
    if hasattr(relay_mod, "relay_state"):
        monkeypatch.setattr(relay_mod, "relay_state", lambda: st)
    yield st
    if st._worker and not st._worker.done():
        st._worker.cancel()


@pytest.mark.asyncio()
async def test_relay_endpoint_extension_info_and_snapshot(ginno_home, fresh_relay_state):
    from ginno_runtime.browser import relay as relay_mod

    ws = _FakeExtWS()
    await ws.incoming.put(json.dumps({
        "method": "extensionInfo",
        "params": {"version": "1.2.3", "browserType": "chrome",
                   "browserClientId": "cid-1", "capabilities": ["mcp-tools", "x"]},
    }))
    task = asyncio.create_task(relay_mod.extension_endpoint(ws))
    # extensionInfo 处理是顺序 await 之前的同步段——轮询等 snapshot 生效
    for _ in range(50):
        if fresh_relay_state.version == "1.2.3":
            break
        await asyncio.sleep(0.02)
    snap = fresh_relay_state.snapshot()
    assert snap["connected"] is True
    assert snap["version"] == "1.2.3"
    assert snap["browserType"] == "chrome"
    assert snap["browserClientId"] == "cid-1"
    assert "x" in snap["capabilities"]
    assert snap["transport"] == "websocket"
    # 连接即下发 tab scope（configChanged）——尽力而为，帧类型核对
    assert any(f.get("method") == "configChanged" for f in ws.sent)
    await ws.incoming.put("__EOF__")
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio()
async def test_relay_invoke_roundtrip_and_response_routing(ginno_home, fresh_relay_state):
    from ginno_runtime.browser import relay as relay_mod

    ws = _FakeExtWS()
    await ws.incoming.put(json.dumps({"method": "extensionInfo", "params": {}}))
    task = asyncio.create_task(relay_mod.extension_endpoint(ws))
    for _ in range(50):
        if fresh_relay_state.connected:
            break
        await asyncio.sleep(0.02)

    invoke = asyncio.create_task(
        relay_mod.invoke_tool("browser_navigate", {"url": "https://x"})
    )
    # FIFO worker 把 tools/invoke 发给扩展
    frame = None
    for _ in range(50):
        hits = [f for f in ws.sent if f.get("method") == "tools/invoke"]
        if hits:
            frame = hits[0]
            break
        await asyncio.sleep(0.02)
    assert frame is not None and frame["params"] == {
        "tool": "browser_navigate", "arguments": {"url": "https://x"}}
    # 扩展回包：按 id 路由回 pending future（response routing 纯逻辑）
    await ws.incoming.put(json.dumps({"id": frame["id"], "result": {"ok": True}}))
    assert await asyncio.wait_for(invoke, 5) == {"ok": True}

    # 错误回包：转成 RelayError
    invoke2 = asyncio.create_task(relay_mod.invoke_tool("t2", {}))
    frame2 = None
    for _ in range(50):
        hits = [f for f in ws.sent if f.get("method") == "tools/invoke"]
        if len(hits) >= 2:
            frame2 = hits[1]
            break
        await asyncio.sleep(0.02)
    await ws.incoming.put(json.dumps(
        {"id": frame2["id"], "error": {"code": "NO_TAB", "message": "no tab"}}))
    with pytest.raises(relay_mod.RelayError) as ei:
        await asyncio.wait_for(invoke2, 5)
    assert "NO_TAB" in str(ei.value)

    await ws.incoming.put("__EOF__")
    await asyncio.wait_for(task, 5)


@pytest.mark.asyncio()
async def test_relay_disconnect_fails_pending_and_resets_state(ginno_home, fresh_relay_state):
    from ginno_runtime.browser import relay as relay_mod

    ws = _FakeExtWS()
    await ws.incoming.put(json.dumps({"method": "extensionInfo", "params": {}}))
    task = asyncio.create_task(relay_mod.extension_endpoint(ws))
    for _ in range(50):
        if fresh_relay_state.connected:
            break
        await asyncio.sleep(0.02)

    invoke = asyncio.create_task(relay_mod.invoke_tool("t", {}))
    for _ in range(50):
        if any(f.get("method") == "tools/invoke" for f in ws.sent):
            break
        await asyncio.sleep(0.02)
    # 未回包即断连 → pending future 收 RelayError，状态复位
    await ws.incoming.put("__EOF__")
    await asyncio.wait_for(task, 5)
    with pytest.raises(relay_mod.RelayError) as ei:
        await asyncio.wait_for(invoke, 5)
    assert "断开" in str(ei.value) or "lost" in str(ei.value)
    assert fresh_relay_state.connected is False
    assert fresh_relay_state.snapshot()["connected"] is False


@pytest.mark.asyncio()
async def test_relay_malformed_frames_ignored(ginno_home, fresh_relay_state):
    """坏 JSON / 未知 method 静默忽略，连接保持。"""
    from ginno_runtime.browser import relay as relay_mod

    ws = _FakeExtWS()
    await ws.incoming.put("not-json{{")
    await ws.incoming.put(json.dumps({"method": "unknownMethod"}))
    await ws.incoming.put(json.dumps({"method": "extensionInfo", "params": {}}))
    task = asyncio.create_task(relay_mod.extension_endpoint(ws))
    for _ in range(50):
        if fresh_relay_state.connected:
            break
        await asyncio.sleep(0.02)
    assert fresh_relay_state.connected is True
    await ws.incoming.put("__EOF__")
    await asyncio.wait_for(task, 5)



# ---- executor helpers ---------------------------------------------------------


def test_jpeg_size_parses_sof(ginno_home):
    from ginno_runtime.browser.executor import _jpeg_size

    # 构造最小 SOF0 段:FF D8( SOI)… FF C0 len h w
    seg = (b"\xff\xd8" + b"\x00" * 8 + b"\xff\xc0"
           + struct.pack(">H", 15) + b"\x08" + struct.pack(">HH", 720, 1280))
    assert _jpeg_size(seg) == (1280, 720)
    assert _jpeg_size(b"\xff\xd8\xff\xe0") is None  # 无 SOF → None


# ---- scripts: both tracks share one source ------------------------------------


def test_scripts_contain_ref_system(ginno_home):
    from ginno_runtime.browser import scripts

    for src in scripts.BRIDGE_SOURCES:
        assert isinstance(src, str) and len(src) > 500
    at = scripts.ACCESSIBILITY_TREE_JS
    assert "__ginnoAT" in at and "ref_" in at and "WeakRef" in at
    assert "resolve" in at  # bridge 依赖的 ref 解析入口
    bridge = scripts.PAGE_BRIDGE_JS
    assert "__ginnoBridge" in bridge
    assert "pageText" in bridge and "fill" in bridge and "find" in bridge


# ---- native host protocol -------------------------------------------------------


def test_host_port_file(ginno_home):
    from ginno_runtime.browser.host_protocol import port_file, read_port

    (ginno_home / "browser-relay-port").write_text("8787")
    assert port_file() == ginno_home / "browser-relay-port"
    assert read_port() == 8787


def test_host_reassembler(ginno_home):
    from ginno_runtime.browser.host_protocol import Reassembler

    r = Reassembler()
    assert r.feed({"type": "relayData", "data": "whole"}) == "whole"
    assert r.feed({"type": "relayDataChunk", "messageId": "m",
                   "chunkIndex": 0, "chunkCount": 2, "data": "a"}) is None
    assert r.feed({"type": "relayDataChunk", "messageId": "m",
                   "chunkIndex": 1, "chunkCount": 2, "data": "b"}) == "ab"
    assert r.feed({"type": "other"}) is None


def test_materialize_version_compare(ginno_home, monkeypatch, tmp_path):

    from ginno_runtime.browser import native_host as nh
    from ginno_runtime.browser.config import extension_dir

    # frozen 布局:<mip>/extension_src/{manifest,background,content-scripts}
    mip = tmp_path / "mip"
    src = mip / "extension_src"
    (src / "content-scripts").mkdir(parents=True)
    (src / "manifest.json").write_text('{"version": "9.9.9"}')
    (src / "background.js").write_text("// bg")
    (src / "content-scripts" / "visual-indicator.js").write_text("// vi")
    monkeypatch.setattr(nh, "_materialize_candidates", lambda: [src])
    out = nh.materialize_extension()
    assert out == extension_dir()
    assert json.loads((out / "VERSION.json").read_text())["version"] == "9.9.9"
    # 同版本重跑:不触碰文件(Chrome 不热重载)
    bg = out / "background.js"
    mtime = bg.stat().st_mtime_ns
    import time as _t

    _t.sleep(0.02)
    nh.materialize_extension()
    assert bg.stat().st_mtime_ns == mtime


@pytest.mark.unit
def test_materialize_generates_locales(ginno_home, monkeypatch, tmp_path):
    """物化冒烟(i18n-design.md §8):materialize_extension 之后
    _locales/{en,zh_CN}/messages.json 存在、en/zh key 集合一致、manifest 已
    改写 __MSG_* + default_locale——打包安装的扩展才有中文名/中文文案。

    扩展源用最小 fixture(同 test_materialize_version_compare);ext catalog
    走真实仓库的 apps/web/messages(物化在 dev checkout 下的真实数据源),
    找不到时跳过(非仓库布局的安装环境)。"""

    from ginno_runtime.browser import native_host as nh
    from ginno_runtime.browser.config import extension_dir

    mip = tmp_path / "mip"
    src = mip / "extension_src"
    (src / "content-scripts").mkdir(parents=True)
    (src / "manifest.json").write_text(
        json.dumps({"version": "9.9.9",
                    "action": {"default_title": "Ginno"}}))
    (src / "background.js").write_text("// bg")
    (src / "content-scripts" / "visual-indicator.js").write_text("// vi")
    monkeypatch.setattr(nh, "_materialize_candidates", lambda: [src])

    from ginno_runtime.browser import ext_locales as extl

    catalogs = None
    for root in nh._ext_messages_candidates():
        catalogs = extl.load_catalogs(root)
        if catalogs is not None:
            break
    if catalogs is None:
        pytest.skip("web ext.json catalogs not found (non-repo layout)")

    out = nh.materialize_extension()
    assert out == extension_dir()

    en_path = out / "_locales" / "en" / "messages.json"
    zh_path = out / "_locales" / "zh_CN" / "messages.json"
    assert en_path.is_file(), "materialized extension missing en _locales"
    assert zh_path.is_file(), "materialized extension missing zh_CN _locales"
    en_msgs = json.loads(en_path.read_text())
    zh_msgs = json.loads(zh_path.read_text())
    assert set(en_msgs) == set(zh_msgs), "en/zh_CN _locales key sets diverge"
    # manifest __MSG_* 引用的 message 必须真实存在,否则 Chrome 拒载
    for ref in ("ext_manifest_name", "ext_manifest_description",
                "ext_manifest_defaultTitle"):
        assert ref in en_msgs and ref in zh_msgs
    mf = json.loads((out / "manifest.json").read_text())
    assert mf["default_locale"] == "en"
    assert mf["name"] == "__MSG_ext_manifest_name__"


# ---- lazy browser skill: disabled-state gating ---------------------------- #
def _browser_settings(ginno_home, enabled: bool):
    (ginno_home / "settings.json").write_text(
        json.dumps({"browser": {"enabled": enabled}}), encoding="utf-8"
    )


def test_browser_skill_hidden_when_disabled(ginno_home):
    """settings browser.enabled=false → the builtin skill must vanish from the
    skills index (its tools don't exist; advertising it would mislead)."""
    from ginno_runtime.world_state import SessionCtx, SkillsSection

    _browser_settings(ginno_home, False)
    ctx = SessionCtx(session_id="s", project_slug="default", agent_id="dev")
    snap = SkillsSection().snapshot(ctx)
    assert "browser" not in snap["names"]
    assert "- browser:" not in snap["index"]

    _browser_settings(ginno_home, True)
    snap = SkillsSection().snapshot(ctx)
    assert "browser" in snap["names"]
    assert "- browser:" in snap["index"]


def test_use_skill_browser_errors_when_disabled(ginno_home):
    _browser_settings(ginno_home, False)
    from ginno_runtime.tools.skill_tools import build_skill_tools

    tools = {t.name: t for t in build_skill_tools("default", "s", "")}
    out = tools["use_skill"].invoke({"name": "browser", "request": "x"})
    assert out.startswith("[error]")
    assert "disabled" in out


def test_slash_browser_passthrough_when_disabled(ginno_home):
    _browser_settings(ginno_home, False)
    from ginno_runtime.commands.resolver import substitute_skill

    text, name = substitute_skill("/browser open example.com", "default")
    assert name is None  # not substituted — falls through as a plain message

    _browser_settings(ginno_home, True)
    text, name = substitute_skill("/browser open example.com", "default")
    assert name == "browser"
    assert "browser_tabs_context" in text  # skill body substituted


def test_sticky_frontmatter_parsed():
    """The builtin browser/todo skills declare `sticky: true` — the flag that
    keeps their lazily-gated tool families bound for the whole session."""
    from ginno_runtime.skills.loader import SkillLoader

    loader = SkillLoader(project_slug="default")
    assert loader.get("browser").sticky is True
    assert loader.get("todo").sticky is True


def test_slash_sticky_appends(isolated_home, monkeypatch, tmp_path):
    """turn.py's slash path feeds sticky_skills with the invoked skill only
    when it declares sticky; [] otherwise (append reducer no-op)."""
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    (tmp_path / "settings.json").write_text("{}")
    from ginno_runtime.api.stream.turn import _slash_sticky

    assert _slash_sticky("browser", "default") == ["browser"]
    assert _slash_sticky("todo", "default") == ["todo"]
    assert _slash_sticky(None, "default") == []
    # a non-sticky user skill (or unknown name) contributes nothing
    d = tmp_path / "skills" / "plain"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: plain\ndescription: p\ntrigger: both\n---\n\nB.\n", encoding="utf-8"
    )
    assert _slash_sticky("plain", "default") == []
    assert _slash_sticky("nope", "default") == []
