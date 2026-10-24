"""Startup hygiene (maintenance.py) — the app-iter-harness ticket batch.

Each test seeds a tiny project index and asserts one migration: missing
workspace fallback, agent backfill, stale-error expiry, duplicate-title
suffixing, orphan-artifact pruning, and title-job re-queue.
"""

from __future__ import annotations

import json
import os
import time

import pytest

from ginno_runtime import artifacts as art_store
from ginno_runtime import maintenance, paths
from ginno_runtime.session_meta import _session_meta_list, _session_meta_rewrite_all

pytestmark = pytest.mark.unit

DAY = 86400


def _write_index(slug: str, rows: list[dict]) -> None:
    paths.project_sessions_dir(slug).mkdir(parents=True, exist_ok=True)
    _session_meta_rewrite_all(slug, rows)


def _row(sid: str, **over) -> dict:
    base = {
        "id": sid,
        "title": f"t-{sid}",
        "agent_id": "dev",
        "workspace": str(paths.session_files_dir("default", sid)),
        "created": time.time(),
        "updated": time.time(),
    }
    base.update(over)
    return base


def test_heal_workspace_agent_error_and_titles(isolated_home, monkeypatch):
    monkeypatch.setattr(maintenance, "_has_messages", lambda slug, sid: True)
    now = time.time()
    stale = now - 30 * DAY
    rows = [
        _row("badws", workspace="/tmp/definitely-not-there-xyz"),
        _row("noagent", agent_id=""),
        _row("staleerr", updated=stale,
             last_error={"message": "boom", "at": stale}),
        _row("dup1", title="⏰ 每日总结", created=now - 2 * DAY),
        _row("dup2", title="⏰ 每日总结", created=now - DAY),
        _row("dup3", title="⏰ 每日总结", created=now - DAY),
        _row("solo", title="unique"),
    ]
    _write_index("default", rows)

    maintenance.run_startup_maintenance()
    out = {m["id"]: m for m in _session_meta_list("default")}

    assert os.path.isdir(out["badws"]["workspace"])
    assert out["noagent"]["agent_id"]  # first registered agent
    assert not out["staleerr"]["last_error"]
    titles = [out[k]["title"] for k in ("dup1", "dup2", "dup3")]
    assert len(set(titles)) == 3 and all(t.startswith("⏰ 每日总结 · ") for t in titles)
    assert out["solo"]["title"] == "unique"

    # Idempotent: a second pass writes nothing new.
    before = paths.session_index_path("default").read_text()
    maintenance.run_startup_maintenance()
    assert paths.session_index_path("default").read_text() == before


def test_prune_orphan_artifacts(isolated_home, tmp_path, monkeypatch):
    monkeypatch.setattr(maintenance, "_has_messages", lambda slug, sid: True)
    _write_index("default", [_row("s1")])
    gone = tmp_path / "gone.md"
    ok = tmp_path / "ok.md"
    ok.write_text("x", encoding="utf-8")
    art_store.add_artifact("default", "file", "ok", str(ok), "s1")
    art_store.add_artifact("default", "file", "gone", str(gone), "s1")
    art_store.add_artifact("default", "doc", "orphan-doc", "/v/doc.md", "deleted-session")

    maintenance.run_startup_maintenance()
    names = {a["name"] for a in art_store.list_artifacts("default")}
    assert names == {"ok"}


def test_requeue_pending_titles_backfills_assistant(isolated_home, monkeypatch):
    monkeypatch.setattr(maintenance, "_has_messages", lambda slug, sid: True)
    old = time.time() - 3 * DAY
    _write_index(
        "default",
        [
            _row(
                "pend",
                created=old,
                title_llm_pending=True,
                title_seed="build a harness",
                title_assistant="",
            ),
            _row("fresh", created=time.time(), title_llm_pending=True, title_seed="x"),
        ],
    )
    spawned: list[str] = []
    import ginno_runtime.title_gen as tg

    monkeypatch.setattr(tg, "spawn_title_gen", lambda sid, tid: spawned.append(sid))
    monkeypatch.setattr(
        maintenance, "_first_assistant_text", lambda slug, sid: "the assistant reply"
    )

    import asyncio

    n = asyncio.run(maintenance.requeue_pending_titles())
    assert n == 1 and spawned == ["pend"]
    meta = {m["id"]: m for m in _session_meta_list("default")}
    assert meta["pend"]["title_assistant"] == "the assistant reply"
    assert meta["fresh"]["title_llm_pending"] is True  # untouched inside the window


def test_stuck_title_gives_up_after_cutoff(isolated_home):
    """A pending title older than the cutoff that cannot be produced (dead
    provider key) must stop being re-queued on every launch (UX-03)."""
    import time as _t

    from ginno_runtime import title_gen

    old = _t.time() - 3 * DAY
    _write_index(
        "default",
        [
            _row("stuck", created=old, title_llm_pending=True, title_seed="x"),
            _row("fresh", created=_t.time(), title_llm_pending=True, title_seed="y"),
        ],
    )
    title_gen._give_up_if_stuck("stuck")
    title_gen._give_up_if_stuck("fresh")
    meta = {m["id"]: m for m in _session_meta_list("default")}
    assert meta["stuck"]["title_llm_pending"] is False
    assert meta["fresh"]["title_llm_pending"] is True


def test_stale_empty_sessions_purged_once(isolated_home, monkeypatch):
    """DATA-05: zero-message sessions older than a day are deleted ONCE (the
    product decision), not by a standing rule that could eat a draft."""
    old = time.time() - 3 * DAY
    _write_index(
        "default",
        [
            _row("empty_old", created=old),
            _row("empty_new", created=time.time() - 3600),
            _row("has_msgs", created=old),
        ],
    )
    monkeypatch.setattr(
        maintenance, "_has_messages", lambda slug, sid: sid == "has_msgs"
    )
    maintenance.run_startup_maintenance()
    ids = {m["id"] for m in _session_meta_list("default")}
    assert ids == {"empty_new", "has_msgs"}
    assert (paths.home() / "maintenance.json").exists()

    # A later empty session is NOT touched — the purge is one-shot.
    _write_index("default", _session_meta_list("default") + [_row("later", created=old)])
    maintenance.run_startup_maintenance()
    assert "later" in {m["id"] for m in _session_meta_list("default")}


def test_requeue_gives_up_when_no_assistant_text(isolated_home, monkeypatch):
    """A stuck session whose history has no assistant prose (only tool calls)
    cannot be titled; it must stop being flagged instead of pending forever."""
    old = time.time() - 3 * DAY
    _write_index(
        "default",
        [_row("toolonly", created=old, title_llm_pending=True, title_seed="x")],
    )
    monkeypatch.setattr(maintenance, "_has_messages", lambda slug, sid: True)
    monkeypatch.setattr(maintenance, "_first_assistant_text", lambda slug, sid: "")
    import asyncio

    assert asyncio.run(maintenance.requeue_pending_titles()) == 0
    meta = {m["id"]: m for m in _session_meta_list("default")}
    assert meta["toolonly"]["title_llm_pending"] is False


def test_image_and_doc_artifacts_with_missing_files_are_pruned(isolated_home, tmp_path, monkeypatch):
    """DATA-07: the sweep must cover image/doc refs, not just kind=file."""
    _write_index("default", [_row("s1")])
    monkeypatch.setattr(maintenance, "_has_messages", lambda slug, sid: True)
    gone = tmp_path / "missing.png"
    ok = tmp_path / "ok.png"
    ok.write_text("x", encoding="utf-8")
    art_store.add_artifact("default", "image", "gone.png", str(gone), "s1")
    art_store.add_artifact("default", "image", "ok.png", str(ok), "s1")
    # URLs and workflow ids must survive
    art_store.add_artifact("default", "doc", "remote", "https://example.com/doc", "s1")
    art_store.add_artifact("default", "workflow", "run", "wf-run-id", "s1")

    maintenance.run_startup_maintenance()
    names = {a["name"] for a in art_store.list_artifacts("default")}
    assert names == {"ok.png", "remote", "run"}


def test_dangling_team_and_run_refs_are_healed(isolated_home, monkeypatch):
    """DATA-06: references to deleted sessions get cleared (team) / dropped
    (schedule run) instead of silently pointing at nothing."""
    import json as _json

    from ginno_runtime import paths as paths_mod
    from ginno_runtime import schedule_store
    from ginno_runtime.teams import store as team_store

    _write_index("default", [_row("alive")])
    monkeypatch.setattr(maintenance, "_has_messages", lambda slug, sid: True)

    t = team_store.create_team(
        project_slug="default", name="Dead", main_session_id="gone-main"
    )
    team_store.add_member(t["id"], "dev", "gone-member", display_name="甲")
    t2 = team_store.create_team(
        project_slug="default", name="Mixed", main_session_id="alive"
    )
    team_store.add_member(t2["id"], "dev", "gone-member-2", display_name="乙")

    day = _t_str = __import__("time").strftime("%Y-%m-%d")
    runs_dir = paths_mod.schedule_runs_dir()
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / f"runs-{day}.jsonl").write_text(
        _json.dumps({"run_id": "sr-1", "session_id": "gone-main"}) + "\n",
        encoding="utf-8",
    )

    maintenance.run_startup_maintenance()

    assert team_store.get_team(t["id"]) is None          # all sessions gone
    mixed = team_store.get_team(t2["id"])
    assert mixed["main_session_id"] == "alive"
    members = mixed.get("members") or []
    assert [m.get("session_ids") for m in members] == []  # dead member dropped

    row = schedule_store.load_day(day)[0]
    assert row["session_id"] is None and row["session_missing"] is True


def test_archive_marks_old_near_empty_sessions(isolated_home, monkeypatch):
    """P1-1: inactivity + almost no messages → archived; a real conversation,
    a pinned session or a referenced one stays visible."""
    import json as _json

    from ginno_runtime import paths as paths_mod

    old = time.time() - 20 * DAY
    _write_index(
        "default",
        [
            _row("qa_old", created=old, updated=old),
            _row("qa_archived", created=old, updated=old),
            _row("chat_old", created=old, updated=old),
            _row("pinned_old", created=old, updated=old, pinned=True),
            _row("recent", created=time.time()),
        ],
    )
    # a scheduled run references qa_old → must NOT be archived
    runs_dir = paths_mod.schedule_runs_dir()
    runs_dir.mkdir(parents=True, exist_ok=True)
    (runs_dir / "runs-2026-10-01.jsonl").write_text(
        _json.dumps({"run_id": "sr-1", "session_id": "qa_old"}) + "\n", encoding="utf-8"
    )
    counts = {"qa_old": 1, "qa_archived": 1, "chat_old": 9, "pinned_old": 1, "recent": 1}
    monkeypatch.setattr(maintenance, "_message_count", lambda slug, sid: counts.get(sid, 0))
    monkeypatch.setattr(maintenance, "_has_messages", lambda slug, sid: True)

    maintenance.run_startup_maintenance()
    rows = {m["id"]: m for m in _session_meta_list("default")}
    assert rows["qa_old"].get("archived") is not True   # referenced by a run
    assert rows["qa_archived"].get("archived") is True  # one-shot Q&A, long idle
    assert rows["qa_archived"].get("archived_auto") is True
    assert rows["chat_old"].get("archived") is False    # real conversation
    assert rows["pinned_old"].get("archived") is None   # pinned: untouched
    assert rows["recent"].get("archived") is None       # too fresh to judge


def test_last_success_backfill(isolated_home, monkeypatch):
    """P0-1: existing history seeds ``last_success`` so the card doesn't claim
    "never succeeded" for a task that actually ran fine yesterday."""
    from ginno_runtime import schedule_store

    schedule_store.save_config(
        {
            "enabled": True,
            "tasks": [
                {"id": "t1", "name": "A", "target": {"type": "prompt"}, "schedule": {}},
                {"id": "t2", "name": "B", "target": {"type": "prompt"}, "schedule": {}},
            ],
        }
    )
    day = time.strftime("%Y-%m-%d")
    rows = [
        {"run_id": "r-old", "task_id": "t1", "status": "ok", "started_at": 100, "finished_at": 200},
        {"run_id": "r-new", "task_id": "t1", "status": "ok", "started_at": 300, "finished_at": 400},
        {"run_id": "r-bad", "task_id": "t2", "status": "error", "started_at": 500, "finished_at": 600},
    ]
    d = paths.schedule_runs_dir()
    d.mkdir(parents=True, exist_ok=True)
    (d / f"runs-{day}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )
    schedule_store.reset_cache()

    maintenance._backfill_last_success()
    tasks = {t["id"]: t for t in schedule_store.list_tasks()}
    assert tasks["t1"]["last_success"] == {"run_id": "r-new", "finished": 400}
    assert "last_success" not in tasks["t2"]  # only failures: still unseeded
