"""Unit tests for a2a.node（N1 headless 入口）—— CLI 解析 / 起停幂等 /
能力地址签发（token 只存 hash）/ 无 GUI 依赖 / GINNO_HOME 隔离。

真实 uvicorn 起停一律 port=0 随机端口并立即 stop，不长时间占端口。
路径隔离靠根 conftest 的 isolated_home（GINNO_HOME→tmp_path）+ 本文件
显式 --root，绝不触碰真实 ~/.ginno 与 ~/ginno-a2a-workspace。
"""

from __future__ import annotations

import asyncio
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime.a2a import address, identity, inbound, node
from ginno_runtime.a2a.auth import TokenStore, token_hash

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean_handles():
    inbound._HANDLES.clear()
    yield
    inbound._HANDLES.clear()


def _cfg(tmp_path: Path, argv: tuple[str, ...] | list[str] = ()) -> node.NodeConfig:
    """走真实 resolve_config（钉 GINNO_HOME + layout + settings 落盘）。
    恒带 --root 指向 tmp，避免默认 root 在真实用户 home 建目录。"""
    args = node.build_parser().parse_args(["--root", str(tmp_path / "ws"), *argv])
    return node.resolve_config(args)


# -- CLI 参数解析 --------------------------------------------------------------


def test_parse_listen_defaults():
    assert node.parse_listen(None, None) == ("loopback", "", 8790)


@pytest.mark.parametrize(
    "listen,expected",
    [
        (":9000", ("loopback", "", 9000)),
        ("localhost:9000", ("loopback", "", 9000)),
        ("127.0.0.1:9000", ("custom", "127.0.0.1", 9000)),
        ("192.168.1.5:8790", ("custom", "192.168.1.5", 8790)),
        ("[::1]:9000", ("custom", "::1", 9000)),
    ],
)
def test_parse_listen_forms(listen, expected):
    assert node.parse_listen(listen, None) == expected


@pytest.mark.parametrize(
    "bad",
    ["abc", "1.2.3.4", "host:notaport", "1.2.3.4:99999", "1.2.3.4:-1",
     "example.com:80", "[::1:9000"],
)
def test_parse_listen_invalid(bad):
    with pytest.raises(ValueError):
        node.parse_listen(bad, None)


def test_parse_listen_explicit_modes():
    assert node.parse_listen(":9001", "loopback") == ("loopback", "", 9001)
    assert node.parse_listen(":9001", "lan") == ("lan", "", 9001)
    assert node.parse_listen("10.0.0.1:9001", "custom") == ("custom", "10.0.0.1", 9001)


@pytest.mark.parametrize(
    "listen,mode",
    [("10.0.0.1:9000", "loopback"), ("192.168.1.5:9000", "lan")],
)
def test_parse_listen_mode_conflict_rejected(listen, mode):
    with pytest.raises(ValueError):
        node.parse_listen(listen, mode)


def test_parse_listen_custom_requires_ip():
    with pytest.raises(ValueError):
        node.parse_listen(None, "custom")
    with pytest.raises(ValueError):
        node.parse_listen(":9000", "custom")
    with pytest.raises(ValueError):
        node.parse_listen("not-an-ip:9000", "custom")


def test_wildcard_fail_closed_end_to_end(tmp_path):
    """0.0.0.0 在解析层放行到 custom，但 resolve_bind/ensure 一律拒绝。"""
    mode, iface, _port = node.parse_listen("0.0.0.0:8790", None)
    assert mode == "custom"
    with pytest.raises(ValueError):
        inbound.resolve_bind(mode, iface)
    cfg = _cfg(tmp_path, ["--listen", "0.0.0.0:8790"])
    with pytest.raises(ValueError):
        node.ensure_node_listener(cfg)


def test_cli_help_exits_zero(capsys):
    with pytest.raises(SystemExit) as exc:
        node.build_parser().parse_args(["--help"])
    assert exc.value.code == 0
    assert "ginno node" in capsys.readouterr().out


def test_cli_bad_bind_mode_choice():
    with pytest.raises(SystemExit) as exc:
        node.build_parser().parse_args(["--bind-mode", "wan"])
    assert exc.value.code == 2


def test_cli_main_dispatch_help(capsys):
    """cli.py 的 node 分流真实可达（不触 exec 主路径）。"""
    from ginno_runtime.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["node", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "--listen" in out and "--bind-mode" in out and "--tls-cert" in out


def test_cli_tls_pair_required(tmp_path):
    assert node.cli_main(["--tls-cert", str(tmp_path / "c.pem")]) == 2
    assert node.cli_main(["--tls-key", str(tmp_path / "k.pem")]) == 2


def test_cli_tls_missing_file(tmp_path):
    cert = tmp_path / "c.pem"
    cert.write_text("x")
    assert node.cli_main(["--tls-cert", str(cert), "--tls-key", str(tmp_path / "no.pem")]) == 2


def test_cli_bad_listen_returns_2():
    assert node.cli_main(["--listen", "garbage"]) == 2


# -- 缺省值 / settings 落盘 / 优先级 -------------------------------------------


def test_resolve_defaults_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # 默认 root 的 expanduser 也隔离
    # 不走 _cfg（它恒注入 --root）——本例就是要验证缺省 root
    cfg = node.resolve_config(node.build_parser().parse_args([]))
    assert cfg.bind_mode == "loopback"
    assert cfg.port == 8790
    assert cfg.name == node.DEFAULT_NAME
    assert cfg.trust == "readonly"
    assert cfg.caps == ["L0"]
    assert cfg.scheme == "http"  # 无 TLS 如实广告 http
    assert cfg.root == tmp_path / "ginno-a2a-workspace"
    assert cfg.root.is_dir()


def test_settings_persisted(tmp_path):
    cfg = _cfg(tmp_path, ["--name", "nas-1", "--listen", ":9001"])
    settings = json.loads(paths.settings_path().read_text())
    hub = settings["connectors"]["a2a"]
    assert hub["inbound_enabled"] is True
    assert hub["node_enabled"] is True
    assert hub["port"] == 9001
    assert hub["node_name"] == "nas-1"
    assert hub["bind_mode"] == "loopback"
    assert hub["trust"] == "readonly"
    assert hub["caps"] == ["L0"]
    assert hub["inbound_exec_root"] == str(cfg.root)


def test_settings_restore_on_bare_restart(tmp_path):
    _cfg(tmp_path, ["--name", "nas-1", "--listen", ":9001"])
    cfg2 = _cfg(tmp_path)  # 全缺省 → 恢复存值
    assert cfg2.port == 9001
    assert cfg2.name == "nas-1"


def test_cli_overrides_stored(tmp_path):
    _cfg(tmp_path, ["--listen", ":9001"])
    cfg = _cfg(tmp_path, ["--listen", ":9002"])
    assert cfg.port == 9002


def test_policy_normalize_fail_closed():
    assert node._normalize_policy({}) == ("readonly", ["L0"])
    assert node._normalize_policy({"trust": "bogus", "caps": ["L9"]}) == ("readonly", ["L0"])
    assert node._normalize_policy({"trust": "normal", "caps": ["L0", "L1"]}) == (
        "normal",
        ["L0", "L1"],
    )


# -- GINNO_HOME 隔离 -----------------------------------------------------------


def test_resolve_home_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("GINNO_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert node.resolve_home(None) == tmp_path / ".ginno-node"  # 默认不踩 ~/.ginno
    monkeypatch.setenv("GINNO_HOME", str(tmp_path / "env-home"))
    assert node.resolve_home(None) == tmp_path / "env-home"
    assert node.resolve_home(str(tmp_path / "cli-home")) == tmp_path / "cli-home"


def test_default_home_isolated_from_desktop(tmp_path, monkeypatch):
    monkeypatch.delenv("GINNO_HOME", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    cfg = _cfg(tmp_path)  # 无 --home、无 env → ~/.ginno-node
    assert cfg.home == tmp_path / ".ginno-node"
    assert paths.home() == tmp_path / ".ginno-node"
    assert os.environ["GINNO_HOME"] == str(tmp_path / ".ginno-node")
    assert paths.settings_path().is_file()
    assert paths.a2a_dir() == tmp_path / ".ginno-node" / "a2a"
    assert not (tmp_path / ".ginno").exists()  # 桌面端目录零触碰


def test_home_flag_wins_over_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path / "env"))
    cfg = _cfg(tmp_path, ["--home", str(tmp_path / "cli")])
    assert cfg.home == tmp_path / "cli"
    assert paths.home() == tmp_path / "cli"


# -- 起停幂等（ensure 语义 + 真实 uvicorn，port=0 即起即停）--------------------


def test_ensure_same_config_same_handle(tmp_path):
    cfg = _cfg(tmp_path, ["--listen", "127.0.0.1:0"])
    assert node.ensure_node_listener(cfg) is node.ensure_node_listener(cfg)


def test_ensure_different_port_different_handle(tmp_path):
    h1 = node.ensure_node_listener(_cfg(tmp_path, ["--listen", "127.0.0.1:0"]))
    h2 = node.ensure_node_listener(_cfg(tmp_path, ["--listen", "127.0.0.1:8791"]))
    assert h1 is not h2


async def test_start_stop_idempotent_real_listener(tmp_path):
    cfg = _cfg(tmp_path, ["--listen", "127.0.0.1:0", "--name", "nas-1"])
    handle = node.ensure_node_listener(cfg)
    assert not handle.running
    await handle.start()
    try:
        assert handle.running
        port = handle.actual_port
        assert port and port > 0
        await handle.start()  # 幂等：重复 start 不另起
        assert handle.running and handle.actual_port == port
        async with httpx.AsyncClient() as c:
            ok = await c.get(
                f"http://127.0.0.1:{port}/a2a/{handle.token}/.well-known/agent-card.json"
            )
            assert ok.status_code == 200
            assert ok.json()["name"] == "nas-1"
            denied = await c.get(f"http://127.0.0.1:{port}/a2a/")
            assert denied.status_code == 401
    finally:
        await handle.stop()
    assert not handle.running
    await handle.stop()  # 幂等：重复 stop 不抛
    assert not handle.running


async def test_run_node_blocks_until_event_then_stops(tmp_path, capsys):
    cfg = _cfg(tmp_path, ["--listen", "127.0.0.1:0"])
    ev = asyncio.Event()
    ev.set()  # 起服即收，验证完整 run_node 路径不阻塞测试
    assert await node.run_node(cfg, stop_event=ev) == 0
    out = capsys.readouterr().out
    assert "listening on http://127.0.0.1:" in out
    assert "trust=readonly caps=L0" in out
    assert not node.ensure_node_listener(cfg).running


# -- 能力地址签发：明文只打印一次 / token 只存 hash / 指纹格式 ------------------


async def test_first_issue_prints_plaintext_once_hash_only(tmp_path, capsys):
    cfg = _cfg(tmp_path, ["--listen", "127.0.0.1:0"])
    ev = asyncio.Event()
    ev.set()
    await node.run_node(cfg, stop_event=ev)
    out = capsys.readouterr().out
    addr_lines = [ln for ln in out.splitlines() if "/a2a/" in ln and "****" not in ln]
    assert len(addr_lines) == 1  # 明文地址恰好一行
    parsed = address.parse_address(addr_lines[0].strip())
    assert parsed.is_capability
    assert len(parsed.token) == 26
    # 明文 http **不广告 TLS 指纹**（2026-11-16）：#fp 是证书指纹，明文无证书
    # 可钉，附上会让 client 去做 TLS 校验并必然 SSL record layer failure。
    assert parsed.fingerprint in ("", None)
    assert "#fp=" not in addr_lines[0]
    handle = node.ensure_node_listener(cfg)
    assert parsed.token == handle.token
    # 身份本身仍有合法指纹（证书已生成），只是明文地址不携带它
    assert re.fullmatch(r"[A-Z2-7]{26}", handle.get_identity().fingerprint)
    # TokenStore 只存 SHA-256 hash：明文不出现在 tokens.json 与 list() 任何字段
    store_path = paths.a2a_dir() / "tokens.json"
    raw = store_path.read_text()
    assert parsed.token not in raw
    assert token_hash(parsed.token) in json.loads(raw)["tokens"]
    for record in TokenStore(store_path).list():
        assert parsed.token not in json.dumps(record)
    # 私钥 0600（identity 层契约）
    key = paths.a2a_dir() / "inbound-key.pem"
    assert stat.S_IMODE(key.stat().st_mode) == 0o600


async def test_second_run_masks_token_and_plain_http_has_no_fp(tmp_path, capsys):
    """二次运行掩码 token；明文 http 掩码地址不得带 #fp（与真实地址同口径）。

    原名 ..._keeps_fingerprint 断言掩码里有 fp——那是 TLS 场景的语义。
    明文 http 无证书可钉，build_address 已丢 fp，掩码显示也须一致
    （2026-11-16；指纹稳定性由 TLS 用例覆盖）。
    """
    cfg = _cfg(tmp_path, ["--listen", "127.0.0.1:0"])
    ev = asyncio.Event()
    ev.set()
    await node.run_node(cfg, stop_event=ev)
    first = capsys.readouterr().out
    addr = next(ln for ln in first.splitlines() if "/a2a/" in ln and "****" not in ln)
    parsed = address.parse_address(addr.strip())
    assert parsed.fingerprint in ("", None), "明文地址本就不该带指纹"
    inbound._HANDLES.clear()  # 模拟进程重启
    cfg2 = _cfg(tmp_path, ["--listen", "127.0.0.1:0"])
    await node.run_node(cfg2, stop_event=ev)
    second = capsys.readouterr().out
    assert parsed.token not in second  # 明文不可回读（不再打印）
    assert "****" in second
    assert "不再回显" in second
    assert "#fp=" not in second, "明文掩码地址不得显示空 #fp="
    assert "****#fp=" not in second


# -- TLS：成对校验 + https 广告 + 真实 HTTPS 起停 ------------------------------


async def test_tls_https_address_and_real_serve(tmp_path, capsys):
    ident = identity.generate_identity(name="node-tls", san_ips=["127.0.0.1"])
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    identity.save_identity(ident, cert_path=cert, key_path=key)
    cfg = _cfg(
        tmp_path,
        ["--listen", "127.0.0.1:0", "--tls-cert", str(cert), "--tls-key", str(key)],
    )
    assert cfg.scheme == "https"
    ev = asyncio.Event()
    ev.set()
    await node.run_node(cfg, stop_event=ev)
    out = capsys.readouterr().out
    addr = next(ln for ln in out.splitlines() if ln.startswith("https://"))
    parsed = address.parse_address(addr)
    assert parsed.fingerprint == ident.fingerprint
    # 同一 handle 再起一次，真实 HTTPS 请求验证证书直通 uvicorn
    handle = node.ensure_node_listener(cfg)
    await handle.start()
    try:
        async with httpx.AsyncClient(verify=False) as c:
            resp = await c.get(
                f"https://127.0.0.1:{handle.actual_port}/a2a/{handle.token}"
                "/.well-known/agent-card.json"
            )
            assert resp.status_code == 200
    finally:
        await handle.stop()


# -- 无 GUI 依赖：子进程扫真实启动链的 sys.modules ------------------------------


def test_no_gui_import_chain(tmp_path):
    """import node + 完整 resolve_config 启动链后，sys.modules 里不得出现
    tauri / 桌面主 server / web 前端资源 / graph 等 GUI 侧模块。"""
    code = (
        "import sys\n"
        "import ginno_runtime.a2a.node as node\n"
        f"args = node.build_parser().parse_args(['--root', {str(tmp_path / 'ws')!r}])\n"
        "node.resolve_config(args)\n"
        "print('\\n'.join(sorted(sys.modules)))\n"
    )
    env = dict(os.environ, GINNO_HOME=str(tmp_path / "node-home"))
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    mods = proc.stdout.splitlines()
    assert "ginno_runtime.a2a.node" in mods
    assert not any("tauri" in m.lower() for m in mods), "import 链出现 tauri 模块"
    for forbidden in ("ginno_runtime.server", "ginno_runtime.web", "ginno_runtime.graph"):
        assert not any(m == forbidden or m.startswith(forbidden + ".") for m in mods), (
            f"import 链出现桌面侧模块 {forbidden}"
        )


def test_frozen_entry_dispatches_node_not_desktop_server(tmp_path):
    """`python -m ginno_runtime node` 必须分流到 node，不能落到桌面 server。

    __main__.py 原先只认 exec，node 会被当未知参数交给桌面 server——
    frozen/`-m` 形态下起的是 GUI 后端而不是 A2A node。两条入口都要通。
    """
    env = dict(os.environ, GINNO_HOME=str(tmp_path / "node-home"))
    proc = subprocess.run(
        [sys.executable, "-m", "ginno_runtime", "node", "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ginno node" in proc.stdout
    assert "--listen" in proc.stdout
    # 桌面 server 的入口特征不应出现
    assert "uvicorn running" not in proc.stdout.lower()


def test_frozen_entry_exec_still_works(tmp_path):
    """加 node 分流不能破坏既有 exec 主路径。"""
    env = dict(os.environ, GINNO_HOME=str(tmp_path / "node-home"))
    proc = subprocess.run(
        [sys.executable, "-m", "ginno_runtime", "exec", "--help"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert "ginno exec" in proc.stdout
