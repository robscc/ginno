"""Five UX contracts on same-origin static export + isolated source/packaged runtime.

复用 ``test_packaged_ui_tasks_overview`` 的 source/strict harness
（GINNO_E2E_RUNTIME=source 时 spawn ``uv run ginno-runtime`` + repo 内
apps/web/out 静态导出；GINNO_E2E_STRICT=1 时环境缺失即 fail），随机空闲端口
+ 临时 GINNO_HOME，绝不碰 8787 与真实设置。成功流走真实服务端（断言打在
服务端终态），故障流用 page.route mock。

覆盖五项（2026-11-21 UX 批次）：
① 环境变量设置：首拉失败（断连/HTTP 503）→ 行锁定不 PUT；重试成功后
   脏编辑可保存，PUT body 与服务端快照逐键相等、仅 env 被替换；
② 工作流删除：自建走确认弹窗（标题带名称 + 正文说明）、内置行无删除
   按钮、删除被拒时行内反馈且服务端定义仍在；dry-run 校验节点顺序
   （顺序反转 → 服务端可达性检查抓到悬空节点）；
③ 步骤编辑器：不写 JSON 建 2 步工作流 → 服务端 DSL 节点顺序/entry/edges
   正确；坏 JSON（nodes 含 null）提示定位到步骤编号、Create 禁用、
   json→steps 切换被拒且不丢名称/描述草稿；
④ 任务总览三档分组：只有排期任务时右栏徽标显示「Scheduled 1」而非
   运行中/进行中、tooltip 带三档 breakdown；parked ask_user 等待项分到
   「等你处理」组；
⑤ 键盘可达性：任务标题是真 button（可 Tab 聚焦、DOM 序在停止之前、
   Enter 打开任务页）；确认弹窗默认焦点在取消、Tab 循环不出弹窗、
   Esc 关闭后焦点回触发器。
"""

import json
import re

import pytest
import test_packaged_ui_ask_user as ask
import test_packaged_ui_tasks_overview as smoke

pytestmark = pytest.mark.e2e


@pytest.fixture
def app(tmp_path, monkeypatch):
    smoke._require_runtime()
    from playwright.sync_api import sync_playwright

    scripts = tmp_path / 'scripts.json'
    ask._ask_scripts(scripts)
    monkeypatch.setenv('GINNO_FAKE_LLM_SCRIPTS', str(scripts))
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        with sync_playwright() as pw:
            browser, page = smoke._new_page(pw)
            try:
                yield page, port, f'http://127.0.0.1:{port}'
            finally:
                browser.close()
    finally:
        smoke._stop(proc, log)


@pytest.mark.parametrize('failure', ['disconnect', 'http503', 'http503_json_body'])
def test_env_read_failure_retry_dirty_save(app, failure):
    """① 首拉失败 → 不 PUT；重试 → 脏编辑保存且 PUT body 只改 env。

    三种失败形态：断连 / 503 非 JSON body / 503 JSON body（FastAPI 风格
    {"detail": ...}）。设置读写走 jsonStrict（core.ts）：非 2xx 一律抛错，
    JSON 错误体绝不会被当成设置文档——否则读改写窗口内会把错误体整份
    PUT 回 settings.json。PUT 断言取「保存前服务端快照 + 仅替换 env」做
    全文档逐键比较，钉死「不覆写其他设置」。
    """
    from playwright.sync_api import expect

    page, port, base = app
    settings = smoke._api(port, 'GET', '/api/settings')
    settings['notifications'] = {'kinds_schema': 2}
    settings['env'] = {'UX_SEED': 'original'}
    smoke._api(port, 'PUT', '/api/settings', settings)
    puts = []
    broken = True

    def intercept(route):
        if route.request.method == 'GET' and broken:
            if failure == 'disconnect':
                route.abort()
            else:
                body = '{"detail": "boom"}' if failure == 'http503_json_body' else 'settings unavailable'
                route.fulfill(status=503, body=body)
            return
        if route.request.method == 'PUT':
            puts.append(route.request.post_data_json)
        route.continue_()

    page.goto(base + '/settings/security/hooks')
    env_tab = page.get_by_role('button', name='Env Vars', exact=True)
    expect(env_tab).to_be_visible()
    page.route('**/api/settings', intercept)
    env_tab.click()
    save = page.get_by_test_id('env-save')
    expect(page.get_by_test_id('env-retry')).to_be_visible()
    expect(save).to_be_disabled()
    expect(page.get_by_test_id('env-add')).to_be_disabled()
    assert puts == []
    broken = False
    page.get_by_test_id('env-retry').click()
    value = page.get_by_test_id('env-value-0')
    expect(value).to_have_value('original')
    expect(save).to_be_disabled()
    value.fill('edited')
    before = smoke._api(port, 'GET', '/api/settings')
    save.click()
    expect(page.get_by_test_id('env-status')).to_have_text('Saved')
    expect(save).to_be_disabled()
    assert len(puts) == 1, '失败的首次加载绝不能触发 PUT；成功保存恰一次'
    expected = dict(before)
    expected['env'] = {'UX_SEED': 'edited'}
    assert puts[0] == expected, 'PUT body 必须与保存前快照逐键相等、仅 env 被替换'
    assert smoke._api(port, 'GET', '/api/settings')['env'] == {'UX_SEED': 'edited'}


def test_workflow_create_and_delete(app):
    """② 步骤编辑器建 2 步工作流 + 自建删除走确认弹窗 + 服务端节点顺序校验。"""
    from playwright.sync_api import expect

    page, port, base = app
    page.goto(base + '/settings/agents/workflows')
    builtin = page.get_by_text('Built-in', exact=True).first
    expect(builtin).to_be_visible()
    expect(builtin.locator('..').get_by_role('button', name='delete', exact=True)).to_have_count(0)
    page.get_by_placeholder('name (overrides').fill('Ordered UX recipe')
    page.get_by_role('button', name='+ Add step').click()
    for i, title in enumerate(['Collect', 'Summarize']):
        page.get_by_placeholder('Step name').nth(i).fill(title)
        page.get_by_placeholder('Execution requirement').nth(i).fill(title + ' data')
    with page.expect_response(lambda r: r.url == base + '/api/workflows' and r.request.method == 'POST') as created:
        page.get_by_role('button', name='Create', exact=True).click()
    assert created.value.ok and created.value.json()['ok']
    wid = created.value.json()['workflow']['id']
    path = f'/api/workflows/{wid}'
    wf = smoke._api(port, 'GET', path)['workflow']
    dsl = wf['dsl']
    first, second = dsl['nodes']
    assert [first['goal'], second['goal']] == ['Collect data', 'Summarize data']
    assert dsl['entry'] == first['id'] and dsl['edges'] == [{'from': first['id'], 'to': second['id']}]
    assert smoke._api(port, 'POST', '/api/workflows/dry-run', {'dsl': dsl})['ok']
    # 服务端顺序校验：把边反过来（s2→s1）→ dry-run 可达性检查必须抓到 s2 悬空
    rev = {'entry': dsl['entry'], 'nodes': dsl['nodes'],
           'edges': [{'from': second['id'], 'to': first['id']}]}
    dr = smoke._api(port, 'POST', '/api/workflows/dry-run', {'dsl': rev})
    assert second['id'] in dr['unreachable'], dr
    delete = page.get_by_text('Ordered UX recipe', exact=True).locator('..').get_by_role('button', name='delete')
    deletes = []
    page.on('request', lambda r: deletes.append(r.url) if r.method == 'DELETE' else None)
    delete.click()
    dialog = page.get_by_role('dialog', name=re.compile('Delete workflow'))
    # 确认弹窗带名称（标题）+ 说明（正文：版本/DSL 永久删除、历史 run 保留）
    expect(dialog.get_by_text('Ordered UX recipe')).to_be_visible()
    expect(dialog.get_by_text('Past runs are kept.', exact=False)).to_be_visible()
    dialog.get_by_role('button', name='Cancel').click()
    expect(dialog).to_have_count(0)
    assert deletes == []
    delete.click()
    dialog.get_by_role('button', name='Delete').click()
    expect(delete).to_have_count(0)
    assert deletes == [base + path]
    assert wid not in {w['id'] for w in smoke._api(port, 'GET', '/api/workflows')}


def test_workflow_delete_rejection(app):
    """② 删除被服务端拒绝：行内反馈 + 定义仍在。

    已知缺口（如实上报，不在本测试强行断言为理想行为）：失败后确认弹窗
    不自动关闭——行内错误先被半透明遮罩盖住，且点 Cancel 会把 delError
    一并清掉。此处钉住「错误文本存在于行内 + 服务端真源未删 + 删除按钮
    退出 Deleting… 态可重试」这一现实契约。
    """
    from playwright.sync_api import expect

    page, port, base = app
    wf = smoke._api(port, 'POST', '/api/workflows', {'name': 'Locked UX recipe', 'steps': [{'id': 'a', 'title': 'Collect'}]})['workflow']
    path = '/api/workflows/' + wf['id']
    page.goto(base + '/settings/agents/workflows')
    delete = page.get_by_text('Locked UX recipe', exact=True).locator('..').get_by_role('button', name='delete')
    dialog = page.get_by_role('dialog', name=re.compile('Delete workflow'))
    delete.click()
    page.route(base + path, lambda r: r.fulfill(json={'ok': False, 'error': 'locked by policy'}), times=1)
    dialog.get_by_role('button', name='Delete').click()
    expect(page.get_by_text('Delete failed: locked by policy')).to_be_visible()
    expect(delete).to_have_text('delete', timeout=10_000)  # Deleting… 已退出，可重试
    assert smoke._api(port, 'GET', path)['ok']


def test_workflow_draft_roundtrip_and_bad_node(app):
    """③ 步骤/JSON 双模式往返不丢草稿；坏节点提示定位到步骤且切换被拒。"""
    from playwright.sync_api import expect

    page, _, base = app
    page.goto(base + '/settings/agents/workflows')
    name_input = page.get_by_placeholder('name (overrides')
    desc_input = page.get_by_placeholder('description', exact=True)
    goal = page.get_by_placeholder('Execution requirement')
    name_input.fill('Draft keep')
    desc_input.fill('Draft description')
    goal.fill('keep this draft')
    advanced = page.get_by_role('button', name='Advanced JSON')
    steps = page.get_by_role('button', name='Step editor')
    advanced.click()
    editor = page.locator('textarea[spellcheck=false]')
    draft = json.loads(editor.input_value())
    assert draft['name'] == 'Draft keep' and draft['description'] == 'Draft description'
    steps.click()
    expect(goal).to_have_value('keep this draft')
    expect(name_input).to_have_value('Draft keep')
    expect(desc_input).to_have_value('Draft description')
    advanced.click()
    draft = json.loads(editor.input_value())
    draft['nodes'].append(None)
    bad = json.dumps(draft)
    editor.fill(bad)
    expect(page.get_by_text('Step 2 must be a node with a non-empty id.')).to_be_visible()
    expect(page.get_by_role('button', name='Create', exact=True)).to_be_disabled()
    steps.click()
    expect(editor).to_have_value(bad)
    expect(page.get_by_text('The node of step 2 cannot be represented in the step editor.')).to_be_visible()
    # 切换被拒不降级：名称/描述草稿仍在，等用户修好 JSON
    expect(name_input).to_have_value('Draft keep')
    expect(desc_input).to_have_value('Draft description')


def test_task_phases_badge_and_keyboard(app):
    """④ 三档分组徽标 + parked 等待项归组；⑤ 标题按钮键盘可达 + 弹窗焦点契约。"""
    from playwright.sync_api import expect

    page, port, base = app
    smoke._api(port, 'POST', '/api/schedule/tasks', {
        'name': 'Future UX task', 'target': {'type': 'prompt', 'prompt': 'hi'},
        'schedule': {'kind': 'interval', 'minutes': 60},
    })
    assert smoke._api(port, 'GET', '/api/tasks/counts')['counts']['phases']['scheduled'] == 1
    page.goto(base)
    smoke._open_running_tab(page)
    tab = page.get_by_role('tab', name=re.compile('^Running'))
    expect(tab).to_have_attribute('title', re.compile('0 running · 0 waiting for you · 1 scheduled'), timeout=15_000)
    expect(tab.get_by_text('Scheduled 1')).to_be_visible()
    expect(page.get_by_test_id('tasks-overview-phase-running')).to_have_count(0)
    ask._send(page, 'Ask me which installation target to use')
    ask._card(page).wait_for(timeout=30_000)
    smoke._open_running_tab(page)
    page.get_by_test_id('tasks-overview-panel').get_by_role('button', name='Refresh').click()
    waiting = page.get_by_test_id('tasks-overview-phase-waiting').locator('..')
    expect(waiting.get_by_test_id('tasks-overview-item-turn')).to_be_visible(timeout=15_000)
    expect(tab.get_by_text('Waiting for you 1')).to_be_visible(timeout=15_000)
    scheduled = page.get_by_test_id('tasks-overview-phase-scheduled').locator('..')
    row = scheduled.get_by_test_id('tasks-overview-item-schedule')
    title = row.get_by_role('button', name=re.compile('Future UX task'))
    stop = row.get_by_test_id('tasks-overview-stop-schedule')
    title.focus()
    page.keyboard.press('Tab')
    expect(stop).to_be_focused()
    page.keyboard.press('Enter')
    dialog = page.get_by_role('dialog', name='Pause this schedule?')
    cancel = dialog.get_by_role('button', name='Cancel')
    confirm = dialog.get_by_role('button', name='Pause')
    expect(cancel).to_be_focused()
    page.keyboard.press('Shift+Tab')
    expect(confirm).to_be_focused()
    page.keyboard.press('Tab')
    expect(cancel).to_be_focused()
    page.keyboard.press('Escape')
    expect(dialog).to_have_count(0)
    expect(stop).to_be_focused()
    page.keyboard.press('Shift+Tab')
    expect(title).to_be_focused()
    page.keyboard.press('Enter')
    expect(page).to_have_url(re.compile(r'/scheduled/?$'))
