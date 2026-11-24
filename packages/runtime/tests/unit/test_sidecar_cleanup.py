"""The pytest session fallback must only stop its registered children."""

import os
import subprocess

import _sidecar as sc
import pytest

pytestmark = pytest.mark.unit


def test_session_cleanup_leaves_unregistered_sidecar_alive(tmp_path):
    binary = tmp_path / "other-worktree" / "dist" / "ginno-runtime" / "ginno-runtime"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexec sleep 60\n")
    binary.chmod(0o700)

    owned = sc.spawn_sidecar(str(binary), dict(os.environ))
    try:
        external = subprocess.Popen([str(binary)], start_new_session=True)
        try:
            sc.stop_spawned_sidecars()
            assert owned.poll() is not None
            assert external.poll() is None
        finally:
            sc.stop_sidecar(external)
    finally:
        sc.stop_sidecar(owned)
