"""Check the runtime Makefile cache without invoking the PyInstaller build."""

import gzip
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit


def test_runtime_strip_change_invalidates_stamp_after_failed_build(tmp_path):
    if not all(shutil.which(tool) for tool in ("make", "shasum", "bash", "gzip")):
        pytest.skip("runtime Makefile requires make, shasum, bash and gzip")

    root = Path(__file__).resolve().parents[4]
    runtime = tmp_path / "runtime"
    (runtime / "src").mkdir(parents=True)
    (runtime / "bin").mkdir()
    (runtime / "scripts").mkdir()
    (runtime / "scripts/fetch_tiktoken_vocab.py").touch()
    (runtime / "pyproject.toml").touch()
    web = tmp_path / "web"
    viewer = web / "vendor/viewer-static.min.js"
    viewer.parent.mkdir(parents=True)
    viewer.write_bytes(b"window.drawio = true;\n")
    tools = tmp_path / "tools"
    tools.mkdir()
    uv = tools / "uv"
    uv.write_text(
        '#!/bin/bash\n'
        'set -eu\n'
        'vendor=dist/ginno-runtime/_internal/web_out/vendor\n'
        'mkdir -p "$vendor"\n'
        'rm -f "$vendor/viewer-static.min.js.gz"\n'
        'cp "$BUILD_WEB/vendor/viewer-static.min.js" "$vendor/"\n'
        'printf "%s\\n" "$*" > dist/ginno-runtime/ginno-runtime\n'
        'chmod +x dist/ginno-runtime/ginno-runtime\n'
        'echo "stub PyInstaller"\n'
        'exit "${BUILD_EXIT:-0}"\n'
    )
    uv.chmod(0o755)
    stamp = tmp_path / "cache/runtime.stamp"
    binary = runtime / "dist/ginno-runtime/ginno-runtime"
    bundled_viewer = binary.parent / "_internal/web_out/vendor" / viewer.name

    def build(flag, exit_code=0):
        return subprocess.run(
            ["make", "-o", "web", "runtime", f"RUNTIME={runtime}", f"WEB_OUT={web}",
             f"CACHE={stamp.parent}", f"RUNTIME_STRIP_FLAG={flag}", "FORCE_RUNTIME=0"],
            cwd=root,
            env={**os.environ, "PATH": f"{tools}{os.pathsep}{os.environ['PATH']}",
                 "BUILD_EXIT": str(exit_code), "BUILD_WEB": str(web)},
            capture_output=True,
            text=True,
            timeout=15,
        )

    first = build("--strip")
    assert first.returncode == 0, first.stdout + first.stderr
    assert not bundled_viewer.exists()
    gzip_viewer = bundled_viewer.with_suffix(".js.gz")
    assert gzip.decompress(gzip_viewer.read_bytes()) == viewer.read_bytes()
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
    assert not bundled_viewer.exists()
    assert gzip.decompress(gzip_viewer.read_bytes()) == viewer.read_bytes()

    disabled = build("")
    assert disabled.returncode == 0, disabled.stdout + disabled.stderr
    assert "--strip" not in binary.read_text()
    assert stamp.read_text() != original_stamp
    assert "跳过 PyInstaller" in build("").stdout
