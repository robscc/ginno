"""版本号单一真源防漂移（2026-11-15）。

背景：`_version.py` 长期停在 0.1.0，而 pyproject / tauri.conf / package.json
已 bump 到 0.3.3——发版 runbook 只列了那三处，漏了 `_version.py`。后果不止
显示错：`ginno_user_agent()` 返回 `ginno/0.1.0`，而网关按 UA 识别 Ginno 流量；
A2A Agent Card 的 version 也是错的（远端对端看到 0.1.0）。

本测试把「runtime 版本 == pyproject 版本」钉在 CI 上，让漂移无法合并。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ginno_runtime._version import __version__, ginno_user_agent

_RUNTIME_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT = _RUNTIME_ROOT.parents[1]


def _pyproject_version() -> str:
    text = (_RUNTIME_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert m, "pyproject.toml 里找不到 version"
    return m.group(1)


def test_version_matches_pyproject():
    """_version.py 必须与 pyproject.toml 一致——发版漏 bump 会在此红。"""
    assert __version__ == _pyproject_version(), (
        f"_version.py={__version__} 与 pyproject={_pyproject_version()} 不一致。"
        "发版时四处都要 bump：tauri.conf.json / package.json / pyproject.toml / _version.py"
    )


def test_user_agent_carries_real_version():
    """UA 是网关识别 Ginno 流量的依据，不能是陈旧版本。"""
    assert ginno_user_agent() == f"ginno/{_pyproject_version()}"


def test_no_stale_version_literals_in_runtime():
    """runtime 源码里不得再出现硬编码的旧版本号。

    只查 src/，不查测试（测试里可能有合法的 fixture 版本字符串）。
    """
    stale = {"0.1.0", "0.0.0"}
    hits: list[str] = []
    for py in (_RUNTIME_ROOT / "src" / "ginno_runtime").rglob("*.py"):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):
                continue
            for s in stale:
                if f'"{s}"' in line or f"'{s}'" in line:
                    hits.append(f"{py.relative_to(_RUNTIME_ROOT)}:{lineno}: {line.strip()}")
    assert not hits, "发现硬编码版本号，应改为 from ._version import __version__：\n" + "\n".join(hits)


@pytest.mark.parametrize(
    "rel",
    ["apps/desktop/tauri.conf.json", "apps/web/package.json"],
)
def test_other_release_surfaces_match(rel):
    """另外两处发版位点也必须与 runtime 同版本（三处同步的既有约定）。"""
    import json

    p = _REPO_ROOT / rel
    if not p.exists():  # 只装了 runtime 的环境（如 wheel 安装）跳过
        pytest.skip(f"{rel} 不存在")
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["version"] == __version__, f"{rel} 版本与 runtime 不一致"
