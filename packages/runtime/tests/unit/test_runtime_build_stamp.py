"""Check the runtime Makefile cache without invoking the PyInstaller build."""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_runtime_strip_change_invalidates_stamp_after_failed_build(tmp_path):
    if not (shutil.which("make") and shutil.which("shasum") and Path("/bin/bash").is_file()):
        pytest.skip("runtime Makefile requires make, shasum and bash")

    root = Path(__file__).resolve().parents[4]
    runtime = tmp_path / "runtime"
    (runtime / "src").mkdir(parents=True)
    (runtime / "bin").mkdir()
    (runtime / "scripts").mkdir()
    (runtime / "scripts/fetch_tiktoken_vocab.py").touch()
    (runtime / "pyproject.toml").touch()
    web = tmp_path / "web"
    web.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    uv = tools / "uv"
    uv.write_text(
        '#!/bin/bash\n'
        'set -eu\n'
        'mkdir -p dist/ginno-runtime/_internal\n'
        'printf "%s\\n" "$*" > dist/ginno-runtime/ginno-runtime\n'
        'chmod +x dist/ginno-runtime/ginno-runtime\n'
        'echo "stub PyInstaller"\n'
        'exit "${BUILD_EXIT:-0}"\n'
    )
    uv.chmod(0o755)
    stamp = tmp_path / "cache/runtime.stamp"
    binary = runtime / "dist/ginno-runtime/ginno-runtime"

    def build(flag, exit_code=0):
        return subprocess.run(
            ["make", "-o", "web", "runtime", f"RUNTIME={runtime}", f"WEB_OUT={web}",
             f"CACHE={stamp.parent}", f"RUNTIME_STRIP_FLAG={flag}", "FORCE_RUNTIME=0"],
            cwd=root,
            env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                 "BUILD_EXIT": str(exit_code)},
            capture_output=True,
            text=True,
            timeout=15,
        )

    first = build("--strip")
    assert first.returncode == 0, first.stdout + first.stderr
    original_stamp = stamp.read_text()
    assert "--strip" in binary.read_text()
    assert "跳过 PyInstaller" in build("--strip").stdout

    failed = build("", exit_code=1)
    assert failed.returncode != 0
    assert "--strip" not in binary.read_text()
    assert not stamp.exists()

    restored = build("--strip")
    assert restored.returncode == 0, restored.stdout + restored.stderr
    assert "--strip" in binary.read_text()
    assert stamp.read_text() == original_stamp

    disabled = build("")
    assert disabled.returncode == 0, disabled.stdout + disabled.stderr
    assert "--strip" not in binary.read_text()
    assert stamp.read_text() != original_stamp
    assert "跳过 PyInstaller" in build("").stdout
