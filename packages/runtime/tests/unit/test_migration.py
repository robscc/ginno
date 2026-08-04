"""Unit tests: best-effort startup migration of session files into session dirs."""

from __future__ import annotations

import pytest

from ginno_runtime import artifacts as art_store
from ginno_runtime import migration, paths
from ginno_runtime.files import get_registry, norm_path, reset_registries

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh(isolated_home):
    reset_registries()
    yield
    reset_registries()


def _seed_legacy(isolated_home, tmp_path, sid="s1"):
    """Create a legacy upload + a legacy analyze result, registered + artifed."""
    reg = get_registry("default")

    # legacy upload at <workspace>/uploads/<sid>/<file>  (shared workspace shape)
    up = tmp_path / "gw" / "uploads" / sid / "abc123-data.xlsx"
    up.parent.mkdir(parents=True, exist_ok=True)
    up.write_bytes(b"x")
    art_up = art_store.add_artifact("default", "file", "data.xlsx", norm_path(up), sid)
    e_up = reg.register("data.xlsx", up, session_id=sid, artifact_id=art_up["id"])

    # legacy analyze result at <source>/results/<stem>-result-<hex>.csv
    res = tmp_path / "elsewhere" / "results" / "data-result-deadbeef.csv"
    res.parent.mkdir(parents=True, exist_ok=True)
    res.write_text("a,b\n1,2\n")
    art_res = art_store.add_artifact("default", "file", res.name, norm_path(res), sid)
    e_res = reg.register(res.name, res, kind="table", session_id=sid, artifact_id=art_res["id"])

    return reg, e_up, e_res, art_up, art_res


def test_migration_moves_legacy_files_into_session_dirs(isolated_home, tmp_path):
    reg, e_up, e_res, art_up, art_res = _seed_legacy(isolated_home, tmp_path)
    sid = "s1"

    stats = migration.migrate_session_files()
    assert stats["moved"] == 2
    assert stats["errors"] == 0

    up_dir = paths.session_uploads_dir("default", sid)
    res_dir = paths.session_results_dir("default", sid)
    new_up = reg.get(e_up["id"])["path"]
    new_res = reg.get(e_res["id"])["path"]
    # upload → uploads/, analyze result → results/
    assert norm_path(new_up).startswith(norm_path(up_dir))
    assert norm_path(new_res).startswith(norm_path(res_dir))
    # physical files moved (old gone, new present)
    assert not (tmp_path / "gw" / "uploads" / sid / "abc123-data.xlsx").exists()
    from pathlib import Path

    assert Path(new_up).is_file() and Path(new_res).is_file()
    # artifact refs rewritten in place (no duplicates)
    assert art_store.get_artifact("default", art_up["id"])["ref"] == norm_path(new_up)
    assert art_store.get_artifact("default", art_res["id"])["ref"] == norm_path(new_res)
    assert len(art_store.list_artifacts("default")) == 2


def test_migration_idempotent_second_run_noop(isolated_home, tmp_path):
    _seed_legacy(isolated_home, tmp_path)
    first = migration.migrate_session_files()
    assert first["moved"] == 2
    second = migration.migrate_session_files()
    assert second["moved"] == 0
    assert second["skipped"] == 2  # both already inside their session dir


def test_migration_missing_file_marks_stale(isolated_home, tmp_path):
    reg = get_registry("default")
    ghost = tmp_path / "gw" / "uploads" / "s2" / "gone.csv"
    ghost.parent.mkdir(parents=True, exist_ok=True)
    ghost.write_text("x")
    e = reg.register("gone.csv", ghost, session_id="s2")
    ghost.unlink()  # simulate tmp being cleared before startup

    stats = migration.migrate_session_files()
    assert stats["missing"] == 1
    assert stats["moved"] == 0
    assert reg.get(e["id"])["stale"] is True


def test_migration_skips_unattributable_entries(isolated_home, tmp_path):
    reg = get_registry("default")
    f = tmp_path / "loose.csv"
    f.write_text("x")
    reg.register("loose.csv", f)  # no session_id

    stats = migration.migrate_session_files()
    assert stats["skipped"] == 1
    assert stats["moved"] == 0
    assert f.exists()  # untouched
