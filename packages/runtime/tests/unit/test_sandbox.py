"""入站 A2A shell 沙箱（tools/sandbox.py）。

背景：guard 的 exec_root 边界对 **shell 不生效**——_guard_kwargs 给 bash 设
target_path=exec_root 使边界检查恒过，命令串只在 readonly 下查白名单。
于是 trust=normal/full 的远端派活可用 `echo x >> /etc/hosts` 逃逸。
命令级解析不可靠（shell 图灵完备），故用 bwrap user namespace 做真隔离。

本套覆盖：开关语义、fail-closed、argv 组装、cwd 改写、桌面端零影响。
真 bwrap 隔离效果另由 skipif 用例在装有 bubblewrap 的机器上验。
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from ginno_runtime.tools import sandbox


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(sandbox.ENV_ROOT, raising=False)
    monkeypatch.delenv(sandbox.ENV_DISABLE, raising=False)


def test_desktop_context_is_untouched():
    """非入站上下文：argv 与 cwd 原样返回，桌面用户 bash 零影响。"""
    argv = ["/bin/zsh", "-lc", "echo hi"]
    assert sandbox.wrap_argv(argv, "/Users/me/proj") == argv
    assert sandbox.sandbox_cwd("/Users/me/proj") == "/Users/me/proj"
    assert sandbox.sandbox_root() == ""
    assert "off" in sandbox.describe()


def test_inbound_context_requires_sandbox(monkeypatch, tmp_path):
    monkeypatch.setenv(sandbox.ENV_ROOT, str(tmp_path))
    assert sandbox.sandbox_root() == str(tmp_path)
    # 宿主 cwd 保持原值——/work 只在沙箱内存在，Popen 的 cwd 在宿主生效
    assert sandbox.sandbox_cwd(str(tmp_path)) == str(tmp_path)


def test_disable_flag_wins(monkeypatch, tmp_path):
    """显式关闭优先（测试/受控环境用）。"""
    monkeypatch.setenv(sandbox.ENV_ROOT, str(tmp_path))
    monkeypatch.setenv(sandbox.ENV_DISABLE, "1")
    assert sandbox.sandbox_root() == ""
    argv = ["/bin/sh", "-lc", "x"]
    assert sandbox.wrap_argv(argv, str(tmp_path)) == argv


def test_missing_bwrap_fails_closed(monkeypatch, tmp_path):
    """要求沙箱但无 bwrap → 抛错，**绝不静默降级为无沙箱**。"""
    monkeypatch.setenv(sandbox.ENV_ROOT, str(tmp_path))
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
    with pytest.raises(sandbox.SandboxUnavailable, match="未安装 bwrap"):
        sandbox.wrap_argv(["/bin/sh", "-lc", "x"], str(tmp_path))


def test_bwrap_present_but_unusable_fails_closed(monkeypatch, tmp_path):
    """bwrap 在但 user namespace 被禁 → 同样 fail-closed。"""
    monkeypatch.setenv(sandbox.ENV_ROOT, str(tmp_path))
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda _: False)
    with pytest.raises(sandbox.SandboxUnavailable, match="user namespace"):
        sandbox.wrap_argv(["/bin/sh", "-lc", "x"], str(tmp_path))


def test_missing_root_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setenv(sandbox.ENV_ROOT, str(tmp_path / "nope"))
    with pytest.raises(sandbox.SandboxUnavailable, match="不存在"):
        sandbox.wrap_argv(["/bin/sh", "-lc", "x"], None)


def test_argv_binds_only_exec_root_writable(monkeypatch, tmp_path):
    """组装出的 argv：exec_root 可写绑到 /work，系统目录只读，无 /etc /home /root。"""
    monkeypatch.setenv(sandbox.ENV_ROOT, str(tmp_path))
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda _: True)
    out = sandbox.wrap_argv(["/bin/sh", "-lc", "echo hi"], str(tmp_path))
    assert out[0] == "/usr/bin/bwrap"
    # exec_root → /work 可写
    i = out.index("--bind")
    assert out[i + 1] == str(tmp_path) and out[i + 2] == "/work"
    assert "--chdir" in out and out[out.index("--chdir") + 1] == "/work"
    # 系统目录只读
    assert "--ro-bind" in out
    # 绝不把 /etc、/home、/root、/var 挂进去
    joined = " ".join(out)
    for forbidden in ("/etc", "/home", "/root", "/var"):
        assert f" {forbidden} " not in joined, f"沙箱不得暴露 {forbidden}"
    # 隔离 + 随父进程死 + 新会话（防 TIOCSTI 注入）
    assert "--unshare-all" in out
    assert "--die-with-parent" in out
    assert "--new-session" in out
    # 保留网络（节点要能出网干活）
    assert "--share-net" in out
    # 原 argv 在末尾
    assert out[-3:] == ["/bin/sh", "-lc", "echo hi"]


def test_probe_handles_missing_binary(monkeypatch):
    monkeypatch.setattr(
        sandbox.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")),
    )
    assert sandbox._probe("/usr/bin/bwrap") is False


@pytest.mark.skipif(not shutil.which("bwrap"), reason="需要 bubblewrap")
def test_real_bwrap_hides_outside_files(tmp_path):
    """真 bwrap：沙箱内看不到 exec_root 之外的文件，工作区可写。

    这是漏洞的正面回归——`cat /etc/shadow`、写 /etc/hosts 在沙箱内必须失败。
    """
    root = tmp_path / "ws"
    root.mkdir()
    os.environ[sandbox.ENV_ROOT] = str(root)
    try:
        argv = sandbox.wrap_argv(
            ["/bin/sh", "-c",
             "cat /etc/shadow 2>&1 | head -1; "
             "(echo x >> /etc/hosts) 2>&1 | head -1; "
             "echo ok > /work/probe.txt && echo WROTE"],
            str(root),
        )
        r = subprocess.run(argv, cwd="/work", capture_output=True, text=True, timeout=60)
        out = r.stdout + r.stderr
        assert "WROTE" in out, f"工作区应可写: {out}"
        assert (root / "probe.txt").read_text().strip() == "ok"
        # 沙箱内 /etc 不存在 → 读 shadow / 写 hosts 都必须失败
        assert "No such file" in out or "nonexistent" in out or "Permission denied" in out, (
            f"沙箱未隔离 /etc: {out}"
        )
    finally:
        os.environ.pop(sandbox.ENV_ROOT, None)


def test_host_cwd_never_becomes_sandbox_only_path(monkeypatch, tmp_path):
    """回归：宿主 Popen 的 cwd 绝不得是 /work（沙箱内路径）。

    实测 bug：曾让 sandbox_cwd 返回 "/work"，而 Popen 的 cwd 在**宿主**上
    生效（exec bwrap 之前）→ FileNotFoundError: '/work'。
    沙箱内工作目录由 bwrap --chdir 负责，与宿主 cwd 无关。
    """
    monkeypatch.setenv(sandbox.ENV_ROOT, str(tmp_path))
    for host_cwd in (str(tmp_path), "/some/host/dir", None):
        got = sandbox.sandbox_cwd(host_cwd)
        assert got == host_cwd
        assert got != "/work"
    # 沙箱内 cwd 仍由 --chdir /work 指定
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_probe", lambda _: True)
    out = sandbox.wrap_argv(["/bin/sh", "-lc", "x"], str(tmp_path))
    assert out[out.index("--chdir") + 1] == "/work"
