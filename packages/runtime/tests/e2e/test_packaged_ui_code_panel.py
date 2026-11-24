"""Packaged-UI e2e: Code 面板核心交互（重构批次 3-3 hooks 化拆分的守卫用例）.

2026-11-12 起的规矩（CLAUDE.md 红线）：前端交互改动必须配 packaged UI 用例——
vitest/tsc 抓不到渲染层断裂。CodePanel.tsx（1666 行）拆成 panel/* 的 6 个域
hook + 3 个子组件后，对外 props 与行为承诺零变化；本文件在**浏览器级**钉住
面板的端到端交互链（拆分等价性）：

A. 打开→关闭生命周期：API 造文件 → 切「代码」tab → FileTree 渲染 session
   工作区 → 点击树项 → EditorTabs 出现该 tab →（文件模式树自动收起）→
   点 tab 关闭按钮 → 回到浏览态（tablist 消失、树重新占满）。
B. ⌘P quick-open：多文件时键盘唤起 QuickOpen 对话框 → 输入过滤 → 回车
   打开目标文件成 tab（打开文件的另一条入口，useCodeTabs 的 openFile）。
C. ⌘B 树开关：非浏览态下 ⌘B 唤出 overlay drawer（树可见）再收起。
D. 本地静态资源：代码高亮、冲突双方高亮和 editor worker 的真实 diff 回复。

与 test_packaged_ui_llm_events.py 同基建：dist sidecar + 真实 Chromium，
GINNO_HOME 隔离 tmp_path，bind(0) 随机空闲口（不碰 8787），语言钉 zh-CN
（断言词取自 messages/zh-CN/code.json）。文件由 sidecar 的 HTTP API 造
（mkdir → read → write 带版本），不经 UI——本文件测的是读侧交互。

GINNO_E2E_RUNTIME=source 使用源码 sidecar 提供同一静态导出。
GINNO_E2E_STRICT=1 时，缺少 sidecar 或 Chromium 会报错。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from contextlib import contextmanager
from urllib.parse import urlencode, urlsplit

import _sidecar as sc
import pytest
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e

# zh-CN 断言词（messages/zh-CN/code.json）
CODE_TAB_LABEL = "代码"        # right.tabs.code
TREE_LABEL = "文件树"          # code.tree.label
QUICK_ARIA = "快速打开文件"     # code.quick.aria
QUICK_PLACEHOLDER = "按文件名搜索…"  # code.quick.placeholder


def _settings() -> dict:
    # language pin zh-CN；fake LLM 模式下 provider 细节无关紧要。
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "language": "zh-CN",
        "providers": {
            "custom": {"enabled": True, "protocol": "openai-compatible", "name": "t",
                       "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m",
                       "default_model": "m", "max_tokens": 100, "temperature": 0.7,
                       "timeout_s": 5},
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _api(port: int, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode())


def _sessions(port: int) -> list:
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/sessions?project_slug=default", timeout=5
    ) as r:
        return json.loads(r.read().decode())


def _wait_session(port: int, pred, timeout: float = 20) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for s in _sessions(port):
            if pred(s):
                return s
        time.sleep(0.3)
    raise AssertionError("等待的会话未出现")


def _make_file(port: int, session_id: str, path: str, content: str) -> None:
    """经 sidecar API 在 session 工作区造一个带内容的文件（mkdir→read→write）."""
    r = _api(port, "POST", "/api/code/mkdir", {
        "project_slug": "default",
        "session_id": session_id,
        "root": "session",
        "path": path,
        "kind": "file",
    })
    assert r.get("ok"), f"mkdir {path} 失败: {r}"
    _write_file(port, session_id, path, content)


def _write_file(port: int, session_id: str, path: str, content: str) -> None:
    qs = urlencode(
        {
            "project_slug": "default",
            "session_id": session_id,
            "root": "session",
            "path": path,
        }
    )
    with urllib.request.urlopen(
        f"http://127.0.0.1:{port}/api/code/read?{qs}",
        timeout=10,
    ) as r:
        rd = json.loads(r.read().decode())
    assert rd.get("ok"), f"read {path} 失败: {rd}"
    w = _api(port, "PUT", "/api/code/write", {
        "project_slug": "default",
        "session_id": session_id,
        "root": "session",
        "path": path,
        "content": content,
        "base_version": rd["version"],
        "encoding": "utf-8",
    })
    assert w.get("ok"), f"write {path} 失败: {w}"


def _send_first_message(page, text: str) -> None:
    ta = page.locator("textarea").first
    ta.wait_for(state="visible", timeout=30_000)
    ta.fill(text)
    ta.press("Enter")


def _launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False
        )
        return pw.chromium.launch()


@contextmanager
def _boot(tmp_path):
    smoke._require_runtime()
    from playwright.sync_api import sync_playwright

    port = smoke._free_port()
    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()), encoding="utf-8")
    env = dict(os.environ, GINNO_HOME=str(home), GINNO_RUNTIME_PORT=str(port))
    env["GINNO_FAKE_LLM"] = "1"
    with (tmp_path / "sidecar-stderr.log").open("wb") as log:
        if smoke.RUNTIME_MODE == "source":
            proc = subprocess.Popen(
                ["uv", "run", "--no-sync", "ginno-runtime"],
                cwd=smoke._REPO, env=env, stdout=subprocess.DEVNULL, stderr=log,
                start_new_session=True,
            )
        else:
            proc = sc.spawn_sidecar(str(smoke.RUNTIME_BIN), env, err=log)
        try:
            smoke._wait_health(port)
            with sync_playwright() as pw:
                browser = pw.chromium.launch() if smoke.STRICT else _launch_chromium(pw)
                page = browser.new_page()
                page.add_init_script("localStorage.setItem('ginno.language', 'zh-CN')")
                page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
                yield page, port
                browser.close()
        finally:
            sc.stop_sidecar(proc)


@pytest.fixture()
def app(tmp_path):
    with _boot(tmp_path) as pair:
        yield pair


def _open_code_tab(page) -> None:
    page.get_by_role("tab", name=CODE_TAB_LABEL, exact=False).first.click()


def test_code_panel_open_and_close_tab(app):
    page, port = app
    _send_first_message(page, "hello code panel")
    session = _wait_session(port, lambda x: True)
    _make_file(port, session["id"], "notes.md", "# panel-e2e\n拆分后仍可打开文件。")

    _open_code_tab(page)

    # A1: 树渲染（session 工作区默认根），造好的文件可见
    tree = page.get_by_role("tree", name=TREE_LABEL)
    tree.wait_for(state="visible", timeout=20_000)
    item = page.get_by_role("treeitem", name="notes.md", exact=False).first
    item.wait_for(state="visible", timeout=20_000)

    # A2: 点击树项 → 该文件成 tab（useCodeTabs.openFile）
    item.click()
    tab = page.get_by_role("tab", name="notes.md", exact=False).first
    tab.wait_for(state="visible", timeout=20_000)

    # A3: 打开文件后退出浏览态——窄面板 file 模式下树收起（布局自动切换）
    page.wait_for_timeout(500)
    assert page.get_by_role("tree", name=TREE_LABEL).count() == 0, (
        "打开文件后（<600px file 模式）树应收起为 ⌘B 抽屉"
    )

    # A4: 点 tab 关闭按钮 → 回到浏览态：tablist 消失、树重新占满
    tab.locator("button").first.click()
    page.wait_for_timeout(400)
    assert page.get_by_role("tab", name="notes.md", exact=False).count() == 0, (
        "关闭后 tab 应消失"
    )
    page.get_by_role("tree", name=TREE_LABEL).first.wait_for(state="visible", timeout=10_000)


def test_code_panel_quick_open(app):
    page, port = app
    _send_first_message(page, "hello quick open")
    session = _wait_session(port, lambda x: True)
    _make_file(port, session["id"], "alpha.md", "A")
    _make_file(port, session["id"], "beta-zeta.md", "B")

    _open_code_tab(page)
    page.get_by_role("tree", name=TREE_LABEL).wait_for(state="visible", timeout=20_000)

    # B1: ⌘P 唤起 QuickOpen 对话框
    page.keyboard.press("Meta+p")
    dialog = page.get_by_role("dialog", name=QUICK_ARIA)
    dialog.wait_for(state="visible", timeout=10_000)
    inp = dialog.get_by_placeholder(QUICK_PLACEHOLDER)
    inp.wait_for(state="visible", timeout=5_000)

    # B2: 过滤 + 回车 → 目标文件成 tab，对话框关闭。
    # 注意：树里也有同名文件行——结果行必须限定在 dialog 内等待（否则
    # wait_for 匹配到树行、Enter 时 hits 尚空 → no-op）。
    inp.fill("beta-zeta")
    dialog.get_by_text("beta-zeta.md", exact=False).first.wait_for(
        state="visible", timeout=15_000
    )
    inp.press("Enter")
    assert page.get_by_role("dialog", name=QUICK_ARIA).count() == 0, (
        "选中后 QuickOpen 应关闭"
    )


def test_code_panel_cmd_b_drawer(app):
    page, port = app
    _send_first_message(page, "hello drawer")
    session = _wait_session(port, lambda x: True)
    _make_file(port, session["id"], "drawer.md", "D")

    _open_code_tab(page)
    tree = page.get_by_role("tree", name=TREE_LABEL)
    tree.first.wait_for(state="visible", timeout=20_000)

    # 打开文件 → file 模式树收起（A3 已证）；⌘B 唤出 overlay 抽屉（树再现）
    page.get_by_role("treeitem", name="drawer.md", exact=False).first.click()
    page.get_by_role("tab", name="drawer.md", exact=False).first.wait_for(
        state="visible", timeout=10_000
    )
    page.wait_for_timeout(400)
    assert page.get_by_role("tree", name=TREE_LABEL).count() == 0

    page.keyboard.press("Meta+b")
    tree.first.wait_for(state="visible", timeout=10_000)

    # 再 ⌘B 收起（toggleTree）
    page.keyboard.press("Meta+b")
    page.wait_for_timeout(400)
    assert page.get_by_role("tree", name=TREE_LABEL).count() == 0, (
        "再次 ⌘B 应收起抽屉"
    )



def _localhost_only(page, port: int) -> None:
    def local_route(route):
        if urlsplit(route.request.url).netloc == f"127.0.0.1:{port}":
            route.continue_()
        else:
            route.abort()

    page.context.route("**/*", local_route)


def _expect_token(editor, text: str) -> None:
    from playwright.sync_api import expect

    # A plaintext model renders the entire line as a single span. Require
    # Monaco's token class and an exact token boundary within the source line.
    token = editor.locator('.view-lines span[class^="mtk"]').filter(
        has_text=re.compile(rf"^{re.escape(text)}$")
    )
    expect(token.first).to_be_visible(timeout=15_000)


@pytest.mark.parametrize(
    ("path", "content", "token"),
    [
        ("sample.js", "const value = 101;", "const"),
        ("sample.py", "def sample(): pass", "def"),
        ("sample.json", '{"value": 101}', "101"),
        ("sample.yaml", "value: 101", "101"),
    ],
    ids=["javascript", "python", "json", "yaml"],
)
def test_code_panel_highlights_from_local_export(app, path, content, token):
    page, port = app
    _localhost_only(page, port)
    _send_first_message(page, "hello highlighting")
    session = _wait_session(port, lambda x: True)
    _make_file(port, session["id"], path, content)

    _open_code_tab(page)
    page.get_by_role("treeitem", name=path, exact=False).first.click()
    _expect_token(page.locator(".monaco-editor"), token)


def test_code_panel_typescript_conflict_diff_uses_editor_worker(app):
    from playwright.sync_api import expect

    page, port = app
    _localhost_only(page, port)
    # Observe a real worker reply for $computeDiff, which distinguishes a
    # healthy generic worker from Monaco's synchronous fallback.
    page.evaluate("""() => {
        const post = Worker.prototype.postMessage;
        Worker.prototype.postMessage = function(message, ...args) {
            if (message?.method === '$computeDiff') {
                const onReply = ({data}) => {
                    if (data?.seq !== message.req) return;
                    this.removeEventListener('message', onReply);
                    if (data.res && !data.err) window.testEditorWorkerDiff = true;
                };
                this.addEventListener('message', onReply);
            }
            return post.call(this, message, ...args);
        };
    }""")
    workers = []
    page.on("worker", lambda worker: workers.append(worker.url))
    _send_first_message(page, "hello diff")
    session = _wait_session(port, lambda x: True)
    path = "sample.ts"
    _make_file(port, session["id"], path, "const initial: number = 100;")

    _open_code_tab(page)
    page.get_by_role("treeitem", name=path, exact=False).first.click()
    editor = page.locator(".monaco-editor")
    _expect_token(editor, "const")
    _expect_token(editor, "number")
    editor.locator(".view-lines").click()
    page.keyboard.press("ControlOrMeta+a")
    page.keyboard.insert_text("const buffer: number = 200;")
    expect(editor.locator(".view-lines")).to_contain_text("buffer")
    _write_file(port, session["id"], path, "const disk: number = 300;")
    with page.expect_response(
        lambda response: response.url.endswith("/api/code/write")
        and response.request.method == "PUT"
    ) as saved:
        page.keyboard.press("ControlOrMeta+s")
    assert saved.value.status == 409

    page.get_by_role("button", name="查看差异", exact=True).click()
    diff = page.locator(".monaco-diff-editor")
    mine = diff.locator(".editor.original")
    theirs = diff.locator(".editor.modified")
    _expect_token(mine, "number")
    _expect_token(theirs, "number")
    expect(mine.locator(".view-lines")).to_contain_text("buffer")
    expect(theirs.locator(".view-lines")).to_contain_text("disk")
    page.wait_for_function("window.testEditorWorkerDiff === true", timeout=10_000)
    assert workers and all(urlsplit(url).netloc == f"127.0.0.1:{port}" for url in workers)
