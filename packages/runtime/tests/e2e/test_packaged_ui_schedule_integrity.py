"""Schedule-integrity 收口复核的 packaged UI 契约（2026-11-22 批次）。

复用 ``test_packaged_ui_tasks_overview`` 的 source/strict harness（随机空闲
端口 + 临时 GINNO_HOME，绝不碰 8787 与真实设置）。复用
``test_packaged_ui_product_ux`` 的 app fixture（fake-LLM 脚本注入，本文件
两组用例实际不触发 LLM）。

钉住调度复核的四个前端契约（对应 apps/web scheduled/ 的收口修复）：

A. 首载严格 + 有界退避 + stale 可见性（复核缺口①③）：
   GET /api/schedule 503 JSON 错误体 → 绝不被当配置渲染（旧行为会把
   {"detail":...} set 成 cfg 然后崩在渲染层）；显示重试态而非断言挂了；
   手动重试拿到畸形 200（shape 不符）依旧失败；真正合法 cfg 才渲染。
   已成功后再次 GET 失败 → stale 横幅（不再静默降级）。

B. once 生命周期诚实（复核缺口②）：
   last_scheduled.status="caught_up" 是**排队补跑标记**（scheduler.
   _consume_missed 先记账、真正结论由 trigger_task→_finish_run 覆盖），
   无成功证据 → 「Catch-up queued」，绝不绿色 Done；老数据 last_run 无
   trigger（无来源）→ 「Unknown source」，同样不绿。

C. run-now 三分型 + 对账闭环（复核缺口④）：
   POST .../run 断连（回执丢失，结果未知）→ 黄色反馈 + **可点击的**
   「View this task's runs」按钮 → 跳执行记录页签并锁定该任务过滤
   （chip 可见、可清除）。断言真实服务端任务行与 GET /api/schedule/runs
   的过滤参数（对账路径真的落到 API，不是纯前端摆设）。

运行前提与其它 packaged UI e2e 相同：本机 8787 被占用时自动 skip
（CI 干净环境全跑）；本地跑前先退出 Ginno。
"""

import json

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


def _task(tid: str, name: str, **extra) -> dict:
    return {
        'id': tid,
        'name': name,
        'target': {'type': 'prompt', 'prompt': 'hello'},
        'schedule': {'kind': 'once', 'at': '2026-11-20T09:00:00'},
        'enabled': False,
        'created': 1,
        'updated': 1,
        **extra,
    }


def test_schedule_first_load_strict_backoff_stale(app):
    """A：503 JSON 错误体不伪装成配置；畸形 200 同样失败；stale 不静默。"""
    from playwright.sync_api import expect

    page, _port, base = app
    # 三段路由状态：broken(503 JSON) → malformed(200 但 shape 不符) → ok(构造 cfg) → 再 broken
    state = {'mode': 'broken'}
    cfg = {
        'enabled': True,
        'keep_awake': False,
        'unseen': {'total': 0, 'error': 0, 'missed': 0},
        'tasks': [
            # 排队补跑：last_scheduled.status="caught_up"（scheduler.py:553 的
            # _record_missed 记账——排队标记，非成功）
            _task(
                't-catch', 'Catchup task',
                last_scheduled={'run_id': 'r1', 'status': 'caught_up',
                                'started': 1, 'finished': 1, 'trigger': 'catch_up'},
            ),
            # 老数据无 last_scheduled、last_run 无 trigger（无来源、无成功证据）
            _task(
                't-nosrc', 'Legacy no-source',
                last_run={'run_id': 'r2', 'status': 'ok', 'started': 1, 'finished': 2},
            ),
        ],
    }

    def intercept(route):
        if route.request.method != 'GET':
            route.continue_()
            return
        mode = state['mode']
        if mode == 'broken':
            route.fulfill(status=503, body='{"detail": "scheduler store corrupted"}')
        elif mode == 'malformed':
            # 200 + JSON 但 shape 不符（无 tasks 数组）：listSchedule 的
            # shape 守卫必须拒绝（复核缺口①的畸形 200 半边）。
            route.fulfill(status=200, content_type='application/json',
                          body=json.dumps({'detail': 'not a schedule config'}))
        else:
            route.fulfill(status=200, content_type='application/json', body=json.dumps(cfg))

    page.route('**/api/schedule', intercept)
    page.goto(base + '/scheduled')

    # 首拉 503：重试态（en）+ 手动按钮，绝无「配置渲染成功」的迹象
    expect(page.get_by_text('retrying automatically', exact=False).first).to_be_visible(timeout=15_000)
    retry = page.get_by_role('button', name='Retry now')
    expect(retry).to_be_visible()
    # 503 JSON 错误体绝不被当 cfg：任务行不存在
    assert page.locator('[data-task-id]').count() == 0

    # 手动重试 → 畸形 200：仍失败（shape 守卫），任务行依旧不存在
    state['mode'] = 'malformed'
    retry.click()
    expect(page.get_by_text('retrying automatically', exact=False).first).to_be_visible(timeout=15_000)
    assert page.locator('[data-task-id]').count() == 0

    # 再重试 → 合法 cfg：渲染两个任务行
    state['mode'] = 'ok'
    retry.click()
    expect(page.locator('[data-task-id="t-catch"]')).to_be_visible(timeout=15_000)
    expect(page.locator('[data-task-id="t-nosrc"]')).to_be_visible()

    # B：caught_up = 排队补跑（黄），unknown source = 灰；两者都不是 Done
    expect(page.get_by_text('Catch-up queued', exact=True)).to_be_visible()
    expect(page.get_by_text('Unknown source', exact=True)).to_be_visible()
    expect(page.get_by_text('Done', exact=True)).to_have_count(0)
    expect(page.get_by_text('Done (legacy scheduled run)', exact=True)).to_have_count(0)
    # 状态行如实：补跑排队等待调度恢复 / 已暂停·来源未知
    expect(page.get_by_text('queued for catch-up', exact=False).first).to_be_visible()
    expect(page.get_by_text('no source record', exact=False).first).to_be_visible()

    # A 尾声：已成功后再失败 → stale 横幅（不静默降级），旧数据仍在
    state['mode'] = 'broken'
    page.get_by_title('Refresh').click()
    expect(page.get_by_test_id('schedule-stale-banner')).to_be_visible(timeout=15_000)
    expect(page.locator('[data-task-id="t-catch"]')).to_be_visible()


def test_run_now_unknown_opens_task_runs(app):
    """C：run-now 断连 → 结果未知 + 可点击对账入口 → 任务过滤锁定。"""
    from playwright.sync_api import expect

    page, port, base = app
    made = smoke._api(port, 'POST', '/api/schedule/tasks', {
        'name': 'Recon task',
        'target': {'type': 'prompt', 'prompt': 'hello'},
        'schedule': {'kind': 'interval', 'minutes': 60},
        'enabled': True,
    })
    tid = made['id']
    runs_queries = []

    def intercept(route):
        if route.request.method == 'POST':
            route.abort()  # 断连：回执丢失，结果未知
            return
        if '/api/schedule/runs' in route.request.url and '?' in route.request.url:
            runs_queries.append(route.request.url.split('?', 1)[1])
        route.continue_()

    page.route('**/api/schedule/tasks/*/run', intercept)
    page.route('**/api/schedule/runs**', intercept)
    page.goto(base + '/scheduled')

    row = page.locator(f'[data-task-id="{tid}"]')
    expect(row).to_be_visible(timeout=15_000)
    row.get_by_title('Run once now (does not advance the schedule)').click()

    # 黄色「结果未知」反馈 + 对账按钮（不是红色「可安全重试」）
    feedback = page.get_by_test_id(f'task-action-feedback-{tid}')
    expect(feedback).to_be_visible(timeout=15_000)
    expect(page.get_by_text('no receipt', exact=False).first).to_be_visible()
    expect(page.get_by_text('safe to retry', exact=False)).to_have_count(0)

    # 点击「查看此任务运行记录」→ runs 页签 + 该任务锁定 chip
    page.get_by_test_id(f'task-view-runs-{tid}').click()
    chip = page.get_by_test_id('runs-task-filter-chip')
    expect(chip).to_be_visible(timeout=15_000)
    expect(chip).to_contain_text('Recon task')
    # 过滤真的下到 API：task_id 参数出现在 runs 请求里（对账闭环非摆设）。
    # expect.poll 在仓库 playwright 版本不可用——手写 deadline 轮询
    # （同 test_ask_user_flow._wait_question_blocks 的慢机器容错模式）。
    import time

    def _saw(filter_fn, timeout=10.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if filter_fn(runs_queries):
                return True
            page.wait_for_timeout(200)
        return filter_fn(runs_queries)

    assert _saw(lambda qs: any(f'task_id={tid}' in q for q in qs)), \
        f'runs 请求必须带 task_id={tid} 过滤（对账闭环），实际: {runs_queries[-3:]}'

    # chip 可清除：清除后过滤参数不再带 task_id
    chip.get_by_role('button').click()
    expect(page.get_by_test_id('runs-task-filter-chip')).to_have_count(0)
    seen_at = len(runs_queries)
    assert _saw(
        lambda qs: len(qs) > seen_at
        and all(f'task_id={tid}' not in q for q in qs[seen_at:]),
    ), f'清除过滤后新 runs 请求不得再带 task_id={tid}，实际: {runs_queries[seen_at:]}'
    # 清理：删除测试任务（隔离的临时 GINNO_HOME，保险起见仍删）
    smoke._api(port, 'DELETE', f'/api/schedule/tasks/{tid}')
