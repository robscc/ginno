"""PUT /api/sessions/{id}/tags + 列表透出（session-label-icon-design.md §5）."""

from __future__ import annotations

import pytest

from ginno_runtime import jsonx as json
from ginno_runtime import paths
from ginno_runtime.testing.fake_model import script

pytestmark = pytest.mark.api


def _sid(create_session) -> str:
    return create_session([script(text="ok")])


def test_tags_roundtrip_and_list_projection(client, patch_build_model, create_session):
    patch_build_model(script(text="ok"))
    sid = _sid(create_session)

    # 老（未打标）会话：列表透出 label_icon=null / labels=[]
    rows = client.get("/api/sessions").json()
    row = next(r for r in rows if r["id"] == sid)
    assert row["label_icon"] is None
    assert row["labels"] == []

    # 合法写入：icon + 去重 + strip
    r = client.put(
        f"/api/sessions/{sid}/tags",
        json={"icon": "wrench", "labels": [" release ", "release", "v0.1.2"]},
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    assert r.json()["session"]["label_icon"] == "wrench"
    assert r.json()["session"]["labels"] == ["release", "v0.1.2"]

    # 列表/get/搜索三面都透出
    row = next(x for x in client.get("/api/sessions").json() if x["id"] == sid)
    assert row["label_icon"] == "wrench" and row["labels"] == ["release", "v0.1.2"]
    assert client.get(f"/api/sessions/{sid}").json()["labels"] == ["release", "v0.1.2"]
    sr = client.get("/api/sessions/search", params={"q": sid[:6]}).json()
    hit = next(x for x in sr["sessions"] if x["id"] == sid)
    assert hit["label_icon"] == "wrench"

    # meta 键：tag_icon（agent 图标快照的 icon 键不被覆盖）
    index = json.loads(paths.session_index_path("default").read_text())
    m = next(x for x in index if x["id"] == sid)
    assert m["tag_icon"] == "wrench" and "icon" in m  # icon 仍是 agent 图标
    assert m["labels"] == ["release", "v0.1.2"]

    # 打标不 bump updated（touch=False，不打乱侧栏排序）
    before = row["updated"]
    r = client.put(f"/api/sessions/{sid}/tags", json={"icon": "bug", "labels": []})
    assert r.status_code == 200
    row = next(x for x in client.get("/api/sessions").json() if x["id"] == sid)
    assert row["updated"] == before

    # 清除：null + [] → 键移除、透出回缺省
    r = client.put(f"/api/sessions/{sid}/tags", json={"icon": None, "labels": []})
    assert r.status_code == 200
    row = next(x for x in client.get("/api/sessions").json() if x["id"] == sid)
    assert row["label_icon"] is None and row["labels"] == []
    index = json.loads(paths.session_index_path("default").read_text())
    m = next(x for x in index if x["id"] == sid)
    assert "tag_icon" not in m and m["labels"] == []


def test_tags_validation_matrix(client, patch_build_model, create_session):
    patch_build_model(script(text="ok"))
    sid = _sid(create_session)

    def put(body):
        return client.put(f"/api/sessions/{sid}/tags", json=body)

    # 非法 icon
    assert put({"icon": "sparkles", "labels": []}).status_code == 422
    assert put({"icon": 42, "labels": []}).status_code == 422
    # 超 3 个
    assert (
        put({"icon": "star", "labels": ["a", "b", "c", "d"]}).status_code == 422
    )
    # 单个超 20 字符 / strip 后为空
    assert put({"icon": "star", "labels": ["x" * 21]}).status_code == 422
    assert put({"icon": "star", "labels": ["   "]}).status_code == 422
    # 非法 labels 类型
    assert put({"icon": "star", "labels": "release"}).status_code == 422
    assert put({"icon": "star", "labels": [1]}).status_code == 422
    # 422 带 message（body 本身即 detail，结构化 {ok:false,error}）
    body = put({"icon": "nope", "labels": []}).json()
    assert body["ok"] is False and "icon" in body["error"]

    # 8 个枚举值全部可写
    for ic in ("wrench", "bug", "doc", "release", "research", "chat", "warn", "star"):
        assert put({"icon": ic, "labels": ["x"]}).status_code == 200


def test_tags_unknown_session_404(client, patch_build_model, create_session):
    r = client.put(
        "/api/sessions/nonexistent-id/tags", json={"icon": "star", "labels": []}
    )
    assert r.status_code == 404
