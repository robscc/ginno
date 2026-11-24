"""Packaged import health：frozen bundle 懒加载 import 与元数据探针（无 UI）。

背景（2026-11-19 定时任务事故）：bundle 瘦身剥光 *.dist-info 后，
``httpx2/__version__.py`` 在 import 顶层查自身元数据（无兜底）——
懒加载 ``import anthropic``（链上 import httpx2）在 frozen 环境即炸
``PackageNotFoundError``，venv 里单测全绿防不住，``make smoke`` 的
/api/health 也触不到（anthropic 是懒加载）。本用例对真 dist binary 起服务，
经 HTTP 端点触发两条懒加载链：

1. ``GET /api/connectors`` —— connectors registry + mods 路径
   （``mods/ops.py`` 的 ``version("ginno-runtime")`` 同款地雷）；
2. ``POST /api/providers/anthropic/verify`` —— 触发 ``import anthropic``
   → ``httpx2.__version__`` 元数据查询（事故主雷）。

verify 对无 key 环境返回业务失败（ok:false）是正常的——断言只钉
「不因 import/元数据失败」：错误体不得含 PackageNotFoundError，HTTP 不得 500。

Skips gracefully when the binary isn't built（与其它 packaged 用例同约定）。
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

import _sidecar as sc
import pytest

pytestmark = pytest.mark.e2e

# __file__ = packages/runtime/tests/e2e/x.py → parents[2] = packages/runtime
BIN = Path(__file__).resolve().parents[2] / "dist" / "ginno-runtime" / "ginno-runtime"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port != 8787
    return port


@pytest.fixture(scope="module")
def running_sidecar(tmp_path_factory):
    if not BIN.exists():
        pytest.skip("dist bundle 未构建（make runtime）")
    home = tmp_path_factory.mktemp("import-health-home")
    # 预置带假 key 的 anthropic 配置：verify 在 `import anthropic` 之前有空
    # key 提前返回（providers.py），假 key + 指向黑洞 base_url 让链路走到
    # import + client 构造（import 炸=500，网络败=业务错误——只钉 import 层）。
    (home / "settings.json").write_text(json.dumps({
        "providers": {
            "anthropic": {
                "enabled": True, "protocol": "anthropic", "api_key": "k-probe",
                "default_model": "claude-probe", "base_url": "http://127.0.0.1:1",
                "max_tokens": 1, "temperature": 0.7, "timeout_s": 3,
            },
        },
    }))
    # 第一次正常启动必须能收编这个真实形状的孤儿记录。主进程就绪后，
    # 再用同一记录模拟主进程已有运行中的子代理。
    index = home / "projects" / "default" / "sessions" / "_index.json"
    index.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    running = [{
        "created": now,
        "updated": now,
        "id": "startup-probe-child",
        "type": "subagent",
        "parent_session_id": "startup-probe-parent",
        "subagent": {"status": "running"},
    }]
    index.write_text(json.dumps(running))
    port = _free_port()
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(home),
        "GINNO_HOME": str(home),
        "GINNO_RUNTIME_PORT": str(port),
        "NO_PROXY": "127.0.0.1",
    }
    base = f"http://127.0.0.1:{port}"
    log_path = home.parent / f"{home.name}.stderr.log"
    with log_path.open("wb") as stderr:
        proc = sc.spawn_sidecar(str(BIN.resolve()), env, err=stderr)
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline and proc.poll() is None:
                try:
                    with urllib.request.urlopen(base + "/api/health", timeout=2) as response:
                        if response.status == 200 and json.load(response)["ok"] is True:
                            break
                except OSError:
                    time.sleep(0.1)
            else:
                pytest.fail(f"dist sidecar 未就绪，exit={proc.poll()}: {log_path.read_text()[-4000:]}")
            yield base, env, proc, index, running
        finally:
            sc.stop_sidecar(proc)


@pytest.fixture(scope="module")
def sidecar(running_sidecar):
    return running_sidecar[0]


def _get(base: str, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=15) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def _post(base: str, path: str) -> tuple[int, str]:
    req = urllib.request.Request(base + path, method="POST", data=b"{}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def test_connectors_path_mods_metadata_ok(sidecar):
    """GET /api/connectors：registry+mods 链在 frozen 里不因元数据缺失炸。"""
    code, body = _get(sidecar, "/api/connectors")
    assert code == 200, f"HTTP {code}: {body[:200]}"
    assert "PackageNotFoundError" not in body


def test_anthropic_lazy_import_ok(sidecar):
    """POST /api/providers/anthropic/verify：懒加载 import anthropic 不炸。

    无 key → 业务失败（ok:false / error dict）是预期；钉的是 import/元数据
    层——httpx2 dist-info 缺失会让这里 500 且含 PackageNotFoundError。
    """
    code, body = _post(sidecar, "/api/providers/anthropic/verify")
    assert code != 500, f"provider verify 500（疑似 import/元数据炸）: {body[:300]}"
    assert "PackageNotFoundError" not in body
    # 结构完整性：verify 必须返回 JSON dict（业务失败也算通过）
    try:
        d = json.loads(body)
        assert isinstance(d, dict)
    except Exception:
        pytest.fail(f"verify 响应非 JSON: {body[:200]}")


def test_duplicate_start_exits_before_reconciling_running_subagent(running_sidecar):
    base, env, primary, index, running = running_sidecar
    # 正对照：空闲端口上的同一 bundle 确实运行了这项 lifespan 任务。
    assert json.loads(index.read_bytes())[0]["subagent"]["status"] == "stopped"
    index.write_text(json.dumps(running))
    settings = Path(env["GINNO_HOME"]) / "settings.json"
    before = {path: path.read_bytes() for path in (index, settings)}
    assert primary.poll() is None

    duplicate = sc.spawn_sidecar(str(BIN.resolve()), env, err=subprocess.PIPE)
    try:
        _, stderr = duplicate.communicate(timeout=30)
    finally:
        sc.stop_sidecar(duplicate)

    after = {path: path.read_bytes() for path in before}
    status = json.loads(after[index])[0]["subagent"]["status"]
    diagnostic = stderr.decode("utf-8", "replace")
    assert (duplicate.returncode, status) == (3, "running"), diagnostic
    assert "单实例守卫" in diagnostic
    assert after == before
    assert primary.poll() is None
    code, body = _get(base, "/api/health")
    assert code == 200 and json.loads(body)["ok"] is True
