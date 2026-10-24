"""ginno exec 的对外合约（in-process，subprocess 版在 tests/e2e/test_cli_exec.py）。

覆盖 crash-safe 终帧与 GINNO_EXEC_NO_ATTACH 的「绝不探活 127.0.0.1:8787」保证。
"""

import pytest

from ginno_runtime import cli
from ginno_runtime import jsonx as json

pytestmark = pytest.mark.e2e


def _exec_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> str:
    """exec 跑起来所需的最小 env；返回隔离 home 路径。"""
    home = str(tmp_path / "home")
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    monkeypatch.setenv("GINNO_EXEC_HOME", home)
    monkeypatch.setenv("GINNO_EXEC_NO_ATTACH", "1")
    monkeypatch.chdir(tmp_path)
    return home


def test_crash_still_emits_result_frame(monkeypatch, tmp_path, capsys):
    """_run 抛未捕获异常：仍 emit 一条 error result 终帧，再以非零码退出。"""

    async def _boom(*a, **kw):
        raise RuntimeError("kaboom")

    _exec_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_run", _boom)
    with pytest.raises(SystemExit) as ei:
        cli.main(["exec", "--cwd", str(tmp_path), "--", "hi"])
    assert ei.value.code == 1
    out = capsys.readouterr().out
    frames = [json.loads(line) for line in out.splitlines() if line.strip()]
    result = [f for f in frames if f.get("type") == "result"]
    assert len(result) == 1
    assert result[0]["subtype"] == "error_deterministic"
    assert result[0]["is_error"] is True
    assert "kaboom" in result[0]["error"]


def test_crash_wrapper_covers_remote_path(monkeypatch, tmp_path, capsys):
    """_run_remote 的异常同样被兜底（wrapper 挂在两个入口上）。"""

    async def _boom(*a, **kw):
        raise ValueError("remote blew up")

    monkeypatch.delenv("GINNO_EXEC_NO_ATTACH", raising=False)
    monkeypatch.setenv("GINNO_FAKE_LLM", "1")
    monkeypatch.setenv("GINNO_EXEC_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_probe_sidecar", lambda port: True)
    monkeypatch.setattr(cli, "_run_remote", _boom)
    with pytest.raises(SystemExit) as ei:
        cli.main(["exec", "--cwd", str(tmp_path), "--", "hi"])
    assert ei.value.code == 1
    result = [
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if line.strip()
    ]
    assert result[-1]["type"] == "result" and result[-1]["is_error"] is True


def test_no_attach_never_probes_sidecar(monkeypatch, tmp_path):
    """GINNO_EXEC_NO_ATTACH=1：绝不探活本地 sidecar（独立可执行性合约）。"""
    probes: list[int] = []

    def _record(port: int) -> bool:
        probes.append(port)
        return False

    _exec_env(monkeypatch, tmp_path)
    monkeypatch.setattr(cli, "_probe_sidecar", _record)
    with pytest.raises(SystemExit) as ei:
        cli.main(["exec", "--cwd", str(tmp_path), "--", "hi"])
    assert ei.value.code == 0
    assert probes == [], "GINNO_EXEC_NO_ATTACH=1 must not probe 127.0.0.1:8787"


def test_attach_probes_sidecar_when_not_disabled(monkeypatch, tmp_path):
    """对照组：没禁用 attach 时探活确实发生（证明上一条测试的断言有意义）。"""
    probes: list[int] = []
    _exec_env(monkeypatch, tmp_path)
    monkeypatch.delenv("GINNO_EXEC_NO_ATTACH", raising=False)
    monkeypatch.setattr(cli, "_probe_sidecar", lambda port: probes.append(port) or False)
    with pytest.raises(SystemExit) as ei:
        cli.main(["exec", "--cwd", str(tmp_path), "--", "hi"])
    assert ei.value.code == 0
    assert len(probes) == 1
