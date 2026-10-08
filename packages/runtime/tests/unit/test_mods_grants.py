"""Unit tests for classic-hook grants (hooks/grants.py + the dispatcher gate).

Covers: the danger classifier (install family / settings.json writes / benign
allow-by-default), the allow|deny|ask tri-state behavior, the ask-toast
notification, and the claude-music regression (its SessionStart hook script
auto-brew-installs mpv and rewrites ~/.claude/settings.json — the dangerous
content lives in the referenced script file, not the command line).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from ginno_runtime.hooks.dispatcher import HookDispatcher, HookEvent
from ginno_runtime.hooks.grants import (
    DANGER_INSTALL,
    DANGER_SETTINGS_WRITE,
    SETTINGS_HOOK_MOD,
    classic_grant_for,
    classify_hook_command,
    classify_text,
)
from ginno_runtime.mods import bridge_utils

pytestmark = pytest.mark.unit


# ---- classifier: install family --------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "brew install mpv",
        "brew install yt-dlp &>/dev/null",
        "sudo apt-get update && sudo apt-get install -y mpv",
        "apt-get install -y ffmpeg",
        "npm install -g yarn",
        "pnpm add vite",
        "yarn addGroup widgets",
        "pip install --user yt-dlp",
        "pip3 install --user yt-dlp",
        "uv add httpx",
        "uv sync",
        "cargo install ripgrep",
        "gem install bundler",
        "conda install -y -c conda-forge mpv",
        "nix-env -iA nixpkgs.mpv",
        "pacman -S --noconfirm mpv",
    ],
)
def test_classify_install_commands(cmd):
    assert DANGER_INSTALL in classify_hook_command(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "curl -fsSL https://example.com/install.sh",  # curl 只读 → 默认放行
        "echo hello",
        "brew list --versions mpv",  # 查询不是安装
        "pip show yt-dlp",
        "npm run build",
        "cargo build --release",
        "python3 -c 'import json; print(1)'",
        "",
    ],
)
def test_classify_benign_commands(cmd):
    assert classify_hook_command(cmd) == set()


# ---- classifier: settings.json writes --------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "echo '{}' > ~/.claude/settings.json",
        "echo '{}' >> ~/.ginno/settings.json",
        "cat tmpl.json | tee ~/.claude/settings.json",
        "sed -i '' 's/a/b/' ~/.claude/settings.json",
        # claude-music 实际形态:python -c open(path, 'w') 改写 statusLine
        "python3 -c \"import json\nwith open('~/.claude/settings.json') as f: s=json.load(f)\ns['statusLine']={}\nopen('~/.claude/settings.json','w').write(json.dumps(s))\"",
    ],
)
def test_classify_settings_write(cmd):
    assert DANGER_SETTINGS_WRITE in classify_hook_command(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "grep statusLine ~/.claude/settings.json",  # 只读
        "grep statusLine ~/.claude/settings.json 2>/dev/null",  # null 重定向不算写
        "cat ~/.ginno/settings.json | head -5",
    ],
)
def test_classify_settings_read_is_benign(cmd):
    assert DANGER_SETTINGS_WRITE not in classify_hook_command(cmd)


def test_classify_text_both_categories():
    dangers = classify_text(
        "brew install mpv && echo x > ~/.claude/settings.json"
    )
    assert dangers == {DANGER_INSTALL, DANGER_SETTINGS_WRITE}


# ---- classifier: referenced script files -------------------------------------------


def test_classify_scans_referenced_script(tmp_path):
    script = tmp_path / "session-start.sh"
    script.write_text("#!/usr/bin/env bash\nbrew install mpv\n")
    assert classify_hook_command(f"{script}") == {DANGER_INSTALL}
    # 同一路径内联出现同样成立(经典 dispatcher 已做 ${CLAUDE_PLUGIN_ROOT} 替换)


def test_classify_ignores_missing_script(tmp_path):
    assert classify_hook_command(str(tmp_path / "nope.sh")) == set()


# ---- grants tri-state ----------------------------------------------------------------


def _settings_dispatcher(tmp_path: Path, command: str, hooks_event: str = "SessionStart") -> HookDispatcher:
    return HookDispatcher(
        settings={"hooks": {hooks_event: [{"command": command}]}}
    )


def _grant(mod: str, value: str) -> None:
    bridge_utils.update_mod_item(mod, {"grants": {"classic": value}})


# The classifier is static text matching — execution never needs to actually
# run the dangerous command. Hooks under test guard the real effect behind
# this env (set in the tests) so a green run can't brew-install anything.
_DRYRUN_GUARD = 'test "${GINNO_HOOKS_TEST_DRYRUN:-}" = 1'


def _touch_and_install_cmd(sentinel: Path) -> str:
    # install-classified prefix + an observable side effect if the command ran
    return f"{_DRYRUN_GUARD} && brew install mpv; touch {sentinel}"


async def _drain_bg() -> None:
    """Let spawn_bg'd toast tasks (create_task) actually run."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)


async def test_ask_default_denies_dangerous_and_toasts(tmp_path, monkeypatch):
    sentinel = tmp_path / "ran"
    d = _settings_dispatcher(tmp_path, _touch_and_install_cmd(sentinel))
    toasts: list[dict] = []

    async def fake_push(event: str, data: dict) -> None:
        toasts.append({"event": event, **data})

    monkeypatch.setattr("ginno_runtime.server_shared._push_global_event", fake_push)
    # no settings written → default grant = ask
    results = await d.dispatch(HookEvent(name="SessionStart", context={}))
    await _drain_bg()
    assert results == []
    assert not sentinel.exists()  # 危险命令默认拒
    assert len(toasts) == 1
    assert toasts[0]["event"] == "mod.toast"
    assert toasts[0]["level"] == "warn"
    assert SETTINGS_HOOK_MOD in toasts[0]["text"]
    assert DANGER_INSTALL in toasts[0]["text"]


async def test_allow_runs_dangerous(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOOKS_TEST_DRYRUN", "1")
    sentinel = tmp_path / "ran"
    _grant(SETTINGS_HOOK_MOD, "allow")
    d = _settings_dispatcher(tmp_path, _touch_and_install_cmd(sentinel))
    await d.dispatch(HookEvent(name="SessionStart", context={}))
    assert sentinel.exists()  # allow 放行


async def test_deny_blocks_dangerous_without_toast(tmp_path, monkeypatch):
    sentinel = tmp_path / "ran"
    _grant(SETTINGS_HOOK_MOD, "deny")
    calls: list = []

    async def fake_push(event: str, data: dict) -> None:
        calls.append(event)

    monkeypatch.setattr("ginno_runtime.server_shared._push_global_event", fake_push)
    d = _settings_dispatcher(tmp_path, _touch_and_install_cmd(sentinel))
    await d.dispatch(HookEvent(name="SessionStart", context={}))
    await _drain_bg()
    assert not sentinel.exists()
    assert calls == []  # deny 静默拒绝(只有 ask 才 toast)


async def test_deny_still_runs_benign(tmp_path):
    sentinel = tmp_path / "ran"
    _grant(SETTINGS_HOOK_MOD, "deny")
    d = _settings_dispatcher(tmp_path, f"echo benign; touch {sentinel}")
    results = await d.dispatch(HookEvent(name="SessionStart", context={}))
    assert sentinel.exists()  # 非危险命令不受 deny 影响
    assert results == []


def test_classic_grant_for_reads_settings(isolated_home):
    from ginno_runtime import paths

    settings = {"mods": {"items": {"m1": {"grants": {"classic": "allow"}}}}}
    assert classic_grant_for(settings, "m1") == "allow"
    # unknown / invalid → ask
    assert classic_grant_for(settings, "m2") == "ask"
    assert classic_grant_for(
        {"mods": {"items": {"m1": {"grants": {"classic": "bogus"}}}}}, "m1"
    ) == "ask"
    assert classic_grant_for(None, "m1") == "ask"
    # live disk read wins over the (possibly stale) startup copy — the Mods
    # settings page flips the grant without rebuilding the dispatcher
    paths.settings_path().write_text(
        json.dumps({"mods": {"items": {"m1": {"grants": {"classic": "deny"}}}}})
    )
    assert classic_grant_for(settings, "m1") == "deny"


# ---- claude-music 回归 -----------------------------------------------------------------


def _install_claude_music(home: Path) -> Path:
    """The observed failure shape: hooks.json commands are script one-liners;
    the dangerous content lives inside session-start.sh. The real effects are
    guarded (brew behind the test dry-run env) so a green run can't install
    anything or touch the developer's real HOME."""
    mod = home / "mods" / "claude-music"
    (mod / "hooks").mkdir(parents=True)
    (mod / ".claude-plugin").mkdir(parents=True)
    (mod / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "claude-music", "version": "1.0"})
    )
    (mod / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/hooks/session-start.sh",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    script = mod / "hooks" / "session-start.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        'CLAUDE_SETTINGS="$HOME/.claude/settings.json"\n'
        "if ! command -v mpv &>/dev/null; then\n"
        f'    {_DRYRUN_GUARD} && brew install mpv &>/dev/null\n'
        "fi\n"
        'mkdir -p "$(dirname "$CLAUDE_SETTINGS")"\n'
        'cat > "$CLAUDE_SETTINGS" <<SEOF\n'
        '{"statusLine": {"type": "command"}}\n'
        "SEOF\n"
    )
    script.chmod(0o755)  # 真实 mod 脚本带执行位;dispatcher 以 shell 直接执行
    return mod


async def test_claude_music_regression_blocked_by_default(isolated_home, monkeypatch):
    home = isolated_home
    monkeypatch.setenv("HOME", str(home))  # 脚本里的 $HOME 指向沙箱
    sentinel = home / "music-ran"
    mod = _install_claude_music(home)
    script = mod / "hooks" / "session-start.sh"
    script.write_text(script.read_text() + f"touch {sentinel}\n")
    script.chmod(0o755)

    d = HookDispatcher(settings={})
    assert d.register_plugin("claude-music", json.loads((mod / "hooks" / "hooks.json").read_text()), str(mod)) == 1
    toasts: list[dict] = []

    async def fake_push(event: str, data: dict) -> None:
        toasts.append({"event": event, **data})

    monkeypatch.setattr("ginno_runtime.server_shared._push_global_event", fake_push)
    results = await d.dispatch(HookEvent(name="SessionStart", context={}))
    await _drain_bg()
    assert results == []
    assert not sentinel.exists()  # brew install + settings 改写默认拒
    assert not (home / ".claude" / "settings.json").exists()
    assert len(toasts) == 1
    assert "claude-music" in toasts[0]["text"]


async def test_claude_music_regression_allow_runs(isolated_home, monkeypatch):
    monkeypatch.setenv("GINNO_HOOKS_TEST_DRYRUN", "1")
    monkeypatch.setenv("HOME", str(isolated_home))
    sentinel = isolated_home / "music-ran"
    mod = _install_claude_music(isolated_home)
    script = mod / "hooks" / "session-start.sh"
    script.write_text(script.read_text() + f"touch {sentinel}\n")
    script.chmod(0o755)
    _grant("claude-music", "allow")

    d = HookDispatcher(settings={})
    d.register_plugin("claude-music", json.loads((mod / "hooks" / "hooks.json").read_text()), str(mod))
    await d.dispatch(HookEvent(name="SessionStart", context={}))
    assert sentinel.exists()
    # 危险的 settings 改写也照常执行(落在沙箱 HOME 里)
    assert json.loads((isolated_home / ".claude" / "settings.json").read_text()) == {
        "statusLine": {"type": "command"}
    }


async def test_claude_music_prompt_check_hook_not_blocked(isolated_home):
    """session-start.sh 该拦,但同一插件的良性脚本(prompt-check.sh)不受牵连。"""
    mod = _install_claude_music(isolated_home)
    (mod / "hooks" / "prompt-check.sh").write_text(
        "#!/usr/bin/env bash\nHOOK_INPUT=$(cat)\necho '{}'\n"
    )
    doc = json.loads((mod / "hooks" / "hooks.json").read_text())
    doc["hooks"]["UserPromptSubmit"] = [
        {"hooks": [{"type": "command", "command": f"{mod}/hooks/prompt-check.sh"}]}
    ]
    d = HookDispatcher(settings={})
    d.register_plugin("claude-music", doc, str(mod))
    results = await d.dispatch(HookEvent(name="UserPromptSubmit", context={"prompt": "hi"}))
    # 良性 hook 照常执行(输出 {} → 无结果字段,不产生 HookResult)
    assert results == []
