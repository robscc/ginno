"""API tests for /api/tasks/counts —— 常驻徽标的轻量计数路径。

验证四条主线（2026-11-21 性能修复验收）：

1. **计数 ⇆ 明细一致**：临时数据下 counts 与 /api/tasks/overview 的 counts
   逐源相等（六源各造至少一条，含 parked turn / 预占委托槽 / 已删会话的
   plan 等易漂移口径）；
2. **状态变化/失效**：run 取消、plan 取消、goal 归档、调度禁用、turn 出
   _RUNNING_TURNS 后，下一请求计数即降——增量索引靠 mtime 失效，不需要
   写路径挂钩；
3. **重启重建**：清空索引（模拟重启冷启动）后再查，计数不变；
4. **读取次数**：monkeypatch tasks_counts._loads 统计真实 JSON 解析次数
   ——sig 未变的文件在后续轮询里零解析（全历史 JSON 解析不随文件数线性
   增长是本次修复的核心验收点）；并发请求全部 200 且计数一致。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from ginno_runtime import schedule_store, server_shared
from ginno_runtime.api import tasks_counts as counts_api
from ginno_runtime.goals import store as goal_store
from ginno_runtime.plans import store as plan_store
from ginno_runtime.session_meta import (
    _session_meta_list,
    _session_meta_rewrite_all,
    _session_meta_upsert,
)
from ginno_runtime.tools.external_agent import _ACTIVE_DELEGATIONS
from ginno_runtime.workflows import store as wf_store

pytestmark = pytest.mark.api

_NOW = time.time()


@pytest.fixture(autouse=True)
def _clean_global_state():
    """conftest 只清 _RUNNING_TURNS；parked/委托表/增量索引本文件自查自清。"""
    server_shared._PENDING_RESUME.clear()
    server_shared._PENDING_KIND.clear()
    _ACTIVE_DELEGATIONS.clear()
    counts_api.reset_counts_cache_for_tests()
    yield
    server_shared._PENDING_RESUME.clear()
    server_shared._PENDING_KIND.clear()
    _ACTIVE_DELEGATIONS.clear()
    counts_api.reset_counts_cache_for_tests()


# ---- seeding helpers（与 test_tasks_overview 同款形状） ---------------------


def _seed_session(sid: str, *, type: str = "", title: str | None = None, **kw) -> None:
    meta = {"id": sid, "title": title or sid, "type": type, "created": _NOW, "updated": _NOW}
    meta.update(kw)
    _session_meta_upsert("default", meta)


def _arm_turn(sid: str) -> None:
    server_shared._RUNNING_TURNS[sid] = "t-1"
    server_shared._RUNNING_TURNS_AT[sid] = _NOW


def _park_turn(sid: str, kind: str = "user_question") -> None:
    server_shared._PENDING_RESUME.add(sid)
    server_shared._PENDING_KIND[sid] = kind


def _seed_schedule_task(task_id: str = "st-1") -> str:
    schedule_store.save_config(
        {
            "enabled": True,
            "tasks": [
                {
                    "id": task_id,
                    "name": "每日趋势分析",
                    "enabled": True,
                    "schedule": {"kind": "interval", "minutes": 60},
                    "target": {"type": "prompt", "prompt": "hi"},
                    "next_run_at": _NOW + 600,
                }
            ],
        }
    )
    return task_id


def _seed_plan(session_id: str = "s-plan") -> str:
    _seed_session(session_id)
    plan = plan_store.create_plan(
        session_id,
        slug="default",
        title="重构批次 8",
        items=[{"goal": "a"}, {"goal": "b"}],
    )
    return plan["plan_id"]


def _seed_run(present_in: str = "s-run-host") -> str:
    _seed_session(present_in)
    run = wf_store.create_run(
        {"id": "wf-1", "name": "周报流水线", "dsl": {"steps": []}},
        session_id=present_in,
        present_in_session_id=present_in,
    )
    return run["id"]


def _seed_goal(sid: str = "s-goal") -> None:
    _seed_session(sid, title="发版窗口")
    goal_store.create_goal("default", sid, "完成发版")


def _seed_delegation(did: str = "did-1", *, with_proc: bool = True) -> None:
    _seed_session("s-parent", title="主会话")
    _seed_session(did, type="delegation", title="codex · 修复登录超时", parent_session_id="s-parent")
    _ACTIVE_DELEGATIONS[did] = {
        "session_id": "s-parent",
        "proc": object() if with_proc else None,  # None = 并发闸预占槽，非真实委托
    }


def _get(client, path: str) -> dict:
    r = client.get(path)
    assert r.status_code == 200, (path, r.status_code, r.text)
    return r.json()


# ---- 1. 形状与一致性 --------------------------------------------------------


def test_counts_empty_shape(client):
    data = _get(client, "/api/tasks/counts")
    assert data["ok"] is True
    counts = data["counts"]
    assert set(counts) == {
        "turns", "scheduled", "subagents", "runs", "goals", "delegations", "total",
    }
    assert all(v == 0 for v in counts.values())


def test_counts_match_overview_all_six_sources(client):
    """临时数据下 counts 与 overview 的 counts 逐源相等（计数 ⇆ 明细一致）。"""
    _seed_session("s-turn", title="ginno 开发区")
    _arm_turn("s-turn")
    _seed_schedule_task()
    _seed_plan()
    run_id = _seed_run()
    _seed_goal()
    _seed_delegation()

    counts = _get(client, "/api/tasks/counts")["counts"]
    overview = _get(client, "/api/tasks/overview")["counts"]
    assert counts == overview, (counts, overview)

    assert counts["turns"] == 1
    assert counts["scheduled"] == 1
    assert counts["subagents"] == 1
    assert counts["runs"] == 1
    assert counts["goals"] == 1
    assert counts["delegations"] == 1
    assert counts["total"] == 6
    assert run_id  # silence unused


def test_counts_parked_turn_counted_once(client):
    """同会话既 running 又 parked：overview 去重为 1，counts 必须同为 1。"""
    _seed_session("s-both")
    _arm_turn("s-both")
    _park_turn("s-both")
    counts = _get(client, "/api/tasks/counts")["counts"]
    overview = _get(client, "/api/tasks/overview")["counts"]
    assert counts["turns"] == 1 == overview["turns"]


def test_counts_exclude_preoccupied_delegation_slot(client):
    """proc=None 的并发闸预占槽不是真实委托，不计数（overview 同口径）。"""
    _seed_delegation("did-slot", with_proc=False)
    counts = _get(client, "/api/tasks/counts")["counts"]
    overview = _get(client, "/api/tasks/overview")["counts"]
    assert counts["delegations"] == 0 == overview["delegations"]


def test_counts_exclude_deleted_session_plans(client):
    """会话已删的 open plan 不枚举（overview 的 _find_meta 过滤）→ 计数也不算。
    同时锻炼 session 索引的 mtime 失效：删会话 = 重写 _index.json。"""
    plan_id = _seed_plan("s-gone")
    assert _get(client, "/api/tasks/counts")["counts"]["subagents"] == 1
    _session_meta_rewrite_all("default", [])
    assert _session_meta_list("default") == []
    counts = _get(client, "/api/tasks/counts")["counts"]
    overview = _get(client, "/api/tasks/overview")["counts"]
    assert counts["subagents"] == 0 == overview["subagents"]
    assert plan_id  # silence unused


# ---- 2. 状态变化 / 失效 -----------------------------------------------------


def test_counts_track_run_cancel(client):
    run_id = _seed_run()
    assert _get(client, "/api/tasks/counts")["counts"]["runs"] == 1
    r = client.post(f"/api/workflow_runs/{run_id}/cancel")
    assert r.status_code == 200, r.text
    counts = _get(client, "/api/tasks/counts")["counts"]
    assert counts["runs"] == 0
    overview = _get(client, "/api/tasks/overview")["counts"]
    assert overview["runs"] == 0


def test_counts_track_plan_cancel(client):
    plan_id = _seed_plan()
    assert _get(client, "/api/tasks/counts")["counts"]["subagents"] == 1
    r = client.post(f"/api/sessions/s-plan/plans/{plan_id}/cancel")
    assert r.status_code == 200, r.text
    assert _get(client, "/api/tasks/counts")["counts"]["subagents"] == 0


def test_counts_track_schedule_disable(client):
    task_id = _seed_schedule_task()
    assert _get(client, "/api/tasks/counts")["counts"]["scheduled"] == 1
    r = client.patch(f"/api/schedule/tasks/{task_id}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert _get(client, "/api/tasks/counts")["counts"]["scheduled"] == 0


def test_counts_track_goal_archive(client):
    _seed_goal()
    assert _get(client, "/api/tasks/counts")["counts"]["goals"] == 1
    goal_store.update_status("default", "s-goal", goal_store.STATUS_COMPLETE)
    assert _get(client, "/api/tasks/counts")["counts"]["goals"] == 0


def test_counts_track_turn_and_delegation_memory_state(client):
    """内存源不走磁盘索引：字典快照变化下一请求立即生效。"""
    _seed_session("s-turn")
    _arm_turn("s-turn")
    _seed_delegation()
    assert _get(client, "/api/tasks/counts")["counts"]["total"] == 2
    server_shared._RUNNING_TURNS.pop("s-turn")
    _ACTIVE_DELEGATIONS.clear()
    counts = _get(client, "/api/tasks/counts")["counts"]
    assert counts["turns"] == 0 and counts["delegations"] == 0 and counts["total"] == 0


# ---- 3. 重启重建 ------------------------------------------------------------


def test_counts_rebuild_after_restart(client):
    """清空增量索引（模拟重启）后计数不变：首请求全量重建一次。"""
    _seed_plan()
    _seed_run()
    _seed_goal()
    warm = _get(client, "/api/tasks/counts")["counts"]
    counts_api.reset_counts_cache_for_tests()
    rebuilt = _get(client, "/api/tasks/counts")["counts"]
    assert rebuilt == warm
    assert rebuilt["subagents"] == 1 and rebuilt["runs"] == 1 and rebuilt["goals"] == 1


# ---- 4. 读取次数（增量索引不重解析未变文件） --------------------------------


class _ParseCounter:
    """包一层 tasks_counts._loads 统计真实解析次数（按文件类型分桶）。"""

    MARKERS = {"runs": '"workflow_id"', "plans": '"plan_id"', "goals": '"goal_id"'}

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.total = 0
        self.by_kind = {k: 0 for k in self.MARKERS}
        real = counts_api._loads

        def counting(text: str) -> object:
            self.total += 1
            for kind, marker in self.MARKERS.items():
                if marker in text:
                    self.by_kind[kind] += 1
                    break
            return real(text)

        monkeypatch.setattr(counts_api, "_loads", counting)


def test_incremental_refresh_does_not_reparse_unchanged(client, monkeypatch):
    """3 个 run：冷启动解析 3 次 → 温请求 0 次 → 取消 1 个只解析 1 个文件。
    历史 run 增多后周期轮询的解析次数只随「变化文件数」走，不随全表走。"""
    counter = _ParseCounter(monkeypatch)
    for _ in range(3):
        _seed_run()
    assert _get(client, "/api/tasks/counts")["counts"]["runs"] == 3
    assert counter.by_kind["runs"] == 3, counter.by_kind  # 冷启动全量

    before = counter.by_kind["runs"]
    assert _get(client, "/api/tasks/counts")["counts"]["runs"] == 3
    assert counter.by_kind["runs"] == before, "sig 未变的文件被重新解析了"

    fourth = _seed_run()
    assert _get(client, "/api/tasks/counts")["counts"]["runs"] == 4
    assert counter.by_kind["runs"] == before + 1, "新文件应触发一次解析"
    r = client.post(f"/api/workflow_runs/{fourth}/cancel")
    assert r.status_code == 200
    before = counter.by_kind["runs"]
    assert _get(client, "/api/tasks/counts")["counts"]["runs"] == 3
    assert counter.by_kind["runs"] == before + 1, "状态变化应只重解析被重写的那个文件"


def test_plan_index_skips_unchanged_plan_files(client, monkeypatch):
    counter = _ParseCounter(monkeypatch)
    _seed_plan("s-a")
    _seed_plan("s-b")
    assert _get(client, "/api/tasks/counts")["counts"]["subagents"] == 2
    assert counter.by_kind["plans"] == 2
    before = counter.by_kind["plans"]
    assert _get(client, "/api/tasks/counts")["counts"]["subagents"] == 2
    assert counter.by_kind["plans"] == before, "plan 未变却被重新解析"


# ---- 5. 并发响应 -------------------------------------------------------------


def test_concurrent_counts_and_overview_consistent(client):
    """并发打两个端点：全部 200，且 counts 与 overview 计数逐请求一致。"""
    _seed_session("s-turn")
    _arm_turn("s-turn")
    _seed_schedule_task()
    _seed_plan()
    _seed_run()
    _seed_goal()
    _seed_delegation()

    def hit(i: int) -> dict:
        path = "/api/tasks/counts" if i % 2 == 0 else "/api/tasks/overview"
        return _get(client, path)["counts"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(hit, range(12)))
    assert len(results) == 12
    assert all(r == results[0] for r in results), "并发请求计数不一致"
    assert results[0]["total"] == 6
