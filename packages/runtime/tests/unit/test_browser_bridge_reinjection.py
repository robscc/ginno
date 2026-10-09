"""B 轨 bridge 注入自愈（2026-10-08 turn 4b3a219e 回归）。

事故:36kr 是 SPA,客户端路由(pushState)不触发 Page 导航事件,而
``Page.addScriptToEvaluateOnNewDocument`` 只在新 document 创建时注入——
于是新上下文里 __ginnoBridge/__ginnoAT 都不存在,表达式
``globalThis.__ginnoBridge && __ginnoBridge.pageText(n)`` 短路成 undefined,
browser_page_text 报出无信息量的"正文抽取不可用"。

模型当时选的是**正确**工具(每篇文章 1 次调用),失败后退回手写
navigate+browser_js 两步走,17 篇文章把回合撞进步数上限。

修复:_eval_bridge 在拿到 undefined(bridge 丢失)时强制重注入一次并重试。
"""

from __future__ import annotations

import pytest

from ginno_runtime.browser.executor import ProfileBackend

pytestmark = pytest.mark.unit


class FakeTab:
    """Returns ``None`` (bridge missing) until inject_scripts() is called."""

    def __init__(self, result_after_inject):
        self._result_after_inject = result_after_inject
        self.injected = 0
        self.evals = 0

    async def eval_js(self, _expr):
        self.evals += 1
        # bridge present only after an injection
        return self._result_after_inject if self.injected else None

    async def inject_scripts(self, _sources):
        self.injected += 1


@pytest.fixture
def backend():
    b = ProfileBackend.__new__(ProfileBackend)  # skip __init__ (profile_dir I/O)
    b.tabs = {1: None}
    return b


def _wire(backend, tab):
    async def _tab(tab_id):
        return tab
    backend._tab = _tab
    return backend


@pytest.mark.asyncio
async def test_eval_bridge_reinjects_once_when_bridge_lost(backend):
    tab = FakeTab({"title": "T", "url": "u", "content": "body"})
    _wire(backend, tab)

    out = await backend.page_text(1)
    assert out["content"] == "body"
    assert tab.injected == 1, "bridge loss must trigger exactly one re-injection"
    assert tab.evals == 2, "one failed attempt + one retry"


@pytest.mark.asyncio
async def test_eval_bridge_does_not_reinject_when_bridge_present(backend):
    """A healthy tab must not pay the injection cost — the common path."""
    tab = FakeTab({"title": "T", "url": "u", "content": "body"})
    tab.injected = 1  # already present
    _wire(backend, tab)

    out = await backend.page_text(1)
    assert out["content"] == "body"
    assert tab.injected == 1, "no re-injection when the bridge answered"
    assert tab.evals == 1


@pytest.mark.asyncio
async def test_page_text_returns_actionable_error_when_bridge_never_returns(backend):
    """Still broken after the retry: the error must TELL THE MODEL what to do,
    not just 'bridge unavailable' — the old message left it guessing and it
    fell back to the expensive 2-calls-per-page path."""
    class DeadTab(FakeTab):
        async def eval_js(self, _expr):
            return None  # never recovers
        async def inject_scripts(self, _sources):
            self.injected += 1

    tab = DeadTab(None)
    _wire(backend, tab)
    out = await backend.page_text(1)
    assert "error" in out
    assert "browser_navigate" in out["error"], "must name the recovery action"
    assert tab.injected == 1, "exactly one retry, then give up (no loop)"


@pytest.mark.asyncio
async def test_legitimate_falsy_result_is_not_mistaken_for_lost_bridge(backend):
    """find() legitimately returns {results: []} — an empty dict is a real
    answer, not a missing bridge, so it must NOT trigger re-injection."""
    tab = FakeTab({"results": []})
    tab.injected = 1
    _wire(backend, tab)

    out = await backend.find_elements(1, "nothing-matches")
    assert out == {"results": []}
    assert tab.injected == 1
    assert tab.evals == 1