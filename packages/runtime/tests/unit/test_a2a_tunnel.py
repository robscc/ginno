"""ginno tunnel（P1-4）单测：命令组装 / 地址口径 / 分流 / 重连语义。

不真连 SSH（无外部依赖）：subprocess.run 用 monkeypatch 桩掉，验调用参数与
重连循环行为。被测对象本身不被 mock。
"""

from __future__ import annotations

import pytest

from ginno_runtime.a2a import tunnel


def test_build_ssh_argv_forwards_loopback_only():
    """只做 -L 本地转发，远端仍绑环回（绝不放行远端端口）。"""
    argv = tunnel.build_ssh_argv(
        host="1.2.3.4", user="root", key="/k", local_port=18790, remote_port=8790
    )
    assert argv[0] == "ssh"
    assert "-N" in argv
    i = argv.index("-L")
    assert argv[i + 1] == "18790:127.0.0.1:8790"
    assert argv[-1] == "root@1.2.3.4"
    assert "-i" in argv and argv[argv.index("-i") + 1] == "/k"
    # 保活与转发失败即退（外层才能重连）
    assert "ServerAliveInterval=30" in argv
    assert "ExitOnForwardFailure=yes" in argv
    # 绝不出现 -R（反向转发会把远端暴露出去）
    assert "-R" not in argv


def test_build_ssh_argv_without_key_and_with_extra():
    argv = tunnel.build_ssh_argv(
        host="h", user="u", key=None, local_port=1, remote_port=2, extra=["-p", "2222"]
    )
    assert "-i" not in argv
    assert argv[-3:] == ["-p", "2222", "u@h"]


def test_local_capability_address_is_plain_http_without_fp():
    """隧道地址是明文 http 且不带 #fp（加密由隧道提供，与 build_address 同口径）。"""
    tok = "Q3EQXAI3LK7GGT5KFSEYJWAXQB"
    addr = tunnel.local_capability_address(18790, tok)
    assert addr == f"http://127.0.0.1:18790/a2a/{tok}"
    assert "#fp=" not in addr


def test_run_tunnel_once_returns_exit_code(monkeypatch):
    """once=True 断线即退，返回 ssh 退出码（供脚本/测试）。"""
    calls = []

    class _P:
        returncode = 7

    def fake_run(argv, check=False):
        calls.append(argv)
        return _P()

    monkeypatch.setattr(tunnel.subprocess, "run", fake_run)
    monkeypatch.setattr(tunnel.shutil, "which", lambda _: "/usr/bin/ssh")
    rc = tunnel.run_tunnel(host="h", once=True)
    assert rc == 7
    assert len(calls) == 1


def test_run_tunnel_reconnects_until_interrupt(monkeypatch):
    """默认断线自动重连；Ctrl-C 干净退出 0。"""
    calls = []

    class _P:
        returncode = 255

    def fake_run(argv, check=False):
        calls.append(argv)
        if len(calls) >= 3:
            raise KeyboardInterrupt
        return _P()

    monkeypatch.setattr(tunnel.subprocess, "run", fake_run)
    monkeypatch.setattr(tunnel.shutil, "which", lambda _: "/usr/bin/ssh")
    monkeypatch.setattr(tunnel.time, "sleep", lambda _: None)
    rc = tunnel.run_tunnel(host="h")
    assert rc == 0
    assert len(calls) == 3, "断线后必须重连"


def test_run_tunnel_missing_ssh_fails_loud(monkeypatch, capsys):
    monkeypatch.setattr(tunnel.shutil, "which", lambda _: None)
    rc = tunnel.run_tunnel(host="h")
    assert rc == 2
    assert "ssh" in capsys.readouterr().err


def test_run_tunnel_prints_capability_address(monkeypatch, capsys):
    class _P:
        returncode = 0

    monkeypatch.setattr(tunnel.subprocess, "run", lambda *a, **k: _P())
    monkeypatch.setattr(tunnel.shutil, "which", lambda _: "/usr/bin/ssh")
    tok = "Q3EQXAI3LK7GGT5KFSEYJWAXQB"
    tunnel.run_tunnel(host="h", token=tok, once=True)
    out = capsys.readouterr().out
    assert f"http://127.0.0.1:{tunnel.DEFAULT_LOCAL_PORT}/a2a/{tok}" in out


def test_parser_defaults():
    args = tunnel.build_parser().parse_args(["--host", "h"])
    assert args.local_port == tunnel.DEFAULT_LOCAL_PORT
    assert args.remote_port == tunnel.DEFAULT_REMOTE_PORT
    assert args.user == "root"
    assert args.once is False


def test_cli_main_dispatches(monkeypatch):
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return 0

    monkeypatch.setattr(tunnel, "run_tunnel", fake_run)
    rc = tunnel.cli_main(["--host", "h", "--local-port", "19000", "--once"])
    assert rc == 0
    assert seen["host"] == "h"
    assert seen["local_port"] == 19000
    assert seen["once"] is True


@pytest.mark.parametrize("entry", ["cli", "main"])
def test_both_entrypoints_dispatch_tunnel(entry, monkeypatch):
    """console script 与 python -m 两条入口都必须分流到 tunnel。"""
    import importlib

    if entry == "cli":
        mod = importlib.import_module("ginno_runtime.cli")
        monkeypatch.setattr(
            "ginno_runtime.a2a.tunnel.cli_main", lambda argv: 42
        )
        with pytest.raises(SystemExit) as ei:
            mod.main(["tunnel", "--host", "h"])
        assert ei.value.code == 42
    else:
        # __main__ 在 import 时即分流，用 runpy 验
        import runpy
        import sys

        monkeypatch.setattr(sys, "argv", ["ginno_runtime", "tunnel", "--host", "h"])
        monkeypatch.setattr("ginno_runtime.a2a.tunnel.cli_main", lambda argv: 43)
        with pytest.raises(SystemExit) as ei:
            runpy.run_module("ginno_runtime", run_name="__main__")
        assert ei.value.code == 43
