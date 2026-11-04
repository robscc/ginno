"""rtk 零分发集成单测（tools/rtk_bridge.py）。

覆盖：有 rtk 时白名单重写 / 无 rtk 原样 / 复合命令不重写 / 开关关闭不重写 /
检测缓存与弱校验 / bash 工具端到端接线。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from ginno_runtime.tools import rtk_bridge
from ginno_runtime.tools.builtin import build_builtin_tools
from ginno_runtime.tools.rtk_bridge import detect_rtk, reset_rtk_cache, rewrite

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch, tmp_path):
    """每测试重置检测缓存，并把 settings 指到临时目录。"""
    reset_rtk_cache()
    monkeypatch.setattr(rtk_bridge, "settings_path", lambda: tmp_path / "settings.json")
    yield
    reset_rtk_cache()


def _install_rtk(monkeypatch, version_output="rtk 0.42.1\n"):
    """模拟已装 rtk：shutil.which 命中 + --version 输出符合弱校验。"""
    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: "/opt/homebrew/bin/rtk" if name == "rtk" else None)
    monkeypatch.setattr(
        rtk_bridge.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess(a[0] if a else [], 0, stdout=version_output, stderr=""),
    )


@pytest.fixture
def rtk_installed(monkeypatch):
    _install_rtk(monkeypatch)


# ---- rewrite：白名单命中 ----


@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "git status --porcelain",
        "git diff",
        "git log --oneline -5",
        "pytest -q",
        "npm test",
        "pnpm test",
        "docker ps",
        "ls",
        "ls -la",
        "tree",
        "cargo build",
        "cargo test -q",
        "go test ./...",
        "kubectl get pods",
        "kubectl pods",
        "gh pr list",
        "gh pr status",
        "grep -rn TODO src",
    ],
)
def test_rewrite_whitelisted_commands(rtk_installed, cmd):
    assert rewrite(cmd) == f"rtk {cmd}"


@pytest.mark.parametrize(
    "cmd",
    [
        "git push",  # 白名单外 git 子命令
        "git commit -m x",
        "terraform plan",  # rtk 0.42.1 无 terraform 子命令（实测）
        "rg TODO",  # rtk 对 rg 是透传原生执行，无压缩收益（实测）
        "kubectl logs x",  # 白名单外 kubectl 子命令
        "gh issue list",  # 白名单外 gh 子命令（保守）
        "pytest::not-this",  # noqa — 见下条真实用例
    ],
)
def test_rewrite_non_whitelisted(rtk_installed, cmd):
    assert rewrite(cmd) is None


def test_rewrite_compound_commands_never_rewritten(rtk_installed):
    for cmd in [
        "git status | head -5",
        "git diff > /tmp/x.patch",
        "pytest && echo done",
        "ls; rm -rf /",
        "git status && git diff",
        "cat log.txt | grep err",
    ]:
        assert rewrite(cmd) is None, cmd


def test_rewrite_empty_and_bad_quoting(rtk_installed):
    assert rewrite("") is None
    assert rewrite("   ") is None
    assert rewrite("git status 'unclosed") is None  # shlex 失败 → 不动


# ---- rewrite：无 rtk / 版本不符 ----


def test_no_rtk_returns_none(monkeypatch):
    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: None)
    assert rewrite("git status") is None


def test_wrong_rtk_binary_version_not_trusted(monkeypatch):
    # crates.io 同名无关 crate：--version 输出不像 rtk → 视为未安装
    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: "/usr/local/bin/rtk")
    monkeypatch.setattr(
        rtk_bridge.subprocess,
        "run",
        lambda *a, **kw: subprocess.CompletedProcess([], 0, stdout="cargo-rtk 1.0\n", stderr=""),
    )
    assert detect_rtk() is None
    assert rewrite("git status") is None


def test_rtk_version_call_raises_falls_back(monkeypatch):
    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: "/usr/bin/rtk")

    def boom(*a, **kw):
        raise OSError("noexec")

    monkeypatch.setattr(rtk_bridge.subprocess, "run", boom)
    assert detect_rtk() is None
    assert rewrite("git status") is None


# ---- 检测缓存 ----


def test_detect_cached_once(monkeypatch):
    calls = {"n": 0}

    def fake_run(*a, **kw):
        calls["n"] += 1
        return subprocess.CompletedProcess([], 0, stdout="rtk 0.42.1\n", stderr="")

    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: "/usr/bin/rtk")
    monkeypatch.setattr(rtk_bridge.subprocess, "run", fake_run)
    assert detect_rtk() == "/usr/bin/rtk"
    assert detect_rtk() == "/usr/bin/rtk"
    assert calls["n"] == 1  # 第二次走缓存


# ---- 开关 ----


def _write_settings(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def test_disabled_via_settings(rtk_installed, monkeypatch, tmp_path):
    sp = tmp_path / "settings.json"
    _write_settings(sp, {"rtk": {"enabled": False}})
    monkeypatch.setattr(rtk_bridge, "settings_path", lambda: sp)
    assert rewrite("git status") is None


def test_enabled_explicit_true(rtk_installed, monkeypatch, tmp_path):
    sp = tmp_path / "settings.json"
    _write_settings(sp, {"rtk": {"enabled": True}})
    monkeypatch.setattr(rtk_bridge, "settings_path", lambda: sp)
    assert rewrite("git status") == "rtk git status"


def test_broken_settings_file_defaults_enabled(rtk_installed, monkeypatch, tmp_path):
    sp = tmp_path / "settings.json"
    sp.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(rtk_bridge, "settings_path", lambda: sp)
    assert rewrite("git status") == "rtk git status"


# ---- bash 工具端到端 ----


@pytest.fixture
def ws(tmp_path):
    d = tmp_path / "ws"
    d.mkdir()
    return str(d)


def test_bash_tool_executes_rewritten_command(monkeypatch, ws, tmp_path):
    """白名单命令在 bash 工具里被改写为 rtk 执行（端到端，mock 子进程）。"""
    _install_rtk(monkeypatch)
    sp = tmp_path / "settings.json"
    monkeypatch.setattr(rtk_bridge, "settings_path", lambda: sp)

    captured = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            captured["cwd"] = kw.get("cwd")
            self.stdout = None
            self.stderr = None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("ginno_runtime.tools.builtin.subprocess.Popen", FakeProc)
    monkeypatch.setattr("ginno_runtime.tools.builtin.snapshot_images", lambda p: {})
    read_file, write_file, glob_files, grep_files, edit_file, bash = build_builtin_tools(Path(ws))
    bash.invoke({"command": "git status", "timeout": 5})
    shell_cmd = captured["argv"][2]
    assert shell_cmd == "rtk git status"


def test_bash_tool_unaffected_without_rtk(monkeypatch, ws, tmp_path):
    """无 rtk：bash 工具行为与现状完全一致。"""
    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: None)
    captured = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self.stdout = None
            self.stderr = None

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("ginno_runtime.tools.builtin.subprocess.Popen", FakeProc)
    monkeypatch.setattr("ginno_runtime.tools.builtin.snapshot_images", lambda p: {})
    read_file, write_file, glob_files, grep_files, edit_file, bash = build_builtin_tools(Path(ws))
    bash.invoke({"command": "git status", "timeout": 5})
    assert captured["argv"][2] == "git status"
