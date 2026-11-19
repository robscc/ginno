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
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def sidecar(tmp_path_factory):
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
    port = _free_port()
    env = {
        **os.environ,
        "GINNO_HOME": str(home),
        "GINNO_RUNTIME_PORT": str(port),
    }
    proc = sc.spawn_sidecar(str(BIN), env)
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/api/health", timeout=2
            ) as r:
                if r.status == 200:
                    break
        except Exception:
            time.sleep(0.5)
    else:
        sc.stop_sidecar(proc)
        pytest.fail("dist sidecar 30s 未就绪")
    yield f"http://127.0.0.1:{port}"
    sc.stop_sidecar(proc)


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
