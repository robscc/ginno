#!/usr/bin/env python3
"""一键重截 README 用 UI 截图（docs/smoke/*.png）。

流程：temp GINNO_HOME + 独立端口起 **打包版** dist sidecar（GINNO_FAKE_LLM
脚本化回复，含 render_widget 演示），Playwright 驱动真实 Chromium 完成两轮
对话后逐页截图。不碰用户的 ~/.ginno 与 8787 端口。

用法：
    cd packages/runtime && uv run --group test python ../../scripts/screenshot_ui.py
    # 可选: --port 8791 --lang zh-CN --out ../docs/smoke --keep(保留临时home调试)

前置：dist sidecar 已构建（make runtime）、playwright + chromium 已安装。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "packages" / "runtime"
RUNTIME_BIN = RUNTIME / "dist" / "ginno-runtime" / "ginno-runtime"
OUT_DIR_DEFAULT = ROOT / "docs" / "smoke"

# ---------------------------------------------------------------- fake scripts

# 两轮「演示对话」：第一轮触发 render_widget 工具调用（no-op 工具，WS 层
# 拦截发 widget.emit → 前端渲染真实卡片），随后给富 markdown 总结；第二轮
# 纯文本短答。usage 让 TopBar 的用量 pill 有真实数据。
SCRIPTS = [
    {
        "content": "我来看一下你最近的项目活动。",
        "tool_calls": [
            {
                "name": "render_widget",
                "args": {
                    "kind": "stat_list",
                    "data": {
                        "title": "本周项目活动总览",
                        "items": [
                            {"label": "会话产出", "value": "12 份报告", "status": "done"},
                            {"label": "代码提交", "value": "23 commits", "status": "ok"},
                            {"label": "CI 状态", "value": "三栈全绿", "status": "done"},
                            {"label": "知识库沉淀", "value": "5 篇 Wiki", "status": "ok"},
                        ],
                    },
                },
            }
        ],
        "usage": {
            "input_tokens": 1580,
            "output_tokens": 96,
            "total_tokens": 1676,
            "input_token_details": {"cache_read": 1200, "cache_creation": 180},
        },
    },
    {
        "content": (
            "整体节奏很健康，几个亮点：\n\n"
            "1. **产出密度高** —— 本周 12 份报告覆盖三个方向；\n"
            "2. **工程线收口** —— 三栈 CI 全绿，发布流水线打通；\n"
            "3. **知识沉淀** —— 5 篇 Wiki 进库，下次可直接引用。\n\n"
            "| 维度 | 本周 | 上周 |\n|---|---|---|\n"
            "| 报告 | 12 | 8 |\n| commits | 23 | 17 |\n"
            "\n建议：把「知识沉淀」环节固定成周五的收尾动作。"
        ),
        "usage": {
            "input_tokens": 2100,
            "output_tokens": 210,
            "total_tokens": 2310,
            "input_token_details": {"cache_read": 1800, "cache_creation": 120},
        },
    },
    {
        "content": "已把这条建议加进本周 TODO（关联产物已挂）。要我现在起草周五收尾 checklist 吗？",
        "usage": {
            "input_tokens": 2450,
            "output_tokens": 48,
            "total_tokens": 2498,
            "input_token_details": {"cache_read": 2200, "cache_creation": 60},
        },
    },
]


def _settings(lang: str) -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": lang,
        "providers": {
            "custom": {
                "enabled": True,
                "protocol": "openai-compatible",
                "name": "demo",
                "api_key": "k",
                "base_url": "http://127.0.0.1:1",
                "model": "m",
                "max_tokens": 100,
                "temperature": 0.7,
                "timeout_s": 5,
            }
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


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


def _wait_health(port: int, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"sidecar on :{port} did not become healthy in {timeout}s")


def _launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False
        )
        return pw.chromium.launch()



def _first_session_id(page) -> str | None:
    return page.evaluate(
        "fetch('/api/sessions?project_slug=default').then(r=>r.json()).then(j=>j.length?j[0].id:null)"
    )


def _send_and_wait(page, text: str, expect_messages: int, timeout: float = 90):
    ta = page.locator("textarea").first
    ta.click()
    ta.fill(text)
    ta.press("Enter")
    # 注意：URL 必须用 Python f-string 拼好再进 evaluate——JS 模板字符串的
    # `${sid}` 在 page.evaluate 的字符串表达式里不会按预期插值（实测恒取
    # 空，轮询永不满足）。
    deadline = time.time() + timeout
    sid = None
    while time.time() < deadline:
        sid = _first_session_id(page)
        n = (
            page.evaluate(
                f"fetch('/api/sessions/{sid}/history').then(r=>r.json()).then(j=>(j.messages||[]).length)"
            )
            if sid
            else -1
        )
        if sid and n >= expect_messages:
            return sid
        page.wait_for_timeout(400)
    # 失败现场：截图 + API 状态 dump，便于诊断
    import pathlib

    dbg = pathlib.Path("/tmp/ginno-shot-debug")
    dbg.mkdir(exist_ok=True)
    try:
        page.screenshot(path=str(dbg / "fail.png"))
        sessions = page.evaluate("fetch('/api/sessions?project_slug=default').then(r=>r.json())")
        print(f"[diag] sessions={json.dumps(sessions, ensure_ascii=False)[:500]}", file=sys.stderr)
        if sid:
            hist = page.evaluate(
                f"fetch('/api/sessions/{sid}/history').then(r=>r.json())"
            )
            print(
                f"[diag] history({sid})={json.dumps(hist, ensure_ascii=False)[:1200]}",
                file=sys.stderr,
            )
    except Exception as e:  # noqa: BLE001
        print(f"[diag] dump failed: {e}", file=sys.stderr)
    raise RuntimeError(f"turn did not complete in {timeout}s (text={text!r})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--lang", default="zh-CN")
    ap.add_argument("--out", default=str(OUT_DIR_DEFAULT))
    ap.add_argument("--keep", action="store_true", help="保留临时 GINNO_HOME 便于调试")
    args = ap.parse_args()

    if not RUNTIME_BIN.exists():
        print("❌ dist sidecar 不存在，先跑 make runtime", file=sys.stderr)
        return 2
    if _port_open(args.port):
        print(f"❌ 端口 {args.port} 被占用", file=sys.stderr)
        return 2

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    home = Path(tempfile.mkdtemp(prefix="ginno-shots-"))

    (home / "settings.json").write_text(json.dumps(_settings(args.lang)), encoding="utf-8")
    scripts_file = home / "scripts.json"
    scripts_file.write_text(json.dumps(SCRIPTS), encoding="utf-8")

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts_file),
        GINNO_RUNTIME_PORT=str(args.port),
    )
    proc = subprocess.Popen(
        [str(RUNTIME_BIN)],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )

    from playwright.sync_api import sync_playwright

    taken: list[Path] = []
    try:
        _wait_health(args.port)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(f"http://127.0.0.1:{args.port}/", wait_until="load")
            page.wait_for_timeout(2500)

            # ---- 1. 聊天工作台：两轮对话 → widget 卡 + markdown 渲染 ----
            # 第一轮折叠为 2 条（user + assistant[widget+text]：no-op 工具
            # 不落 ToolMessage，widget 卡与文本并入同一条 assistant blocks）
            _send_and_wait(page, "帮我看看本周的项目活动，先给个总览", 2, timeout=90)
            page.wait_for_timeout(600)  # widget 卡片动画
            _send_and_wait(page, "很好，总结一下并给我一条行动建议", 4)
            page.wait_for_timeout(1200)  # markdown 表格/列表渲染
            p = out / "ui-workspace.png"
            page.screenshot(path=str(p))
            taken.append(p)

            # ---- 2. Workflow Studio（lifespan 自动种子配方）----
            page.goto(f"http://127.0.0.1:{args.port}/workflows", wait_until="load")
            page.wait_for_timeout(2500)
            p = out / "ui-workflow-tab.png"
            page.screenshot(path=str(p))
            taken.append(p)

            # ---- 3. 知识库（无数据时的引导态也如实呈现）----
            page.goto(f"http://127.0.0.1:{args.port}/kb", wait_until="load")
            page.wait_for_timeout(2500)
            p = out / "kb-overview.png"
            page.screenshot(path=str(p))
            taken.append(p)

            browser.close()
    finally:
        # PyInstaller bootloader 对 TERM 不可靠：TERM→宽限→KILL 整个进程组
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            time.sleep(2)
            if proc.poll() is None:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        proc.wait()
        if not args.keep:
            shutil.rmtree(home, ignore_errors=True)

    for p in taken:
        print(f"✅ {p}  ({p.stat().st_size // 1024} KB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
