"""产物回报补充断言（N3 → headless 设计 §2.5；N2b 已实现，此处只补测试）。

§2.5 定案：产物留在 exec_root，Task 以 A2A Artifact 回**路径+摘要+元信息**，
**不传文件内容**（L2 文件传取是独立 P1）。

N2b 已在 inbound_exec._collect_artifacts 实现并有 _run_job 级用例
（test_a2a_inbound_exec.py::test_artifact_returns_path_metadata_not_content）。
本文件补三层断言：
1. _collect_artifacts 直测：文本/二进制判别、未变更不报、隐藏文件跳过、
   序列化 blob 绝不包含文件内容；
2. 数量上限 _MAX_ARTIFACTS（防大工作区把 JSON-RPC 响应撑爆）；
3. 出站工具面：Task.artifacts 经 delegate 结果回流为路径+摘要文本
   （真实 A2AClient + MockTransport，只桩传输层）。
"""

from __future__ import annotations

import httpx
import pytest

from ginno_runtime import jsonx as json
from ginno_runtime.a2a import inbound_exec
from ginno_runtime.a2a.client import A2AClient
from ginno_runtime.a2a.contexts import ContextMap
from ginno_runtime.a2a.inbound_exec import _MAX_ARTIFACTS, _collect_artifacts, _snapshot
from ginno_runtime.a2a.peers import PeerRecord, PeerStore
from ginno_runtime.tools import a2a_tools

pytestmark = pytest.mark.unit


# --------------------- 执行侧：_collect_artifacts 直测 --------------------- #


def test_collect_artifacts_path_summary_metadata_no_content(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    unchanged = root / "unchanged.txt"
    unchanged.write_text("old file", encoding="utf-8")
    before = _snapshot(str(root))

    secret_text = "TOP-SECRET-TEXT-BODY-DO-NOT-TRANSFER"
    secret_bin = b"\x00\x01BIN-SECRET-PAYLOAD\xff"
    (root / "report.md").write_text(secret_text, encoding="utf-8")
    (root / "data.bin").write_bytes(secret_bin)
    hidden_dir = root / ".hidden"
    hidden_dir.mkdir()
    (hidden_dir / "secret.txt").write_text("hidden secret", encoding="utf-8")

    after = _snapshot(str(root))
    arts = _collect_artifacts(str(root), before, after)

    names = {a.name for a in arts}
    assert names == {"report.md", "data.bin"}  # 未变更不报、隐藏文件跳过
    by_name = {a.name: a for a in arts}
    for name, kind, size in (
        ("report.md", "text", len(secret_text.encode("utf-8"))),
        ("data.bin", "binary", len(secret_bin)),
    ):
        a = by_name[name]
        assert a.metadata["path"] == str(root / name)
        assert a.metadata["relpath"] == name
        assert a.metadata["size_bytes"] == size
        assert a.metadata["kind"] == kind
        assert a.metadata["exec_root"] == str(root)
        assert a.metadata["modified"]  # ISO 时间戳
        # parts 是路径+摘要，不是内容
        text = a.text()
        assert f"path: {root / name}" in text
        assert name in text and "bytes" in text
    # 整个 Artifact 序列化 blob 绝不包含文件内容（§2.5 红线）。
    blob = json.dumps([a.to_dict() for a in arts], ensure_ascii=False)
    assert secret_text not in blob
    assert "BIN-SECRET-PAYLOAD" not in blob
    assert "hidden secret" not in blob


def test_collect_artifacts_cap(tmp_path):
    """变更文件超过上限只报前 _MAX_ARTIFACTS 个（响应有界）。"""
    root = tmp_path / "ws"
    root.mkdir()
    before = _snapshot(str(root))
    for i in range(_MAX_ARTIFACTS + 5):
        (root / f"f{i:03d}.txt").write_text(f"body {i}", encoding="utf-8")
    arts = _collect_artifacts(str(root), before, _snapshot(str(root)))
    assert len(arts) == _MAX_ARTIFACTS


def test_collect_artifacts_empty_when_no_change(tmp_path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.txt").write_text("stable", encoding="utf-8")
    snap = _snapshot(str(root))
    assert _collect_artifacts(str(root), snap, snap) == []


# ------------------- 出站工具面：Artifact 路径回流 ------------------------- #


@pytest.fixture(autouse=True)
def _enabled(monkeypatch):
    monkeypatch.setenv("GINNO_A2A", "1")


@pytest.fixture()
def store(tmp_path):
    return PeerStore(tmp_path / "peers.json", contexts_path=tmp_path / "contexts.json")


@pytest.fixture()
def cm(tmp_path):
    return ContextMap(tmp_path / "contexts.json")


async def test_delegate_result_surfaces_artifact_path_not_content(store, cm, monkeypatch):
    """对端按 §2.5 回 Artifact（路径+摘要）→ delegate 结果忠实回流路径文本。

    真实 A2AClient + MockTransport（只桩传输层，不 mock 被测工具）。
    """
    secret_content = "FILE-BODY-MUST-NOT-APPEAR-IN-CONTEXT"
    art_text = "path: /ws/report.md\nreport.md (text, 42 bytes, modified 2026-11-15T00:00:00+00:00)"

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        result = {
            "id": "t-1", "contextId": "c-1", "status": {"state": "completed"},
            "artifacts": [{
                "artifactId": "a1", "name": "report.md",
                "parts": [{"kind": "text", "text": art_text}],
                "metadata": {"path": "/ws/report.md", "size_bytes": 42, "kind": "text"},
            }],
        }
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": body["id"], "result": result})

    class TransportClient(A2AClient):
        def __init__(self, endpoint, **kw):
            self._injected = httpx.AsyncClient(transport=httpx.MockTransport(handler))
            super().__init__(endpoint, http_client=self._injected, **kw)

        async def aclose(self):
            await super().aclose()
            await self._injected.aclose()

    monkeypatch.setattr(a2a_tools, "A2AClient", TransportClient)
    store.add(PeerRecord(slug="node-box", name="无桌面机", endpoint="https://node.example/a2a"))
    tools = {t.name: t for t in a2a_tools.build_a2a_tools(store=store, contexts=cm)}
    out = await tools["a2a_node_box_delegate"].ainvoke({"task": "生成报告"})

    assert out.startswith("[a2a:node-box]")
    assert "state=completed" in out
    assert "path: /ws/report.md" in out  # 路径回流
    assert "42 bytes" in out  # 摘要回流
    assert secret_content not in out  # 文件内容不进上下文
