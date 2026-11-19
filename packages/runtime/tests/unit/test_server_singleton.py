"""单实例守卫（server.ensure_sole_runtime，2026-11-19 启动序修复）。

背景（三次实锤 07:58×2/08:12×1）：本地 e2e 误启第二 runtime 实例时，
uvicorn 的 lifespan（含 reconcile_orphan_subagents 孤儿收编）先于绑定
失败执行——第二实例看不到主实例内存运行态，把磁盘 meta 里 running 的
子代理误判孤儿 settle 成 stopped，Errno 48 退出前已污染共享状态
（「修复前红灯」由既有 test_subagent_p3.
test_reconcile_orphan_subagents_settles_live_metas 存档：收编确实会把
活 meta 打成 stopped）。守卫把探测提到 main() 最早、uvicorn/app 构造
之前 = 任何共享状态操作之前。
"""

import socket
import sys

import pytest

from ginno_runtime.server import ensure_sole_runtime, main


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Occupied:
    """占住一个端口（真实监听）。"""

    def __init__(self) -> None:
        self.s = socket.socket()
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind(("127.0.0.1", 0))
        self.s.listen(1)
        self.port = self.s.getsockname()[1]

    def close(self) -> None:
        self.s.close()


def test_guard_exits_when_port_occupied(capsys):
    occ = _Occupied()
    try:
        with pytest.raises(SystemExit) as ei:
            ensure_sole_runtime(occ.port, timeout=0.5)
        assert ei.value.code == 3
        err = capsys.readouterr().err
        assert "已被另一个 Ginno runtime 占用" in err
        assert "不触碰任何会话状态" in err
    finally:
        occ.close()


def test_guard_passes_when_port_free():
    # 空闲端口：原样放行（无异常、无输出）
    assert ensure_sole_runtime(_free_port(), timeout=0.5) is None


def test_main_exits_before_uvicorn_when_occupied(monkeypatch, capsys):
    """顺序红线：端口被占时 main() 必须在 uvicorn.run 之前退出——
    uvicorn.run 不被调用 = app/lifespan（孤儿收编等状态操作）从未构造，
    第二实例对运行中会话零接触。"""
    occ = _Occupied()
    try:
        monkeypatch.setenv("GINNO_RUNTIME_PORT", str(occ.port))
        import uvicorn

        called = []

        def _fail_if_reached(*a, **kw):  # pragma: no cover - 红线即失败
            called.append(1)
            raise AssertionError("uvicorn.run 被调用：守卫没拦住！")

        monkeypatch.setattr(uvicorn, "run", _fail_if_reached)
        with pytest.raises(SystemExit) as ei:
            main()
        assert ei.value.code == 3
        assert called == []
        assert "已被另一个 Ginno runtime 占用" in capsys.readouterr().err
    finally:
        occ.close()


def test_main_proceeds_when_port_free(monkeypatch):
    """正常路径不回归：端口空闲 → 守卫放行 → uvicorn.run 正常接管。"""
    port = _free_port()
    monkeypatch.setenv("GINNO_RUNTIME_PORT", str(port))
    import uvicorn

    seen = {}

    def _fake_run(app, **kw):
        seen["port"] = kw.get("port")
        seen["app"] = app is not None

    monkeypatch.setattr(uvicorn, "run", _fake_run)
    main()
    assert seen == {"port": port, "app": True}
