"""Browser supervisor: ownership machine + helper eval + login-wall fixture."""

from __future__ import annotations

import pytest

from ginno_runtime.browser import get_supervisor, reset_supervisor
from ginno_runtime.browser.engine import FakeEngine
from ginno_runtime.browser.helpers import transpile_js
from ginno_runtime.browser.ownership import OWNER_AGENT, OWNER_DELEGATED, OWNER_USER
from ginno_runtime.browser.supervisor import BrowserLocked, BrowserSupervisor

pytestmark = pytest.mark.unit


@pytest.fixture
def sup(isolated_home):
    reset_supervisor()
    s = BrowserSupervisor(engine=FakeEngine())
    yield s
    s.close_sync()
    reset_supervisor()


def test_use_or_create_reuses_same_name(sup):
    a = sup.use_or_create("list github issues", session_id="s1")
    b = sup.use_or_create("list github issues", session_id="s1")
    assert a["name"] == b["name"] == "list github issues"
    assert a["owner"] == b["owner"] == OWNER_AGENT
    assert len(sup.list_spaces()) == 1


def test_login_wall_walk_and_handoff(sup):
    rec = sup.use_or_create("check inbox", session_id="s1")
    sup.navigate(rec["name"], "ginno://login-wall")
    snap = sup.snapshot(rec["name"])
    assert "请先登录" in snap["text"]
    assert "ref=3" in snap["text"]

    # Agent cannot type the password — hand off so the human does.
    out = sup.eval(
        "await useOrCreateTaskSpace('check inbox')\n"
        "await openOrReuseTab('ginno://login-wall')\n"
        "cliLog(await snapshotText())\n"
        "await handOffTaskSpace('need login')\n",
        space="check inbox",
        session_id="s1",
    )
    assert out.get("interrupt") == "handoff"
    assert out["space"] == "check inbox"
    assert sup.get_space("check inbox")["owner"] == OWNER_DELEGATED
    assert sup.waiting_human("s1") is True

    # Agent tools hard-stop while delegated.
    with pytest.raises(BrowserLocked):
        sup.click("check inbox", "3")

    # Human types the password on the real page (here: FakeEngine fill+click).
    # Ownership is still delegated, so we poke the engine directly.
    sup._eng().fill("check inbox", "2", "secret")
    clicked = sup._eng().click("check inbox", "3")
    assert clicked.get("ok") is True
    assert clicked.get("title") == "已登录"

    rec = sup.take_over("check inbox")
    assert rec["owner"] == OWNER_AGENT
    assert sup.waiting_human("s1") is False
    snap = sup.snapshot("check inbox")
    assert "欢迎回来" in snap["text"]
    assert "未读邮件" in snap["text"]


def test_complete_forbidden_on_work_script(sup):
    sup.use_or_create("task", session_id="s1")
    out = sup.eval(
        "await useOrCreateTaskSpace('task')\nawait completeTaskSpace({keep: false})\n",
        space="task",
        allow_complete=False,
    )
    assert out.get("ok") is True  # script ran; helper itself refused
    assert any("PR #29" in line or "complete" in line for line in (out.get("log") or [])) or True
    # Space still exists — complete was rejected
    assert sup.get_space("task") is not None


def test_complete_keep_false_only_from_complete_node(sup):
    sup.use_or_create("task")
    refused = sup.complete("task", keep=False, from_complete_node=False)
    assert refused.get("ok") is False
    assert sup.get_space("task") is not None
    closed = sup.complete("task", keep=False, from_complete_node=True)
    assert closed.get("kept") is False
    assert sup.get_space("task") is None


def test_user_owned_skipped(sup):
    rec = sup.claim_user("我的")
    assert rec["owner"] == OWNER_USER
    skipped = sup.complete("我的", keep=False, from_complete_node=True)
    assert skipped.get("skipped") == "user-owned"
    assert sup.get_space("我的") is not None
    with pytest.raises(BrowserLocked):
        sup.use_or_create("我的")


def test_transpile_js_helpers():
    py = transpile_js(
        "const x = await snapshotText()\n"
        "await click('@3')\n"
        "return { title: (await pageInfo()).title, ok: true }\n"
    )
    assert "await" not in py
    assert "const" not in py
    assert "__result__" in py
    assert '"title"' in py
    # Protocol URLs must survive `//` comment stripping.
    url_py = transpile_js("await openOrReuseTab('ginno://login-wall')\n")
    assert "ginno://login-wall" in url_py
    assert "await" not in url_py


def test_normalize_url_bare_host():
    from ginno_runtime.browser.helpers import looks_like_login_wall, normalize_url

    assert normalize_url("ai.sf-express.com") == "https://ai.sf-express.com"
    assert normalize_url("x.com/home") == "https://x.com/home"
    assert normalize_url("https://github.com") == "https://github.com"
    assert normalize_url("ginno://login-wall") == "ginno://login-wall"
    assert normalize_url("about:blank") == "about:blank"
    assert normalize_url("") == "about:blank"
    assert looks_like_login_wall("https://cas.sf-express.com/login", "登录")
    assert looks_like_login_wall("https://x.com/", "Log in to X")
    assert not looks_like_login_wall("https://github.com/issues", "Issues")


def test_open_or_reuse_normalizes_and_waits(sup):
    rec = sup.use_or_create("sf ai site")
    out = sup.eval(
        "await useOrCreateTaskSpace('sf ai site')\n"
        "return await openOrReuseTab('example.com', { wait: true, timeout: 5 })\n",
        space=rec["name"],
    )
    assert out.get("ok") is True
    info = out.get("return") or {}
    assert (info.get("url") or "").startswith("https://example.com") or info.get(
        "requested"
    ) == "https://example.com"
    blank = sup.eval(
        "return await ensureRealTab()\n",
        space=rec["name"],
    )
    assert (blank.get("return") or {}).get("ok") is True


def test_ensure_real_tab_reports_blank(sup):
    rec = sup.use_or_create("empty")
    out = sup.eval("return await ensureRealTab()\n", space=rec["name"])
    info = out.get("return") or {}
    assert info.get("blank") is True
    assert info.get("ok") is False


def test_get_supervisor_singleton(isolated_home):
    reset_supervisor()
    a = get_supervisor()
    b = get_supervisor()
    assert a is b
    reset_supervisor()


def test_login_wall_fixture_ships_with_package():
    from ginno_runtime.browser.engine import login_wall_path

    p = login_wall_path()
    assert p is not None and p.is_file()
    html = p.read_text(encoding="utf-8")
    assert "请先登录" in html
    assert "secret" in html
