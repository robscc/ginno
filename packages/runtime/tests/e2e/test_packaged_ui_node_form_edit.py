"""Packaged-UI e2e：方案 D2——python 节点全程结构化表单配置，不碰 JSON。

契约（goal：建 python 节点全程不碰 JSON）：
* entry 渲染为下拉（数据来自 GET /api/workflows/python-entries 白名单——
  断言真实注册表项出现在 options 里，端点通路被浏览器级验证）；
* args 是 kv-map 行编辑器（key/value input + context 引用插入下拉），
  args 字段区域内 **没有任何 textarea**（结构性证明「不碰 JSON」）；
* Save → 真实 dry-run 通过 → Apply → v2 落库；
* 服务端终态 DSL 的 entry/args 与表单输入一致（draft↔DSL 序列化往返的
  浏览器级闭环）。

复用 tasks_overview 的 source/strict harness（GINNO_E2E_RUNTIME=source 时
spawn 源码 sidecar + repo 内 apps/web/out；随机空闲端口 + 临时 GINNO_HOME，
不碰 8787 与真实设置）。全程走真实服务端，无 route mock。
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


def _python_workflow(port: int) -> str:
    """一个 python 节点骨架（entry 已在白名单内）+ 下游 step。

    context.schema 声明两个 key（vault_path / report_dir）——kv-map 的
    「插入 context 引用」下拉的数据源就是它们。"""
    dsl = {
        "entry": "p1",
        "nodes": [
            {"id": "p1", "type": "python", "entry": "kb_wiki_lint"},
            {"id": "s1", "type": "step", "agent": "research", "goal": "summarize the lint report"},
        ],
        "edges": [{"from": "p1", "to": "s1"}],
        "context": {
            "schema": {
                "type": "object",
                "properties": {
                    "vault_path": {"type": "string"},
                    "report_dir": {"type": "string"},
                },
            },
            "initial": {},
        },
    }
    wf = smoke._api(port, "POST", "/api/workflows", {"name": "d2 python form", "dsl": dsl})["workflow"]
    return wf["id"]


def test_unrepresentable_args_lock_form_mode_and_never_drop(app):
    """无法表达的 DSL（嵌套 args）锁结构化模式并提示切 JSON；编辑其它字段
    保存后嵌套 args 原样落库——绝不静默丢字段。"""
    import re

    from playwright.sync_api import expect

    page, port, base = app
    dsl = {
        "entry": "p1",
        "nodes": [
            {
                "id": "p1",
                "type": "python",
                "entry": "kb_wiki_lint",
                # 嵌套 args：行编辑器无法无损表达 → 锁定
                "args": {"model_name": "{{vault_path}}", "opts": {"deep": [1, 2]}},
            },
            {"id": "s1", "type": "step", "agent": "research", "goal": "report"},
        ],
        "edges": [{"from": "p1", "to": "s1"}],
    }
    wid = smoke._api(port, "POST", "/api/workflows", {"name": "d2 locked", "dsl": dsl})["workflow"]["id"]
    page.goto(f"{base}/workflows#wf={wid}&node=p1&tab=design")
    expect(page.get_by_text("p1", exact=True).first).to_be_visible(timeout=20_000)

    # ① lockedHint 出现（黄框），args 区域没有行编辑 input（被锁）。
    expect(page.get_by_text(re.compile(r"cannot express losslessly"), exact=False)).to_be_visible()
    args_block = page.locator('label:text-is("args")').locator("xpath=../..")
    expect(args_block.locator("input[aria-label='key']")).to_have_count(0)
    # 原始嵌套 JSON 以只读 pre 展示（内容可见，不藏不丢）。
    expect(args_block.locator("pre")).to_contain_text('"deep"')

    # ② 编辑未锁字段（title）→ 保存 → Apply；嵌套 args 必须原样落库。
    title = page.locator('label:text-is("title") + input')
    title.fill("renamed while locked")
    save = page.get_by_role("button", name="Save as new version")
    expect(save).to_be_enabled()
    save.click()
    apply = page.get_by_role("button", name=re.compile(r"Apply.*v2"))
    expect(apply).to_be_visible(timeout=15_000)
    apply.click()
    expect(page.get_by_role("button", name="Save as new version")).to_be_visible(timeout=15_000)

    wf = smoke._api(port, "GET", f"/api/workflows/{wid}")["workflow"]
    p1 = next(n for n in wf["dsl"]["nodes"] if n["id"] == "p1")
    assert p1["title"] == "renamed while locked"
    assert p1["args"] == {"model_name": "{{vault_path}}", "opts": {"deep": [1, 2]}}, p1["args"]


def test_python_node_configured_entirely_in_form(app):
    import re

    from playwright.sync_api import expect

    page, port, base = app
    wid = _python_workflow(port)
    page.goto(f"{base}/workflows#wf={wid}&node=p1&tab=design")

    # Inspector 挂载（节点标题出现）。
    expect(page.get_by_text("p1", exact=True).first).to_be_visible(timeout=20_000)

    # ① entry 是下拉，options 含真实注册表项（python-entries 端点通路）。
    entry_select = page.locator('label:text-is("entry") + select')
    expect(entry_select).to_be_visible()
    options = entry_select.locator("option").all_inner_texts()
    for registered in ("kb_wiki_lint", "normalize_and_compare", "fetch_aliyun_bills"):
        assert registered in options, f"entry dropdown missing registered entry {registered!r}: {options}"
    entry_select.select_option("normalize_and_compare")

    # ② args 是 kv-map 行编辑器：字段区域内没有 textarea（不碰 JSON 的结构性证明）。
    #    structured 字段的 label 包在 div.mb-1.flex 里（toggle 按钮同行），
    #    行编辑器在更外层——从 label 上移两级才是字段容器。
    args_block = page.locator('label:text-is("args")').locator("xpath=../..")
    expect(args_block.locator("textarea")).to_have_count(0)
    args_block.get_by_role("button", name="Add row").click()

    key_input = args_block.locator('input[aria-label="key"]')
    value_input = args_block.locator('input[aria-label^="value"]')
    key_input.fill("model_name")

    # ③ 「插入 context 引用」下拉列出 DSL context.schema 的 keys，选择后
    #    value 变成 {{vault_path}} 模板。
    insert_sel = args_block.locator('select[aria-label="Insert context ref"]')
    ctx_options = insert_sel.locator("option").all_inner_texts()
    assert "vault_path" in ctx_options and "report_dir" in ctx_options, ctx_options
    insert_sel.select_option("vault_path")
    expect(value_input).to_have_value("{{vault_path}}")

    # ④ 保存 → 真实 dry-run（entry 白名单合法、args 对象合法 → 必过）→ Apply v2。
    save = page.get_by_role("button", name="Save as new version")
    expect(save).to_be_enabled()
    save.click()
    apply = page.get_by_role("button", name=re.compile(r"Apply.*v2"))
    expect(apply).to_be_visible(timeout=15_000)
    apply.click()
    # 应用成功后确认卡收起（回到非编辑态）。
    expect(page.get_by_role("button", name="Save as new version")).to_be_visible(timeout=15_000)

    # ⑤ 服务端终态：表单输入原样落 DSL（draft↔DSL 序列化往返闭环）。
    wf = smoke._api(port, "GET", f"/api/workflows/{wid}")["workflow"]
    p1 = next(n for n in wf["dsl"]["nodes"] if n["id"] == "p1")
    assert p1["entry"] == "normalize_and_compare", p1
    assert p1["args"] == {"model_name": "{{vault_path}}"}, p1
    assert wf["version"] == 2, wf["version"]
