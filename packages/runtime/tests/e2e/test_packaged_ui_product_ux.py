"""Five UX contracts on same-origin static export + isolated source/packaged runtime.

复用 ``test_packaged_ui_tasks_overview`` 的 source/strict harness
（GINNO_E2E_RUNTIME=source 时 spawn ``uv run ginno-runtime`` + repo 内
apps/web/out 静态导出；GINNO_E2E_STRICT=1 时环境缺失即 fail），随机空闲端口
+ 临时 GINNO_HOME，绝不碰 8787 与真实设置。成功流走真实服务端（断言打在
服务端终态），故障流用 page.route mock。

覆盖五项（2026-11-21 UX 批次；① 于 settings-integrity 批次改为 env 每键
CAS 契约）：
① 环境变量设置：首拉失败（断连/HTTP 503）→ 行锁定不 PATCH；重试成功后
   脏编辑可保存，PATCH /api/settings/env 的 body 是每键 CAS 载荷
   （key+version+value），服务端终态仅 env 变化、其它设置原样；
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
    """① 首拉失败 → 不 PATCH；重试 → 脏编辑保存且 PATCH 是每键 CAS 载荷。

    settings-integrity 新契约：env 视图/保存走专用端点 GET/PATCH
    /api/settings/env（每键 version token CAS），不再整文档 PUT
    /api/settings——「不覆写其他设置」由 PATCH 语义天然保证（updates 只含
    变更键），断言打在 PATCH body 与服务端终态（env 只改 UX_SEED、
    notifications 等其它设置原样保留）。
    三种失败形态不变：断连 / 503 非 JSON body / 503 JSON body。GET env
    非 2xx → 行锁定，绝不发 PATCH（错误体绝不会被当成视图重放）。
    """
    from playwright.sync_api import expect

    page, port, base = app
    settings = smoke._api(port, 'GET', '/api/settings')
    settings['notifications'] = {'kinds_schema': 2}
    settings['env'] = {'UX_SEED': 'original'}
    smoke._api(port, 'PUT', '/api/settings', settings)
    patches = []
    broken = True

    def intercept(route):
        if route.request.method == 'GET' and broken:
            if failure == 'disconnect':
                route.abort()
            else:
                body = '{"detail": "boom"}' if failure == 'http503_json_body' else 'env unavailable'
                route.fulfill(status=503, body=body)
            return
        if route.request.method == 'PATCH':
            patches.append(route.request.post_data_json)
        route.continue_()

    page.goto(base + '/settings/security/hooks')
    env_tab = page.get_by_role('button', name='Env Vars', exact=True)
    expect(env_tab).to_be_visible()
    page.route('**/api/settings/env', intercept)
    env_tab.click()
    save = page.get_by_test_id('env-save')
    expect(page.get_by_test_id('env-retry')).to_be_visible()
    expect(save).to_be_disabled()
    expect(page.get_by_test_id('env-add')).to_be_disabled()
    assert patches == []
    broken = False
    page.get_by_test_id('env-retry').click()
    value = page.get_by_test_id('env-value-0')
    expect(value).to_have_value('original')
    expect(save).to_be_disabled()
    value.fill('edited')
    # 重试成功后页面 GET env 拿到的 version token（无写入发生，测试侧同值）
    token = smoke._api(port, 'GET', '/api/settings/env')['entries']['UX_SEED']['version']
    assert isinstance(token, str) and token
    save.click()
    expect(page.get_by_test_id('env-status')).to_have_text('Saved')
    expect(save).to_be_disabled()
    assert len(patches) == 1, '失败的首次加载绝不能触发 PATCH；成功保存恰一次'
    assert patches[0] == {'updates': [
        {'key': 'UX_SEED', 'version': token, 'value': 'edited'},
    ]}, 'PATCH 必须是每键 CAS 载荷（key+version+value）'
    after = smoke._api(port, 'GET', '/api/settings')
    assert after['env'] == {'UX_SEED': 'edited'}
    assert after['notifications'] == {'kinds_schema': 2}, 'PATCH 只动 env，其它设置原样'


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


def test_moved_artifact_requires_explicit_relink(app, tmp_path):
    """Real hover, preview and cancel leave both registrations untouched."""
    from urllib.parse import urlsplit

    from playwright.sync_api import expect

    from ginno_runtime import artifacts

    page, port, base = app
    workspace = tmp_path / 'workspace'
    workspace.mkdir()
    sid = smoke._api(port, 'POST', '/api/sessions', {
        'project_slug': 'default', 'title': 'Moved artifact fixture', 'workspace': str(workspace),
    })['id']
    vault = tmp_path / 'vault'
    smoke._api(port, 'PUT', '/api/kb/wiki/config', {'vault_path': str(vault), 'enabled': False})
    name = 'Acceptance report.md'
    source = workspace / name
    original = b'# Original report body\n'
    source.write_bytes(original)
    artifact = artifacts.add_artifact('default', 'file', name, str(source), sid)
    file_id = smoke._api(port, 'POST', '/api/files/ensure-registered', {
        'session_id': sid, 'path': str(source), 'name': name,
    })['file']['id']
    target = vault / 'Z original' / name
    unrelated = vault / 'A unrelated' / name
    target.parent.mkdir(parents=True)
    unrelated.parent.mkdir()
    unrelated.write_bytes(b'# Different report body\n')
    source.rename(target)
    artifact_url = f"/api/artifacts/{artifact['id']}"
    preview_url = f'/api/files/{file_id}/preview'
    download_url = f'/api/files/{file_id}/download'
    writes = []

    def on_request(req):
        if urlsplit(req.url).path == artifact_url and req.method == 'PUT':
            writes.append(req.post_data_json)

    def unchanged(stage):
        stored = smoke._api(port, 'GET', artifact_url)
        assert stored['ref'] == str(source), f'{stage}: artifact rebound to {stored["ref"]}'
        listing = smoke._api(port, 'GET', '/api/files')
        assert next(f for f in listing if f['id'] == file_id)['path'] == str(source.resolve())
        assert writes == []

    page.on('request', on_request)
    page.goto(base)
    page.wait_for_function('typeof window.__ginnoOpenSession === "function"')
    page.evaluate('sid => window.__ginnoOpenSession(sid)', sid)
    page.get_by_role('tab', name=re.compile('^Artifacts')).click()
    name_label = page.get_by_text(name, exact=True).first
    with page.expect_response(lambda res: urlsplit(res.url).path == artifact_url + '/metadata') as hover:
        name_label.hover()
    unchanged('hover')
    assert hover.value.json()['file']['id'] == file_id
    assert hover.value.json()['exists'] is False

    with page.expect_response(lambda res: urlsplit(res.url).path == preview_url) as preview:
        name_label.click()
    assert preview.value.status == 404
    unchanged('preview')
    page.keyboard.press('Escape')

    row = page.locator(f'[data-artifact-row="{artifact["id"]}"]')
    row.get_by_role('button', name='Artifact details', exact=True).click()
    card = page.get_by_role('dialog', name=f'Artifact metadata: {name}')
    card.get_by_role('button', name='Relink file', exact=True).click()
    exact = card.get_by_role('radio', name=str(target.resolve()), exact=True)
    wrong = card.get_by_role('radio', name=str(unrelated.resolve()), exact=True)
    expect(exact).to_be_visible()
    expect(wrong).to_be_visible()
    expect(card.get_by_role('button', name='Confirm relink')).to_be_disabled()
    wrong.check()
    card.get_by_role('button', name='Cancel', exact=True).click()
    unchanged('cancel')

    card.get_by_role('button', name='Relink file', exact=True).click()
    exact.check()
    with page.expect_response(lambda res: urlsplit(res.url).path == artifact_url and res.request.method == 'PUT') as saved:
        card.get_by_role('button', name='Confirm relink').click()
    assert saved.value.ok and saved.value.json()['artifact']['id'] == artifact['id']
    assert writes == [{'relink': {
        'path': str(target.resolve()), 'expected_ref': str(source), 'expected_file_id': file_id,
    }}]
    expect(row.get_by_text(str(target.resolve()), exact=True)).to_be_visible()
    card.get_by_title('Close', exact=True).click()

    with page.expect_response(lambda res: urlsplit(res.url).path == preview_url) as opened:
        row.get_by_text(name, exact=True).click()
    assert opened.value.status == 200 and opened.value.json()['file']['id'] == file_id
    expect(page.get_by_text('Original report body', exact=True)).to_be_visible()
    page.keyboard.press('Escape')
    row.hover()
    with page.expect_response(lambda res: urlsplit(res.url).path == download_url) as download:
        row.get_by_title('Save to Downloads', exact=True).click()
    assert download.value.status == 200 and download.value.body() == original
    assert len(writes) == 1
    print(json.dumps({'same_name_candidates': 2, 'wrong_relinks': 0,
                      'explicit_writes': len(writes), 'file_id': file_id,
                      'artifact_id': artifact['id'], 'original_bytes': True}))


def test_relinkable_artifacts_survive_runtime_restart(tmp_path):
    from urllib.request import urlopen

    from ginno_runtime import artifacts

    smoke._require_runtime()
    workspace = tmp_path / 'workspace'
    moved = tmp_path / 'moved'
    workspace.mkdir()
    moved.mkdir()
    records = []
    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        sid = smoke._api(port, 'POST', '/api/sessions', {
            'project_slug': 'default', 'title': 'Restart relink fixture', 'workspace': str(workspace),
        })['id']
        for kind, name, registered in [
            ('file', 'registered.md', True), ('image', 'registered.png', True),
            ('file', 'unregistered.md', False),
        ]:
            source = workspace / name
            body = f'original bytes for {name}'.encode()
            source.write_bytes(body)
            artifact = artifacts.add_artifact('default', kind, name, str(source), sid)
            file_id = None
            if registered:
                file_id = smoke._api(port, 'POST', '/api/files/ensure-registered', {
                    'session_id': sid, 'path': str(source), 'name': name,
                })['file']['id']
            destination = source.rename(moved / name)
            stored = smoke._api(port, 'GET', f"/api/artifacts/{artifact['id']}")
            assert stored['ref'] == str(source)
            records.append((artifact['id'], source, destination, file_id, body))
    finally:
        smoke._stop(proc, log)

    port = smoke._free_port()
    proc, log = smoke._spawn(tmp_path, port)
    try:
        smoke._wait_health(port)
        stored = {a['id']: a for a in smoke._api(port, 'GET', f'/api/artifacts?session_id={sid}')}
        assert set(stored) == {record[0] for record in records}
        for artifact_id, source, destination, file_id, body in records:
            assert stored[artifact_id]['ref'] == str(source)
            endpoint = f'/api/artifacts/{artifact_id}'
            meta = smoke._api(port, 'GET', endpoint + '/metadata')
            assert meta['exists'] is False and (meta['file'] or {}).get('id') == file_id
            result = smoke._api(port, 'PUT', endpoint, {'relink': {
                'path': str(destination), 'expected_ref': str(source), 'expected_file_id': file_id,
            }})
            assert result['ok'] and result['artifact']['id'] == artifact_id
            entry = smoke._api(port, 'GET', endpoint + '/metadata')['file']
            assert file_id is None or entry['id'] == file_id
            with urlopen(f"http://127.0.0.1:{port}/api/files/{entry['id']}/download", timeout=5) as response:
                assert response.status == 200 and response.read() == body
        print(json.dumps({'restart_retained': len(stored), 'relinked': len(records),
                          'original_ids': sum(record[3] is not None for record in records), 'original_bytes': True}))
    finally:
        smoke._stop(proc, log)
