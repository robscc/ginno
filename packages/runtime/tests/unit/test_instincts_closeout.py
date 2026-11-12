# -*- coding: utf-8 -*-
"""Instincts P1 收尾测试（2026-11-12）：冲突检测 / 归因 / export-import。"""

from ginno_runtime.instincts import capture, store
from ginno_runtime.instincts.types import Instinct


def _mk(iid, trigger, behavior, conf=0.8, status="active", slug="t-proj"):
    return store.save_instinct(Instinct(
        id=iid, trigger=trigger, behavior=behavior, confidence=conf,
        domain="tool-usage", scope="project", project_slug=slug,
        source="manual", status=status,
        evidence={"observed": 3, "applied": 0, "uncorrected": 0, "corrected": 0, "last_seen": ""},
    ))


def test_conflict_detection_pos_neg_pair(isolated_home):
    _mk("c1", "run rm command cleanup build", "never run rm -rf")
    _mk("c2", "run rm command cleanup dist", "should use rm -rf directly")
    _mk("c3", "git commit style", "always use conventional commits")
    pairs = store.find_conflicts(slug := "t-proj")
    assert len(pairs) == 1, pairs
    assert {pairs[0]["a"], pairs[0]["b"]} == {"c1", "c2"}


def test_conflict_freeze_and_resolve(isolated_home):
    _mk("f1", "run rm command cleanup build", "never run rm -rf")
    _mk("f2", "run rm command cleanup dist", "should use rm -rf directly")
    pairs = store.find_conflicts("t-proj")
    n = store.mark_conflict([pairs[0]["a"], pairs[0]["b"]])
    assert n == 2
    # 冻结后不再出现在 active（inject 天然排除）
    assert all(i.status == "conflict" for i in store.list_instincts(status="conflict"))
    assert store.find_conflicts("t-proj") == []
    # 裁决：保 f1 弃 f2
    assert store.resolve_conflict("f1", "f2")
    act = {i.id for i in store.list_instincts(status="active")}
    pruned = {i.id for i in store.list_instincts(status="pruned")}
    assert "f1" in act and "f2" in pruned


def test_attribution_no_correction(isolated_home):
    _mk("a1", "即将执行 rm（参数形如 build 目录类）时", "先列出内容确认再执行", conf=0.8)
    n = capture.note_injection_feedback("执行 rm 清理 build", "t-proj", had_correction=False)
    assert n >= 1
    inst = [i for i in store.list_instincts(status="active") if i.id == "a1"][0]
    assert inst.evidence["applied"] == 1
    assert inst.evidence["uncorrected"] == 1


def test_attribution_correction_downgrades(isolated_home):
    _mk("b1", "即将执行 rm（参数形如 build 目录类）时", "先列出内容确认再执行 rm build", conf=0.9)
    n = capture.note_injection_feedback("不要 rm build，改用 mv", "t-proj", had_correction=True)
    assert n == 1
    inst = [i for i in store.list_instincts(status="active") if i.id == "b1"][0]
    assert inst.evidence["corrected"] == 1
    assert inst.confidence == 0.7  # -0.2 降档


def test_export_import_roundtrip(isolated_home):
    from ginno_runtime.api.instincts import api_export, api_import, get_instinct_by_id
    _mk("e1", "t1", "b1", conf=0.5, status="pending")
    dump = api_export()
    assert dump["version"] == 1 and any(i["id"] == "e1" for i in dump["instincts"])
    # 空库再导入 → added
    import shutil
    shutil.rmtree(store.instincts_root(), ignore_errors=True)
    r = api_import(dump)
    assert r["added"] >= 1 and get_instinct_by_id("e1") is not None
    # 重复导入（updated 不更新）→ skipped
    r2 = api_import(dump)
    assert r2["added"] == 0
