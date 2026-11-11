"""Instincts P0 单测：捕获→模板规则→注入 闭环（docs/instincts-design.md §13 P0 验收）。"""

from __future__ import annotations

import json

import pytest

from ginno_runtime.instincts import (
    build_instinct_context,
    list_instincts,
    maybe_analyze,
    observe,
)
from ginno_runtime.instincts.store import observations_path


pytestmark = pytest.mark.unit


@pytest.fixture
def isolated_instincts(isolated_home, monkeypatch):
    """隔离的 instincts 目录（复用 isolated_home 的临时 GINNO_HOME）。"""
    monkeypatch.setattr(
        "ginno_runtime.instincts.types._read_settings", lambda: {}
    )
    return isolated_home


def _deny(n: int = 3, tool: str = "bash", args: str = "rm -rf build/") -> None:
    for _ in range(n):
        observe({
            "type": "tool_denied",
            "tool": tool,
            "args_repr": args,
            "static": False,
            "session": "s1",
            "project": "demo",
            "turn": "t1",
        })


async def test_capture_writes_sanitized_jsonl(isolated_instincts):
    observe({
        "type": "tool_denied",
        "tool": "bash",
        "args_repr": "curl -H 'Authorization: Bearer sk-abc123' http://x",
        "static": False,
        "session": "s1",
        "project": "demo",
        "turn": "t",
    })
    lines = observations_path().read_text().strip().splitlines()
    ev = json.loads(lines[-1])
    assert ev["type"] == "tool_denied"
    assert ev["tool"] == "bash"
    # 敏感模式必须被 sanitize（token 形态不落盘）。
    assert "sk-abc123" not in ev["args_repr"]


async def test_template_rule_after_three_denies(isolated_instincts):
    _deny(3)
    maybe_analyze()
    items = [i for i in list_instincts(project_slug="demo") if i.source == "permission-deny"]
    assert len(items) == 1
    inst = items[0]
    assert inst.status == "pending"
    assert inst.evidence["observed"] == 3
    assert inst.trigger.startswith("即将执行 bash")


async def test_two_denies_no_rule(isolated_instincts):
    """宁缺毋滥：3 次以下不生成（§13 P0 验收口径）。"""
    _deny(2)
    maybe_analyze()
    assert not [i for i in list_instincts(project_slug="demo") if i.source == "permission-deny"]


async def test_repeat_analysis_reinforces(isolated_instincts):
    _deny(3)
    maybe_analyze()
    first = [i for i in list_instincts(project_slug="demo")][0]
    assert first.confidence == 0.5
    # 再来 2 次（共 5 观察全量重算）→ 幂等映射：5 次 = 0.5+0.1*(5-3) = 0.7。
    _deny(2)
    maybe_analyze()
    again = [i for i in list_instincts(project_slug="demo")][0]
    assert again.evidence["observed"] == 5
    assert again.confidence == 0.7
    # 重复分析不重复加分（幂等）。
    maybe_analyze()
    once_more = [i for i in list_instincts(project_slug="demo")][0]
    assert once_more.confidence == 0.7


async def test_inject_filters_and_formats(isolated_instincts, monkeypatch):
    from ginno_runtime.instincts.store import save_instinct
    from ginno_runtime.instincts.types import Instinct

    # 门槛 0.7 之上的才注入；项目隔离：其它 project 的不注。
    save_instinct(Instinct(
        id="a", trigger="清理缓存时", behavior="先列出将删文件再确认",
        confidence=0.8, status="active", scope="project", project_slug="demo"))
    save_instinct(Instinct(
        id="b", trigger="无关", behavior="不该出现", confidence=0.8,
        status="active", scope="project", project_slug="other"))
    save_instinct(Instinct(
        id="c", trigger="低置信", behavior="不该出现", confidence=0.4,
        status="active", scope="project", project_slug="demo"))

    ctx = build_instinct_context("帮我 清理缓存", project_slug="demo")
    assert ctx is not None
    assert "清理缓存" in ctx
    assert "0.8" in ctx
    assert "不该出现" not in ctx
    # 高置信行为规则普适注入（方案 §8：排序按 relevance，门槛按 confidence）：
    # 与 query 无关的 turn 也注入（行为规则如「测试带 --extra docs」对任何
    # query 都适用）。零命中 = 没有合格条目：
    monkeypatch.setattr(
        "ginno_runtime.instincts.inject.load_instincts_config",
        lambda settings=None: __import__("ginno_runtime.instincts.types", fromlist=["InstinctsConfig"]).InstinctsConfig(inject_min_confidence=0.95),
    )
    assert build_instinct_context("帮我 清理缓存", project_slug="demo") is None


async def test_retry_recovered_observed(isolated_instincts):
    from ginno_runtime.instincts import recent_observations

    observe({"type": "retry_recovered", "retry_kind": "rate_limited",
             "attempts": 2, "tool": "llm", "args_repr": ""})
    ev = recent_observations(5)[-1]
    assert ev["type"] == "retry_recovered"
    assert ev["attempts"] == 2


async def test_decay_scan_pending_expiry_and_active_decay(isolated_instincts):
    from datetime import datetime, timedelta

    from ginno_runtime.instincts.capture import decay_scan
    from ginno_runtime.instincts.store import save_instinct
    from ginno_runtime.instincts.types import Instinct

    old = (datetime.now().astimezone() - timedelta(days=20)).isoformat(timespec="seconds")
    ancient = (datetime.now().astimezone() - timedelta(days=100)).isoformat(timespec="seconds")
    # pending 滞留 20 天 → 过期 pruned
    save_instinct(Instinct(id="p1", trigger="t", behavior="b", confidence=0.5,
                           status="pending", scope="project", project_slug="demo",
                           evidence={"observed": 3, "applied": 0, "uncorrected": 0,
                                     "corrected": 0, "last_seen": old}))
    # active 100 天未观察 0.6 → 降档 0.5（未到 0.3 不 pruned）
    save_instinct(Instinct(id="a1", trigger="t2", behavior="b2", confidence=0.6,
                           status="active", scope="project", project_slug="demo",
                           evidence={"observed": 5, "applied": 0, "uncorrected": 0,
                                     "corrected": 0, "last_seen": ancient}))
    n = decay_scan()
    assert n == 2
    from ginno_runtime.instincts import list_instincts
    m = {i.id: i for i in list_instincts()}
    assert m["p1"].status == "pruned"
    assert m["a1"].confidence == 0.5
    # 幂等：再扫一遍——a1 时间戳没变会再次降档？last_seen 未更新会重复衰减；
    # 设计口径是「超窗每次扫描降一档直至 pruned」——第二遍 0.4，仍幂等可控。
    decay_scan()
    m2 = {i.id: i for i in list_instincts()}
    assert m2["a1"].confidence == 0.4
