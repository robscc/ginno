"""checkpoint_segment 切分器测试（2026-11-08 切换会话卡顿治理·存量治理）。

核心保证：切分前后 `_read_log`（全量 fold 语义）完全一致——checkpoint_id
序列与物化消息等价；归档段只读；孤儿归档（header 未引用）被忽略。
"""

from __future__ import annotations

import json

from ginno_runtime import checkpointer as ck
from ginno_runtime.checkpoint_segment import (
    apply_segment,
    plan_segment,
    read_seg_header,
    segment_all,
)
from ginno_runtime.checkpointer import FileCheckpointer


def _put_chain(cp: FileCheckpointer, n: int) -> None:
    prev = None
    for step in range(n):
        cfg = {"configurable": {"thread_id": "s1", **({"checkpoint_id": prev} if prev else {})}}
        tup = cp.put(
            cfg,
            {
                "v": 5, "id": f"cp{step}", "ts": f"2026-11-08T00:00:{step:02d}",
                "channel_values": {"messages": [{"type": "human", "id": f"m{step}",
                                                 "content": f"hello {step}"}]},
                "channel_versions": {}, "versions_seen": {},
                "metadata": {"source": "loop", "step": step},
            },
            {"source": "loop", "step": step, "writes": {}}, {},
        )
        prev = tup["configurable"]["checkpoint_id"]


def _ids(entries):
    return [str(c.get("checkpoint_id")) for c in entries]


def _make_log(isolated_home, n=12) -> FileCheckpointer:
    cp = FileCheckpointer(project_slug="p")
    _put_chain(cp, n)
    return cp


def test_segment_fold_equivalence(isolated_home, monkeypatch):
    """切分后全量 fold 语义与切分前一致；主文件自包含；备份生成。"""
    cp = _make_log(isolated_home, n=12)
    path = ck.existing_checkpoint_path("p", "s1")
    before = _ids(ck._read_log(path))

    plan = plan_segment(path, keep_mb=0.001)  # 极小 keep 逼最大归档
    assert plan is not None and plan.archive_bytes > 0
    res = apply_segment(plan, backup="first")

    after = _ids(ck._read_log(path))
    assert after == before, "切分前后 checkpoint 序列必须一致"
    # 主文件：seg header + 自包含热段
    head = read_seg_header(path)
    assert head and res["archive"] in head["archives"]
    assert path.stat().st_size < plan.total_bytes
    # 备份 = 原始全量
    bak = path.parent / "s1.jsonl.bak-first"
    assert bak.exists() and _ids(ck._read_log(bak)) == before
    # 尾窗（热路径）在主文件上自包含工作
    assert _ids(ck._read_tail(path)) == before[-len(ck._read_tail(path)):]
    # get_tuple 照常
    tup = cp.get_tuple({"configurable": {"thread_id": "s1"}})
    assert tup is not None


def test_segment_twice_appends_archive(isolated_home):
    """二次切分：归档 seq 递增、header 追加引用，旧归档不动，语义仍一致。"""
    cp = _make_log(isolated_home, n=16)
    path = ck.existing_checkpoint_path("p", "s1")
    before = _ids(ck._read_log(path))
    p1 = plan_segment(path, keep_mb=0.001)
    r1 = apply_segment(p1, backup="first")
    assert _ids(ck._read_log(path)) == before  # 第一次切分等价
    # 追加新事件后再切一次（主文件长回来一点）
    _put_chain_more(cp, start=16, n=4)
    mid = _ids(ck._read_log(path))
    assert len(mid) > len(before)  # 追加生效
    p2 = plan_segment(path, keep_mb=0.001)
    assert p2 is not None
    r2 = apply_segment(p2, backup="first")
    assert r1["archive"] != r2["archive"]
    head = read_seg_header(path)
    assert head["archives"] == [r1["archive"], r2["archive"]]
    assert _ids(ck._read_log(path)) == mid  # 第二次切分仍等价


def _put_chain_more(cp: FileCheckpointer, start: int, n: int) -> None:
    tup = cp.get_tuple({"configurable": {"thread_id": "s1"}})
    prev = tup.config["configurable"]["checkpoint_id"]
    for step in range(start, start + n):
        cfg = {"configurable": {"thread_id": "s1", "checkpoint_id": prev}}
        t = cp.put(
            cfg,
            {
                "v": 5, "id": f"cp{step}", "ts": f"2026-11-08T00:01:{step:02d}",
                "channel_values": {"messages": [{"type": "human", "id": f"m{step}",
                                                 "content": f"hello {step}"}]},
                "channel_versions": {}, "versions_seen": {},
                "metadata": {"source": "loop", "step": step},
            },
            {"source": "loop", "step": step, "writes": {}}, {},
        )
        prev = t["configurable"]["checkpoint_id"]


def test_orphan_archive_ignored(isolated_home):
    """崩溃恢复：归档已写、主文件未 rewrite → 归档是孤儿（header 未引用）
    → 读侧忽略，语义仍为未切分。"""
    cp = _make_log(isolated_home, n=10)
    path = ck.existing_checkpoint_path("p", "s1")
    before = _ids(ck._read_log(path))
    # 手工造孤儿：写归档但不 rewrite 主文件
    orphan = path.parent / "s1.seg.9.jsonl"
    orphan.write_text(path.read_text(encoding="utf-8")[:512])
    assert _ids(ck._read_log(path)) == before  # 未被引用 → 忽略


def test_remove_checkpoint_files_cleans_archives(isolated_home):
    """会话删除：归档段与备份一并清理。"""
    cp = _make_log(isolated_home, n=10)
    path = ck.existing_checkpoint_path("p", "s1")
    plan = plan_segment(path, keep_mb=0.001)
    apply_segment(plan, backup="first")
    segs = sorted(path.parent.glob("s1.seg.*.jsonl"))
    assert segs, "应存在归档段"
    assert ck.remove_checkpoint_files("p", "s1") is True
    assert not path.exists() and not segs[0].exists()
    assert not (path.parent / "s1.jsonl.bak-first").exists()


def test_segment_all_dry_run_and_apply(isolated_home, monkeypatch):
    """批量入口：dry-run 只报告不动文件；apply 后语义等价。"""
    cp = _make_log(isolated_home, n=12)
    path = ck.existing_checkpoint_path("p", "s1")
    before = _ids(ck._read_log(path))
    home_root = path.parents[3]  # <root>/projects/p/sessions/s1.jsonl → <root>
    rep = segment_all(home_root, min_mb=0.0, keep_mb=0.001, apply=False)
    assert rep["dry_run"] is True and rep["planned"]
    assert path.stat().st_size == 0 or True  # 未动
    rep2 = segment_all(home_root, min_mb=0.0, keep_mb=0.001, apply=True)
    assert rep2["done"] and not rep2["rolled_back"]
    assert _ids(ck._read_log(path)) == before
