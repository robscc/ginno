"""Privileged-session mount semantics (runtime-data-access-design.md).

A privileged session is a read-only mount of the runtime data home. Three
invariants under test:
  1. library clamp — any entry whose path is the runtime home or inside it is
     ``ro`` by force (create / re-add / patch all clamp);
  2. execution gate — ``resolve_session_dirs`` re-clamps even a hand-edited
     folders.json entry (rw inside the runtime home resolves read-only);
  3. deny order — always-deny (credential files) beats the mount exemption,
     so settings/config/mcp stay unreadable even in a privileged session,
     while logs become readable; writes stay denied (ro is a hard tier).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ginno_runtime import context_folders as cf
from ginno_runtime.tools.builtin import _deny_policy

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("GINNO_HOME", str(tmp_path))
    yield


def _home() -> Path:
    return cf.paths.home()


def _mk_runtime_tree() -> tuple[Path, Path, Path, Path]:
    """(logs file, settings.json, config.json, mcp.json) inside $GINNO_HOME."""
    logs = _home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_file = logs / "sidecar.log"
    log_file.write_text("2026-11-08 10:00:00 ERROR demo session=x\n", encoding="utf-8")
    settings = _home() / "settings.json"
    settings.write_text('{"providers": {}}', encoding="utf-8")
    config = _home() / "config.json"
    config.write_text("{}", encoding="utf-8")
    mcp = _home() / "mcp" / "mcp.json"
    mcp.parent.mkdir(parents=True, exist_ok=True)
    mcp.write_text('{"mcpServers": {}}', encoding="utf-8")
    return log_file, settings, config, mcp


# --------------------------------------------------------------------------- #
# 1. library clamp
# --------------------------------------------------------------------------- #

def test_add_folder_inside_runtime_home_forces_ro():
    _home().mkdir(parents=True, exist_ok=True)
    f = cf.add_folder(str(_home()), access="rw")
    assert f["access"] == "ro"
    assert f["forced_ro"] is True


def test_add_folder_subdir_of_runtime_home_forces_ro():
    logs = _home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    f = cf.add_folder(str(logs), access="rw")
    assert f["access"] == "ro"
    assert f["forced_ro"] is True


def test_readd_existing_runtime_entry_ignores_rw_request():
    _home().mkdir(parents=True, exist_ok=True)
    f1 = cf.add_folder(str(_home()))
    f2 = cf.add_folder(str(_home()), access="rw")
    assert f2["id"] == f1["id"]
    assert f2["access"] == "ro"


def test_patch_cannot_escalate_runtime_entry_to_rw():
    _home().mkdir(parents=True, exist_ok=True)
    f = cf.add_folder(str(_home()))
    out = cf.update_folder(f["id"], {"access": "rw"})
    assert out["access"] == "ro"
    assert out["forced_ro"] is True


def test_patch_moving_path_into_runtime_home_clamps():
    outside = _home().parent / "plain"
    outside.mkdir(parents=True, exist_ok=True)
    f = cf.add_folder(str(outside), access="rw")
    assert f["access"] == "rw"
    out = cf.update_folder(f["id"], {"path": str(_home())})
    assert out["access"] == "ro"
    assert out["forced_ro"] is True


def test_probe_reports_runtime_home():
    _home().mkdir(parents=True, exist_ok=True)
    assert cf.probe(str(_home())).get("runtime_home") is True
    outside = _home().parent / "plain"
    outside.mkdir(parents=True, exist_ok=True)
    assert cf.probe(str(outside)).get("runtime_home") is False


# --------------------------------------------------------------------------- #
# 2. execution gate (resolve_session_dirs re-clamp)
# --------------------------------------------------------------------------- #

def test_resolve_reclamps_hand_edited_rw_entry():
    import json as _json

    _home().mkdir(parents=True, exist_ok=True)
    f = cf.add_folder(str(_home()))
    # Simulate a hand-edited library entry: flip access to rw on disk.
    lib = _json.loads((cf.paths.home() / "folders.json").read_text(encoding="utf-8"))
    for e in lib["folders"]:
        if e["id"] == f["id"]:
            e["access"] = "rw"
            e.pop("forced_ro", None)
    (cf.paths.home() / "folders.json").write_text(
        _json.dumps(lib), encoding="utf-8"
    )
    dirs, _ = cf.resolve_session_dirs([f["id"]], None)
    assert dirs[0]["access"] == "ro"
    assert dirs[0]["forced_ro"] is True


# --------------------------------------------------------------------------- #
# 3. deny order — credential files stay blocked under the mount
# --------------------------------------------------------------------------- #

def test_mount_beats_home_deny_but_not_always_deny():
    log_file, settings, config, mcp = _mk_runtime_tree()
    policy = _deny_policy(str(_home() / "projects" / "d" / "sessions" / "s"), [str(_home())])
    # The mount (extra_roots = runtime home) opens the tree…
    assert policy.denied(log_file) is False
    # …but credential files stay denied: always-deny precedes mount exemption.
    assert policy.denied(settings) is True
    assert policy.denied(config) is True
    assert policy.denied(mcp) is True


def test_no_mount_keeps_whole_home_denied():
    log_file, settings, _, _ = _mk_runtime_tree()
    base = _home() / "projects" / "d" / "sessions" / "s"
    policy = _deny_policy(str(base), [])
    assert policy.denied(log_file) is True
    assert policy.denied(settings) is True


def test_path_traversal_out_of_mount_still_denied():
    log_file, _, _, _ = _mk_runtime_tree()
    policy = _deny_policy(str(_home() / "projects" / "d" / "sessions" / "s"), [str(_home() / "logs")])
    assert policy.denied(log_file) is False
    assert policy.denied(_home() / "logs" / ".." / "settings.json") is True


# --------------------------------------------------------------------------- #
# 4. subagents do not inherit the privileged mount
# --------------------------------------------------------------------------- #

def test_spawn_filter_drops_runtime_mounts_only():
    from ginno_runtime.subagent_scheduler.spawn import _filter_privileged_mounts

    _home().mkdir(parents=True, exist_ok=True)
    plain = _home().parent / "plain"
    plain.mkdir(parents=True, exist_ok=True)
    priv = cf.add_folder(str(_home()))
    normal = cf.add_folder(str(plain))
    kept = _filter_privileged_mounts([priv["id"], normal["id"], "f_unknown"])
    assert kept == [normal["id"], "f_unknown"]
