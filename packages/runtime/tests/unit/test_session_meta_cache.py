"""_session_meta_list 读缓存（P2 会话索引缓存）回归。

覆盖：重复读 parse 次数下降、外部原子替换/就地改写失效、内部写路径
（patch/upsert/rewrite）后读到最新、损坏与缺失文件的确定性、多 home
隔离、可变字典不污染缓存。
"""

from __future__ import annotations

import json as stdjson
import os
import types

import ginno_runtime.session_meta as sm


def _use_home(monkeypatch, tmp_path, slug="default"):
    monkeypatch.setattr(sm.paths, "home", lambda: tmp_path)
    proj = tmp_path / "projects" / slug / "sessions"
    proj.mkdir(parents=True, exist_ok=True)
    return proj / "_index.json"


def _counting_loads(monkeypatch):
    """替换 session_meta 内的 json 模块引用，仅计数 loads 调用。"""
    calls = {"n": 0}
    real_loads = sm.json.loads

    def loads(data):
        calls["n"] += 1
        return real_loads(data)

    shim = types.SimpleNamespace(
        loads=loads, dumps=sm.json.dumps, JSONDecodeError=sm.json.JSONDecodeError
    )
    monkeypatch.setattr(sm, "json", shim)
    return calls


def test_repeated_reads_parse_once(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    p.write_text(stdjson.dumps([{"id": "a"}, {"id": "b"}]))
    calls = _counting_loads(monkeypatch)
    for _ in range(10):
        assert [m["id"] for m in sm._session_meta_list("default")] == ["a", "b"]
    assert calls["n"] == 1  # 10 次读只 parse 一次


def test_external_atomic_replace_invalidates(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    p.write_text(stdjson.dumps([{"id": "a"}]))
    assert [m["id"] for m in sm._session_meta_list("default")] == ["a"]
    # 外部原子替换（tmp+rename，与 _write_index 同形态）
    tmp = p.with_name(p.name + ".ext")
    tmp.write_text(stdjson.dumps([{"id": "a"}, {"id": "b"}]))
    os.replace(tmp, p)
    assert [m["id"] for m in sm._session_meta_list("default")] == ["a", "b"]


def test_external_inplace_write_invalidates(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    p.write_text(stdjson.dumps([{"id": "a"}]))
    assert [m["id"] for m in sm._session_meta_list("default")] == ["a"]
    p.write_text(stdjson.dumps([{"id": "c"}]))
    assert [m["id"] for m in sm._session_meta_list("default")] == ["c"]


def test_internal_patch_then_read(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    sm._session_meta_upsert("default", {"id": "s1", "title": "t"})
    got = sm._session_meta_patch("default", "s1", {"title": "t2"})
    assert got is not None and got["title"] == "t2"
    again = sm._session_meta_list("default")
    assert again[0]["title"] == "t2" and again[0]["id"] == "s1"


def test_rewrite_all_then_read(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    p.write_text(stdjson.dumps([{"id": "old"}]))
    sm._session_meta_list("default")
    sm._session_meta_rewrite_all("default", [{"id": "new"}])
    assert [m["id"] for m in sm._session_meta_list("default")] == ["new"]


def test_corrupt_file_returns_empty_and_recovers(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    p.write_text("{not json")
    assert sm._session_meta_list("default") == []
    # 损坏不缓存：反复读仍确定性为空
    assert sm._session_meta_list("default") == []
    # heal 落盘后恢复
    sm._session_meta_rewrite_all("default", [{"id": "ok"}])
    assert [m["id"] for m in sm._session_meta_list("default")] == ["ok"]


def test_missing_file_returns_empty(monkeypatch, tmp_path):
    _use_home(monkeypatch, tmp_path)
    assert sm._session_meta_list("default") == []
    assert sm._session_meta_list("nope") == []


def test_multiple_homes_isolated(monkeypatch, tmp_path):
    home_a = tmp_path / "a"
    home_b = tmp_path / "b"
    for h, ids in ((home_a, ["x"]), (home_b, ["y", "z"])):
        proj = h / "projects" / "default" / "sessions"
        proj.mkdir(parents=True)
        (proj / "_index.json").write_text(stdjson.dumps([{"id": i} for i in ids]))
    monkeypatch.setattr(sm.paths, "home", lambda: home_a)
    assert [m["id"] for m in sm._session_meta_list("default")] == ["x"]
    monkeypatch.setattr(sm.paths, "home", lambda: home_b)
    assert [m["id"] for m in sm._session_meta_list("default")] == ["y", "z"]
    monkeypatch.setattr(sm.paths, "home", lambda: home_a)
    assert [m["id"] for m in sm._session_meta_list("default")] == ["x"]


def test_returned_copies_do_not_pollute_cache(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    p.write_text(stdjson.dumps([{"id": "s1", "labels": ["a"]}]))
    first = sm._session_meta_list("default")
    # 顶 层改写 + 嵌套列表追加都不得影响后续读
    first[0]["title"] = "polluted"
    first[0]["labels"].append("polluted")
    second = sm._session_meta_list("default")
    assert "title" not in second[0]
    assert second[0]["labels"] == ["a"]


def test_patch_does_not_leak_mutations(monkeypatch, tmp_path):
    p = _use_home(monkeypatch, tmp_path)
    sm._session_meta_upsert("default", {"id": "s1", "labels": ["keep"]})
    got = sm._session_meta_patch("default", "s1", {"title": "x"})
    got["labels"].append("leak")
    reread = sm._session_meta_list("default")
    assert reread[0]["labels"] == ["keep"]
    assert reread[0]["title"] == "x"
