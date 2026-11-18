"""Packaged-UI e2e: channel (read-only) sessions must NOT show the compact card.

2026-11-18 截图实录：渠道只读会话（企微/钉钉，桌面侧标注「此处只读：回复在
聊天应用里进行」）在上下文接近压缩阈值时，顶部仍弹出「上下文接近压缩阈值」
提示条，右侧带「立即压缩」按钮。只读会话的操作入口已全部收掉（#170 渠道
readonly），压缩动作属于 IM 侧对话流（agent 自动压缩 / 用户在 IM 侧
/compact），桌面侧触发语义错误。

前端修复：filterVisibleSuggestions（apps/web/src/components/chat/stream/
suggestionFilter.ts）对 type="channel" 会话过滤所有 action.type=="compact"
的卡片（context.pressure 与 failure.context_length），open_settings /
switch_model 卡保留。

构造方式（不走真对话——channel 会话的 WS invoke 被 #170 readonly 闸拒绝）：
POST /api/sessions 建 channel 会话 → 直接向 GINNO_HOME/usage/ 的当日 jsonl
追加一条近阈值 input_tokens 记录（context.pressure 的判定源
usage_store.latest_record）→ 浏览器打开会话（打开即拉 suggestions）断言
卡片/按钮不出现；对照组普通会话同 seed，卡片与「Compact now」按钮出现。

sidecar 运行形态：优先 dist bundle（CI make runtime 后，web_out 打包自同一
commit、含本修复）；无 dist 时回退源码模式 .venv/bin/ginno-runtime
（server._web_out_dir 的 dev 回退会 serve 仓库 apps/web/out，同样是当前代码
的 web）。两种形态的前端都来自当前 checkout。
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

_REPO = Path(__file__).resolve().parents[4]
_RUNTIME_DIST = _REPO / "packages" / "runtime" / "dist" / "ginno-runtime"
_DIST_BIN = _RUNTIME_DIST / "ginno-runtime"
_VENV_BIN = _REPO / "packages" / "runtime" / ".venv" / "bin" / "ginno-runtime"
_WEB_OUT = _REPO / "apps" / "web" / "out"
PORT = 8899

# custom provider 显式 context_window=100_000 → threshold=min(500k, 75k)=75_000；
# seed input_tokens=70_000 = threshold 的 93%（≥85% 黄卡阈值，必然触发）。
WINDOW = 100_000
SEED_TOKENS = 70_000


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


def _api(port: int, path: str, payload: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _settings() -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "en",
        "providers": {
            "custom": {
                "enabled": True,
                "protocol": "openai-compatible",
                "name": "t",
                "api_key": "k",
                "base_url": "http://127.0.0.1:1",
                "model": "m",
                "max_tokens": 100,
                "temperature": 0.7,
                "timeout_s": 5,
                "context_window": WINDOW,
            },
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _seed_usage(home: Path, sid: str, provider: str, model: str) -> None:
    """向当日 usage jsonl 追加一条近阈值记录（context.pressure 的判定源）。"""
    day = time.strftime("%Y-%m-%d", time.localtime())
    usage_dir = home / "usage"
    usage_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": time.time(),
        "session_id": sid,
        "project_slug": "default",
        "agent_id": None,
        "turn_id": None,
        "source": "chat",
        "provider": provider,
        "model": model,
        "input_tokens": SEED_TOKENS,
        "output_tokens": 10,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "reasoning_tokens": 0,
        "latency_ms": 100,
        "ok": True,
        "error": None,
    }
    with open(usage_dir / f"requests-{day}.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _pick_runtime_bin() -> Path:
    if _DIST_BIN.exists():
        return _DIST_BIN
    if _VENV_BIN.exists():
        return _VENV_BIN
    pytest.skip("no runtime binary: build dist (make runtime) or uv sync for source mode")


def test_packaged_ui_channel_session_hides_compact_suggestion(tmp_path):
    if not _WEB_OUT.exists():
        pytest.skip("web out not built (run `pnpm build` in apps/web)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    if _port_open(PORT):
        pytest.skip(f"port {PORT} already in use")

    bin_path = _pick_runtime_bin()

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))

    env = dict(os.environ, GINNO_HOME=str(home), GINNO_FAKE_LLM="1", GINNO_RUNTIME_PORT=str(PORT))
    proc = sc.spawn_sidecar(str(bin_path), env)
    try:
        _wait_health(PORT)

        # 渠道只读会话 + 普通会话（对照组），provider 都指向 custom（窗口可解析）
        r_ch = _api(PORT, "/api/sessions", {
            "project_slug": "default", "workspace": "/tmp",
            "type": "channel", "channel": "wecom", "provider": "custom", "title": "RO wecom",
        })
        assert r_ch.get("ok"), r_ch
        sid_ch = r_ch.get("session_id") or r_ch.get("id")
        r_main = _api(PORT, "/api/sessions", {
            "project_slug": "default", "workspace": "/tmp",
            "provider": "custom", "title": "Plain main",
        })
        assert r_main.get("ok"), r_main
        sid_main = r_main.get("session_id") or r_main.get("id")

        # 两组都 seed 近阈值 usage → context.pressure 判定成立
        _seed_usage(home, sid_ch, "custom", "m")
        _seed_usage(home, sid_main, "custom", "m")

        # API 层先验：channel 会话的 suggestions 里 compact 卡已被前端过滤的前提
        # 是后端确实返回了它（过滤在前端）——钉住判定源成立
        sugg_ch = _api(PORT, f"/api/sessions/{sid_ch}/suggestions")
        ids_ch = [s["id"] for s in sugg_ch.get("suggestions", [])]
        assert "context.pressure" in ids_ch, (
            f"seed must reproduce the bug precondition (backend emits pressure): {sugg_ch}"
        )
        sugg_main = _api(PORT, f"/api/sessions/{sid_main}/suggestions")
        assert "context.pressure" in [s["id"] for s in sugg_main.get("suggestions", [])]

        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/", wait_until="load")
            page.wait_for_timeout(1500)

            # ── 渠道只读会话：卡片整条不出现，「Compact now」按钮不存在 ──
            page.get_by_text("RO wecom", exact=False).first.click()
            page.wait_for_timeout(1200)
            assert page.get_by_text("上下文接近压缩阈值").count() == 0, (
                "channel session must not show the context.pressure card"
            )
            assert page.get_by_text("Compact now", exact=False).count() == 0, (
                "channel session must not offer the compact action"
            )

            # ── 对照组普通会话：同 seed 下卡片与按钮照常出现 ──
            page.get_by_text("Plain main", exact=False).first.click()
            page.wait_for_timeout(1200)
            assert page.get_by_text("上下文接近压缩阈值").count() >= 1, (
                "regular session should still show the pressure card (control group)"
            )
            assert page.get_by_text("Compact now", exact=False).count() >= 1, (
                "regular session should still offer compact (control group)"
            )
            browser.close()
    finally:
        sc.stop_sidecar(proc)
