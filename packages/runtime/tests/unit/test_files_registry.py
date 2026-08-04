"""Unit tests: file registry (identity ledger + reactive touch layer)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime.files import get_by_id, get_registry, reset_registries

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _fresh_registry(isolated_home):
    reset_registries()
    yield
    reset_registries()


def _touch(p: Path) -> Path:
    p.write_text("x", encoding="utf-8")
    return p


def test_register_and_get(tmp_path):
    f = _touch(tmp_path / "a.xlsx")
    reg = get_registry("default")
    e = reg.register("a.xlsx", f, session_id="s1")
    assert e["id"] and e["name"] == "a.xlsx"
    assert e["kind"] == "spreadsheet"  # classified by extension
    assert e["session_id"] == "s1"
    assert e["project_slug"] == "default"
    assert e["mtime"] > 0
    assert reg.get(e["id"]) is e
    assert get_by_id(e["id"]) is e


def test_find_by_path_and_idempotent(tmp_path):
    f = _touch(tmp_path / "b.csv")
    reg = get_registry("default")
    e1 = reg.register("b.csv", f, session_id="s1")
    # resolving the same path finds the entry
    assert reg.find_by_path(f) is e1
    # re-registering the same path updates, not duplicates
    e2 = reg.register("renamed.csv", f, session_id="s2")
    assert e2["id"] == e1["id"]
    assert e2["name"] == "renamed.csv"
    assert e2["session_id"] == "s2"
    assert len(reg.list_all()) == 1


def test_list_session_scoping(tmp_path):
    reg = get_registry("default")
    a = reg.register("a.csv", _touch(tmp_path / "a.csv"), session_id="s1")
    reg.register("b.csv", _touch(tmp_path / "b.csv"), session_id="s2")
    ids = {e["id"] for e in reg.list_session("s1")}
    assert ids == {a["id"]}


def test_persistence_survives_reset(tmp_path):
    f = _touch(tmp_path / "p.docx")
    reg = get_registry("proj")
    e = reg.register("p.docx", f, session_id="s1", artifact_id="art1")
    eid = e["id"]

    reset_registries()  # simulate process restart

    reg2 = get_registry("proj")
    loaded = reg2.get(eid)
    assert loaded is not None
    assert loaded["name"] == "p.docx"
    assert loaded["artifact_id"] == "art1"
    assert reg2.find_by_path(f)["id"] == eid


def test_touch_fires_subscribers(tmp_path):
    f = _touch(tmp_path / "c.xlsx")
    reg = get_registry("default")
    e = reg.register("c.xlsx", f, session_id="s1")

    seen: list[tuple[str, str]] = []
    from ginno_runtime.files import registry as regmod

    unsub = regmod.subscribe(f, lambda entry, reason: seen.append((entry["id"], reason)))

    from ginno_runtime.files import touch

    touched = touch(f, reason="tool:write_file")
    assert [t["id"] for t in touched] == [e["id"]]
    assert seen == [(e["id"], "tool:write_file")]

    unsub()
    touch(f)
    assert len(seen) == 1  # no further callbacks after unsubscribe


def test_touch_untracked_path_is_noop(tmp_path):
    from ginno_runtime.files import touch

    assert touch(tmp_path / "never-registered.bin") == []


def test_mark_stale(tmp_path):
    f = _touch(tmp_path / "d.xlsx")
    reg = get_registry("default")
    e = reg.register("d.xlsx", f)
    assert e["stale"] is False
    reg.mark_stale(e["id"], True)
    assert reg.get(e["id"])["stale"] is True
    # re-register (e.g. fresh upload) clears the flag
    reg.register("d.xlsx", f)
    assert reg.get(e["id"])["stale"] is False


def test_set_kind(tmp_path):
    f = _touch(tmp_path / "weird.csv")
    reg = get_registry("default")
    e = reg.register("weird.csv", f)
    assert e["kind"] == "table"
    # user correction from the metadata inspector
    upd = reg.set_kind(f, "spreadsheet")
    assert upd["kind"] == "spreadsheet"
    assert reg.find_by_path(f)["kind"] == "spreadsheet"
    # unknown path / empty kind are safe no-ops
    assert reg.set_kind(tmp_path / "missing.csv", "x") is None
    assert reg.set_kind(f, "")["kind"] == "spreadsheet"


def test_relocate_rekeys_path_index(tmp_path):
    src = _touch(tmp_path / "old.csv")
    reg = get_registry("default")
    e = reg.register("old.csv", src, session_id="s1")
    fid = e["id"]
    dst = tmp_path / "moved" / "old.csv"
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    upd = reg.relocate(fid, dst)
    assert upd is not None
    assert reg.get(fid)["path"] == str(dst.resolve())
    # old path no longer resolves; new path does
    assert reg.find_by_path(src) is None
    assert reg.find_by_path(dst)["id"] == fid
    assert reg.get(fid)["stale"] is False


def test_relocate_normalizes_symlinked_tmp(tmp_path):
    # /tmp ↔ /private/tmp style: register via one spelling, relocate via another
    src = _touch(tmp_path / "a.csv")
    reg = get_registry("default")
    e = reg.register("a.csv", src)
    fid = e["id"]
    dst = tmp_path / "b.csv"
    src.rename(dst)
    reg.relocate(fid, str(dst))
    assert reg.find_by_path(dst)["id"] == fid


def test_unregister_removes_entry_not_file(tmp_path):
    f = _touch(tmp_path / "gone.csv")
    reg = get_registry("default")
    e = reg.register("gone.csv", f, session_id="s1")
    fid = e["id"]
    assert reg.unregister(fid) is True
    assert reg.get(fid) is None
    assert reg.find_by_path(f) is None
    assert f.exists()  # the file itself is untouched
    assert reg.unregister(fid) is False  # idempotent / unknown id


def test_unique_dest_no_clobber(tmp_path):
    from ginno_runtime.files import unique_dest

    cand = tmp_path / "r.csv"
    assert unique_dest(cand) == cand  # free name passes through
    cand.write_text("x")
    alt = unique_dest(cand)
    assert alt == tmp_path / "r (1).csv"
    alt.write_text("y")
    assert unique_dest(cand) == tmp_path / "r (2).csv"
