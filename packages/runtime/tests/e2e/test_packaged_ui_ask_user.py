"""Playwright e2e: the ask_user card in a REAL browser against the PACKAGED sidecar.

Same harness as test_packaged_ui_playwright.py (real PyInstaller binary on a free
port, temp home, GINNO_FAKE_LLM, real Chromium), but walks the ambiguity flow
end to end: the agent parks the turn on an ask_user interrupt, the card renders
inside the assistant bubble, the user clicks an option, the receipt folds in —
and the card SURVIVES A PAGE RELOAD, which is the property that separates it
from the permission prompt (a client-side ref that is lost on reload).

The second test is the one that pins the reload gap fixed during development:
`turn_state` must be probed unconditionally on socket open, otherwise a card
rebuilt from history after a reload renders disabled even though the backend is
still parked and would accept the answer.

Skips gracefully when the binary isn't built, playwright isn't installed, or the
port is taken.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

_RUNTIME_DIST = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime"
RUNTIME_BIN = _RUNTIME_DIST / "ginno-runtime" if _RUNTIME_DIST.is_dir() else _RUNTIME_DIST
# The sibling packaged-UI file owns 8899/8898; these are ours. Each test gets
# its OWN port: they share a harness, and a slow teardown of the first sidecar
# would otherwise leave the second skipping on "port already in use" — which is
# exactly the test that pins the reload fix, so it must never skip silently.
PORTS = {0: 8897, 1: 8896}

OPTION_A = "Ginno 全局 ~/.ginno/skills"
OPTION_B = "本仓库 .claude/skills"
HEADER = "选择安装位置"


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


def _launch_chromium(pw):
    try:
        return pw.chromium.launch()
    except Exception:
        import sys

        subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"], check=False
        )
        return pw.chromium.launch()


def _wait_health(port: int, timeout: float = 60) -> None:
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.5)
    raise RuntimeError(f"sidecar on :{port} did not become healthy")


def _settings() -> dict:
    return {
        "default_provider": "custom",
        "bypass_permissions": True,
        "providers": {
            "anthropic": {
                "enabled": False, "protocol": "anthropic", "api_key": "",
                "default_model": "x", "base_url": "", "max_tokens": 1,
                "temperature": 0.7, "timeout_s": 5,
            },
            "openai": {
                "enabled": False, "protocol": "openai", "api_key": "",
                "default_model": "x", "base_url": "", "org_id": "", "max_tokens": 1,
            },
            "custom": {
                "enabled": True, "protocol": "openai-compatible", "name": "t",
                "api_key": "k", "base_url": "http://127.0.0.1:1", "model": "m",
                "max_tokens": 100, "temperature": 0.7, "timeout_s": 5,
            },
        },
        "permissions": {"allow": [], "deny": [], "ask": []},
        "hooks": {},
        "knowledge": {"enabled": False},
    }


def _ask_scripts(path: Path) -> None:
    """One scripted turn: park on ask_user, then finish once the answer lands."""
    path.write_text(
        json.dumps(
            [
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "name": "ask_user",
                            "args": {
                                "question": "「导入 skill」装到哪个目录？",
                                "options": [OPTION_A, OPTION_B],
                                "header": HEADER,
                            },
                        }
                    ],
                },
                {"content": "好，按你选的位置装好了。"},
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def app(tmp_path, request):
    """Boot the packaged sidecar + Chromium; yields (page, home).

    Parametrize indirectly to give each test a private port:
    ``@pytest.mark.parametrize("app", [8897], indirect=True)``.
    """
    port = request.param
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip(
            "playwright not installed (uv sync --group test && playwright install chromium)"
        )
    if _port_open(port):
        pytest.skip(f"port {port} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    scripts = tmp_path / "scripts.json"
    _ask_scripts(scripts)

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(port),
    )
    proc = subprocess.Popen(
        [str(RUNTIME_BIN)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_timeout(1500)
            yield page, home
            browser.close()
    finally:
        proc.terminate()


def _send(page, text: str) -> None:
    """Type into the welcome composer and send (sessions are created lazily)."""
    ta = page.locator("textarea").first
    ta.click()
    ta.fill(text)
    ta.press("Enter")


def _card(page):
    """The question card, located by its header."""
    return page.locator(f"text={HEADER}").first


def _transcript_scroll(page) -> dict:
    """Where the transcript sits, found via the bubble's scrollable ancestor.

    Walking up from a known bubble is the only reliable way to pick the message
    list out of the several scrollable panels on the page.
    """
    return page.evaluate(
        """(needle) => {
          const node = Array.from(document.querySelectorAll('div'))
            .find(d => d.children.length === 0 && d.textContent.trim() === needle);
          if (!node) return {noBubble: true};
          let el = node.parentElement, sc = null;
          while (el) {
            const s = getComputedStyle(el);
            if ((s.overflowY === 'auto' || s.overflowY === 'scroll')
                && el.scrollHeight > el.clientHeight + 10) { sc = el; break; }
            el = el.parentElement;
          }
          if (!sc) return {noScroller: true};
          return {scrollTop: Math.round(sc.scrollTop),
                  maxScroll: sc.scrollHeight - sc.clientHeight,
                  atBottom: sc.scrollHeight - sc.scrollTop - sc.clientHeight <= 2};
        }""",
        "导入 ponytail skill",
    )


@pytest.mark.parametrize("app", [PORTS[0]], indirect=True)
def test_card_renders_answer_folds_and_survives_reload(app):
    page, _home = app

    _send(page, "导入 ponytail skill")
    _card(page).wait_for(timeout=20_000)
    assert page.locator(f"text={OPTION_A}").count() >= 1
    assert page.locator(f"text={OPTION_B}").count() >= 1
    # the 跳过 escape hatch is always offered alongside the options
    assert page.locator("text=跳过").count() >= 1

    page.locator(f"text={OPTION_B}").first.click()

    # the receipt folds into the transcript once the turn resumes
    page.locator(f"text=已选择：{OPTION_B}").first.wait_for(timeout=20_000)
    page.locator("text=好，按你选的位置装好了。").first.wait_for(timeout=20_000)

    # THE property the permission prompt does not have: a reload rebuilds the
    # card from the checkpoint (question from the tool_call, answer from the
    # tool result) instead of losing it.
    page.reload(wait_until="load")
    page.wait_for_timeout(2000)
    assert page.locator(f"text=已选择：{OPTION_B}").count() >= 1, (
        "the answered card must come back from history after a reload"
    )
    # …and it is a receipt, not a live question again
    assert page.locator(f"text={OPTION_A}").count() == 0

    # Scroll landing is deterministic: whenever the transcript overflows it must
    # sit at the BOTTOM. It used to depend on whether the pin frame beat the
    # transcript's progressive layout — the same session landed at scrollTop 0
    # in a tall viewport and mid-transcript in a short one, which is how a first
    # message ends up invisible with no scrollbar hint that anything is above.
    page.set_viewport_size({"width": 1100, "height": 560})
    page.wait_for_timeout(1200)
    scroll = _transcript_scroll(page)
    if not scroll.get("noScroller"):
        assert scroll["atBottom"], f"transcript must land at the bottom: {scroll}"


@pytest.mark.parametrize("app", [PORTS[1]], indirect=True)
def test_pending_card_survives_reload_and_is_still_answerable(app):
    """Regression for the reload gap: after a reload the client has no memory of
    the turn, so the (unconditional) turn_state probe is the only thing that
    keeps the rebuilt pending card interactive. Without it the card renders
    disabled while the backend is still parked and would accept the answer."""
    page, _home = app

    _send(page, "导入 ponytail skill")
    _card(page).wait_for(timeout=20_000)

    page.reload(wait_until="load")
    page.wait_for_timeout(2500)

    # still pending (blank receipt), options present
    _card(page).wait_for(timeout=20_000)
    assert page.locator("text=已选择").count() == 0, "the question is unanswered"
    assert page.locator(f"text={OPTION_A}").count() >= 1

    # …and ANSWERABLE: clicking resumes the parked turn.
    page.locator(f"text={OPTION_A}").first.click()
    page.locator(f"text=已选择：{OPTION_A}").first.wait_for(timeout=20_000)
    page.locator("text=好，按你选的位置装好了。").first.wait_for(timeout=20_000)

# --------------------------------------------------------------------------- #
# inline numbered choices (no `options` arg) → quick-reply buttons
# --------------------------------------------------------------------------- #
INLINE_Q = (
    "你要把 skill 装到哪个位置？请直接回复 1 / 2 / 3：\n\n"
    "1. 当前仓库 .claude/skills → ~/workspace/dev/demo/.claude/skills/\n"
    "2. Ginno 全局 → ~/.ginno/skills/\n"
    "3. 两个都装\n"
)


def _inline_scripts(path: Path) -> None:
    """A question with NO options — the choices are numbered in the body.

    This is the shape the model fell back to on 2026-09-20 after its array
    argument was rejected twice; the card must still offer clickable choices
    instead of making the user type a digit."""
    path.write_text(
        json.dumps(
            [
                {
                    "content": "",
                    "tool_calls": [
                        {"name": "ask_user", "args": {"question": INLINE_Q, "header": HEADER}}
                    ],
                },
                {"content": "好，装到全局了。"},
            ]
        ),
        encoding="utf-8",
    )


@pytest.fixture
def app_inline(tmp_path, request):
    """Same harness, an inline-choices script, its own port."""
    if not RUNTIME_BIN.exists():
        pytest.skip("packaged sidecar not built (run `make runtime`)")
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        pytest.skip("playwright not installed")
    port = request.param
    if _port_open(port):
        pytest.skip(f"port {port} already in use")

    home = tmp_path / "home"
    home.mkdir()
    (home / "settings.json").write_text(json.dumps(_settings()))
    scripts = tmp_path / "scripts.json"
    _inline_scripts(scripts)

    env = dict(
        os.environ,
        GINNO_HOME=str(home),
        GINNO_FAKE_LLM="1",
        GINNO_FAKE_LLM_SCRIPTS=str(scripts),
        GINNO_RUNTIME_PORT=str(port),
    )
    proc = subprocess.Popen(
        [str(RUNTIME_BIN)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    try:
        _wait_health(port)
        with sync_playwright() as pw:
            browser = _launch_chromium(pw)
            page = browser.new_page()
            page.goto(f"http://127.0.0.1:{port}/", wait_until="load")
            page.wait_for_timeout(1500)
            yield page, home
            browser.close()
    finally:
        proc.terminate()


@pytest.mark.parametrize("app_inline", [8895], indirect=True)
def test_inline_numbered_choices_become_buttons(app_inline):
    page, _home = app_inline

    _send(page, "导入 skill")
    _card(page).wait_for(timeout=20_000)

    # Each numbered line is a BUTTON (getByRole excludes the body text, which
    # renders the same lines as markdown).
    b1 = page.get_by_role("button", name="当前仓库 .claude/skills")
    b2 = page.get_by_role("button", name="Ginno 全局")
    b3 = page.get_by_role("button", name="两个都装")
    b1.wait_for(timeout=10_000)
    assert b2.count() == 1 and b3.count() == 1

    # One click replies for the user — no typing.
    b2.click()
    page.locator("text=已选择：").first.wait_for(timeout=20_000)
    page.locator("text=好，装到全局了。").first.wait_for(timeout=20_000)

    # …and the reply is the line text, so the model gets the full choice.
    page.locator("text=Ginno 全局 →").first.wait_for(timeout=10_000)


@pytest.mark.parametrize("app_inline", [8894], indirect=True)
def test_inline_choices_survive_reload(app_inline):
    page, _home = app_inline
    _send(page, "导入 skill")
    _card(page).wait_for(timeout=20_000)
    page.reload(wait_until="load")
    page.wait_for_timeout(2500)
    # still pending, still clickable after the reload
    b = page.get_by_role("button", name="两个都装")
    b.wait_for(timeout=20_000)
    b.click()
    page.locator("text=已选择：").first.wait_for(timeout=20_000)
