"""rtk 零分发集成单测（tools/rtk_bridge.py）。

覆盖：有 rtk 时白名单重写 / 无 rtk 原样 / 复合命令不重写 / 开关关闭不重写 /
检测缓存与弱校验 / bash 工具端到端接线。
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path

import pytest

from ginno_runtime.tools import rtk_bridge
from ginno_runtime.tools.builtin import build_builtin_tools
from ginno_runtime.tools.rtk_bridge import detect_rtk, reset_rtk_cache, rewrite

pytestmark = pytest.mark.unit

# rewrite 用绝对路径调用 rtk（裸名在最小 PATH 环境会 exit 127）——
# 单测断言的前缀即 _install_rtk mock 的 which 返回值。
_RTK = "/opt/homebrew/bin/rtk"


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
        # 2026-11-07 rtk 1.1.0 实测加白（见 _RTK_WHITELIST 注释的逐条结论）
        "rg TODO src",
        "rg --pcre2 '(a)\\1' .",
        "find . -name '*.py'",
        "diff a.txt b.txt",
        "diff -u a.txt b.txt",
    ],
)
def test_rewrite_whitelisted_commands(rtk_installed, cmd):
    assert rewrite(cmd) == f"{_RTK} {cmd}"


@pytest.mark.parametrize(
    "cmd",
    [
        "git push",  # 白名单外 git 子命令
        "git commit -m x",
        "terraform plan",  # rtk 无 terraform 子命令（0.42.1/1.1.0 实测）
        "kubectl logs x",  # 白名单外 kubectl 子命令
        "gh issue list",  # 白名单外 gh 子命令（保守）
        # rtk 1.1.0 实测不加白（见模块注释）：err/summary 是吞 stdout 的
        # runner 形态，test/read 与 shell 内建同名会被改写破坏语义，
        # log 是 macOS 系统 log 代理非文件日志过滤器。
        "err cat a.txt",
        "summary make build",
        "test -f a.txt",
        "test -d /tmp && echo y",
        "read -r line",
        "log show --last 1h",
    ],
)
def test_rewrite_non_whitelisted(rtk_installed, cmd):
    assert rewrite(cmd) is None


# ---- B2：git -C <path> <sub> 形态 ----


@pytest.mark.parametrize(
    "cmd",
    [
        "git -C /repo status",
        "git -C /repo diff",
        "git -C /repo log --oneline -3",
        "git -C ../rel/path status --porcelain",
        "git -C /repo diff HEAD~1",
    ],
)
def test_rewrite_git_c_forms(rtk_installed, cmd):
    assert rewrite(cmd) == f"{_RTK} {cmd}"


@pytest.mark.parametrize(
    "cmd",
    [
        "git -C /repo add -A",  # -C 后子命令仅 status/diff/log
        "git -C /repo push",
        "git -c user.email=t@t status",  # -c（小写，配置注入）不是 -C
        "git worktree list",
    ],
)
def test_rewrite_git_c_non_whitelisted(rtk_installed, cmd):
    assert rewrite(cmd) is None


# ---- B3：复合段感知 —— 按顶层 && / ; 拆段，命中段前插 rtk ----


@pytest.mark.parametrize(
    "cmd,expected",
    [
        # 实际会话的主形态：cd 复合命令的第二段现在能命中
        ("cd /repo && git log --oneline", f"cd /repo && {_RTK} git log --oneline"),
        # 多段：命中的都换，不命中的原样
        ("echo hi && git status && echo bye", f"echo hi && {_RTK} git status && echo bye"),
        ("ls; echo done", f"{_RTK} ls; echo done"),
        ("git status && git diff", f"{_RTK} git status && {_RTK} git diff"),
        ("rg TODO . ; rg DONE .", f"{_RTK} rg TODO . ; {_RTK} rg DONE ."),
        # 引号内的 && / ; 不拆段（引号内是参数文本）
        ('echo "a && b" ; ls', f'echo "a && b" ; {_RTK} ls'),
        ("echo 'x|y' && rg foo", f"echo 'x|y' && {_RTK} rg foo"),
        # 重组保留原空白（仅多出 "rtk "）
        (" pytest -q ", f" {_RTK} pytest -q "),
        ("cd  x   &&   git status", f"cd  x   &&   {_RTK} git status"),
        # git -C 复合段
        ("echo pre && git -C /repo log -2", f"echo pre && {_RTK} git -C /repo log -2"),
    ],
)
def test_rewrite_compound_segments(rtk_installed, cmd, expected):
    assert rewrite(cmd) == expected


def test_rewrite_compound_all_miss_returns_none(rtk_installed):
    # 没有任何段命中 → None（一切照旧），不返回原样字符串
    assert rewrite("cd /repo && make build") is None
    assert rewrite("echo a; echo b") is None


def test_rewrite_pipe_and_redirect_segments_never_rewritten(rtk_installed):
    """管道/重定向破坏 rtk 输出语义：所在段整段原样（goal B3 保守要求）。"""
    for cmd in [
        "git status | head -5",
        "git diff > /tmp/x.patch",
        "rg TODO . | wc -l",
        "cat log.txt | grep err",
        "ls > out.txt",
        "find . | xargs grep x",
        # 段级：第二段含管道 → 只有该段不动，第一段仍可命中
        "git status && git diff | cat",
        # 后台 & / 输入 < 同样整段不重写
        "git status &",
        "grep x < in.txt",
    ]:
        if cmd == "git status && git diff | cat":
            assert rewrite(cmd) == f"{_RTK} git status && git diff | cat", cmd
        else:
            assert rewrite(cmd) is None, cmd


def test_rewrite_empty_and_bad_quoting(rtk_installed):
    assert rewrite("") is None
    assert rewrite("   ") is None
    assert rewrite("git status 'unclosed") is None  # 引号不闭合 → 整条不动
    assert rewrite('echo "unclosed && ls') is None
    assert rewrite("echo `date` && ls") is None  # 命令替换 → 整条不动
    assert rewrite("echo a\necho b") is None  # 换行 → 整条不动


# ---- rewrite：无 rtk / 版本不符 ----


def test_no_rtk_returns_none(monkeypatch):
    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: None)
    # Homebrew 前缀兜底也要挡掉：本机 /opt/homebrew/bin/rtk 真实存在，
    # 不 patch os.access 的话此用例只在「没装 rtk 的机器」上成立。
    monkeypatch.setattr(rtk_bridge.os, "access", lambda p, m: False)
    assert rewrite("git status") is None


def test_homebrew_fallback_found_when_which_fails(monkeypatch):
    # GUI 最小 PATH 场景（2026-11-04 实案）：which 失败但 Homebrew 路径存在
    def fake_run(args, **kw):
        assert args[0].endswith("/rtk")
        return subprocess.CompletedProcess(args, 0, stdout="rtk 0.42.1\n", stderr="")

    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: None)
    monkeypatch.setattr(rtk_bridge.os, "access", lambda p, m: p == "/opt/homebrew/bin/rtk")
    monkeypatch.setattr(rtk_bridge.subprocess, "run", fake_run)
    assert rtk_bridge.detect_rtk() == "/opt/homebrew/bin/rtk"


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
    assert rewrite("git status") == f"{_RTK} git status"


def test_broken_settings_file_defaults_enabled(rtk_installed, monkeypatch, tmp_path):
    sp = tmp_path / "settings.json"
    sp.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(rtk_bridge, "settings_path", lambda: sp)
    assert rewrite("git status") == f"{_RTK} git status"


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
            self.stdout = io.StringIO()
            self.stderr = io.StringIO()

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("ginno_runtime.tools.builtin.subprocess.Popen", FakeProc)
    monkeypatch.setattr("ginno_runtime.tools.builtin.snapshot_images", lambda p: {})
    read_file, write_file, glob_files, grep_files, edit_file, bash = build_builtin_tools(Path(ws))
    bash.invoke({"command": "git status", "timeout": 5})
    shell_cmd = captured["argv"][2]
    assert shell_cmd == f"{_RTK} git status"


def test_bash_tool_unaffected_without_rtk(monkeypatch, ws, tmp_path):
    """无 rtk：bash 工具行为与现状完全一致。"""
    monkeypatch.setattr(rtk_bridge.shutil, "which", lambda name: None)
    # 本机 /opt/homebrew/bin/rtk 真实存在：连 Homebrew 兜底一起挡掉，
    # 该用例才与「机器上没装 rtk」等价。
    monkeypatch.setattr(rtk_bridge.os, "access", lambda p, m: False)
    captured = {}

    class FakeProc:
        def __init__(self, argv, **kw):
            captured["argv"] = argv
            self.stdout = io.StringIO()
            self.stderr = io.StringIO()

        def wait(self, timeout=None):
            return 0

        def kill(self):
            pass

    monkeypatch.setattr("ginno_runtime.tools.builtin.subprocess.Popen", FakeProc)
    monkeypatch.setattr("ginno_runtime.tools.builtin.snapshot_images", lambda p: {})
    read_file, write_file, glob_files, grep_files, edit_file, bash = build_builtin_tools(Path(ws))
    bash.invoke({"command": "git status", "timeout": 5})
    assert captured["argv"][2] == "git status"
