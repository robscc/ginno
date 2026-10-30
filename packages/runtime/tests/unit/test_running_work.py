"""running_work 聚合(退出前确认 /api/running 的事实源)单测。"""

from __future__ import annotations

from ginno_runtime import server_shared
from ginno_runtime.running_work import running_work_report
from ginno_runtime.session_meta import _session_meta_upsert
from ginno_runtime.tasks import store as task_store
from ginno_runtime.tasks.registry import reset_registry


class _FakeRegistry:
    """只实现 list()——聚合端唯一的 registry 消费面。"""

    def __init__(self, records: list[dict]) -> None:
        self._records = records

    def list(self) -> list[dict]:
        return list(self._records)


def _use_records(monkeypatch, records: list[dict]) -> None:
    monkeypatch.setattr(
        "ginno_runtime.running_work.get_registry", lambda: _FakeRegistry(records)
    )


def test_report_empty_when_idle(seeded_home, monkeypatch):
    _use_records(monkeypatch, [])
    rep = running_work_report()
    assert rep["ok"] is True
    assert rep["count"] == 0
    assert rep["sessions"] == []
    assert rep["tasks"] == []


def test_report_counts_running_turns_with_titles(seeded_home, monkeypatch):
    _session_meta_upsert(
        "default",
        {"id": "sess-a", "title": "AMS 迁移", "type": "main", "updated": 1.0},
    )
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-a", "turn-1")
    _use_records(monkeypatch, [])
    rep = running_work_report()
    assert rep["count"] == 1
    assert rep["sessions"] == [{"id": "sess-a", "title": "AMS 迁移"}]


def test_report_title_falls_back_empty_for_unknown_session(seeded_home, monkeypatch):
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-ghost", "turn-2")
    _use_records(monkeypatch, [])
    rep = running_work_report()
    assert rep["sessions"] == [{"id": "sess-ghost", "title": ""}]


def test_report_skips_detached_and_terminal_tasks(seeded_home, monkeypatch):
    _use_records(
        monkeypatch,
        [
            {
                "id": "t_1",
                "status": task_store.STATUS_RUNNING,
                "title": "dev server",
                "command": "npm run dev",
                "detached": False,
            },
            {
                "id": "t_2",
                "status": task_store.STATUS_RUNNING,
                "title": "长跑训练",
                "command": "train.sh",
                "detached": True,  # detached:sidecar 死后继续跑,不算被打断
            },
            {
                "id": "t_3",
                "status": task_store.STATUS_FINISHED,
                "title": "done",
                "command": "ls",
                "detached": False,
            },
        ],
    )
    rep = running_work_report()
    assert [t["id"] for t in rep["tasks"]] == ["t_1"]
    assert rep["count"] == 1


def test_report_counts_sessions_and_tasks_together(seeded_home, monkeypatch):
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-a", "turn-1")
    monkeypatch.setitem(server_shared._RUNNING_TURNS, "sess-b", "turn-2")
    _use_records(
        monkeypatch,
        [
            {
                "id": "t_1",
                "status": task_store.STATUS_RUNNING,
                "title": "build",
                "command": "make app",
                "detached": False,
            }
        ],
    )
    rep = running_work_report()
    assert rep["count"] == 3  # 2 个运行中会话 + 1 个后台命令


def teardown_function() -> None:
    # _FakeRegistry 不碰真单例,但保险起见隔离测试间的 registry 状态。
    reset_registry()
