"""_build.build_info 的 channel env 覆盖（docs/runtime-hot-swap-design.md P1）。

壳的 spawn ladder 从 dev-override 拉起 sidecar 时注入 GINNO_RUNTIME_CHANNEL=dev
（sidecar.rs）——channel 是「进程从哪被拉起」的运行时事实，build-info.json 写死
的构建目标永远是 app，前端 dev·徽标因此才有了可靠信号（2026-11-04）。
"""

from __future__ import annotations

import pytest

from ginno_runtime import _build

pytestmark = pytest.mark.unit


def test_channel_env_override(monkeypatch):
    _build.build_info.cache_clear()
    try:
        # 模拟 factory/dev-override bundle：build-info.json 的 channel=app
        monkeypatch.setattr(
            _build,
            "_read_bundled_info",
            lambda: {
                "fingerprint": "abc1234",
                "channel": "app",
                "built_at": "2026-11-04T00:00:00Z",
            },
        )
        # 无 env：原样透出（factory spawn 不注入）
        monkeypatch.delenv("GINNO_RUNTIME_CHANNEL", raising=False)
        assert _build.build_info()["channel"] == "app"
        # 壳从 dev-override 拉起 → env 覆盖为 dev，其余字段不动
        _build.build_info.cache_clear()
        monkeypatch.setenv("GINNO_RUNTIME_CHANNEL", "dev")
        info = _build.build_info()
        assert info["channel"] == "dev"
        assert info["fingerprint"] == "abc1234"
        assert info["built_at"] == "2026-11-04T00:00:00Z"
    finally:
        # lru_cache 清掉，别把带 monkeypatch 的结果漏给后续测试
        _build.build_info.cache_clear()
