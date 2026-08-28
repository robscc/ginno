"""Debug 模式开关（restart-to-apply）。

浏览器模式（内嵌 CEF / CDP / ``browser_*`` 工具 / ``browse`` skill 等不稳定特性）
整体收在 ``settings.json`` 的 ``debug`` 布尔后面：进程启动后本模块只读一次并缓存，
运行中改 ``settings.json`` 不生效——必须重启应用（Rust shell 同样启动时读一次）。

测试环境（``conftest.py`` 设 ``GINNO_BROWSER_ENGINE=fake``）与开发环境可用
``GINNO_DEBUG=1`` 或任一 ``GINNO_BROWSER_ENGINE`` 值强制开启，避免破坏
``test_browser_*`` 用例。
"""

from __future__ import annotations

import json
import os
import threading

from . import paths

_lock = threading.Lock()
_value: bool | None = None


def debug_enabled() -> bool:
    """进程级单次求值：第一次调用读 settings.json 后缓存，重启才重新读。"""
    global _value
    with _lock:
        if _value is None:
            _value = _read()
        return _value


def _read() -> bool:
    # 开发/测试用 env 强制开启（tests conftest 设 GINNO_BROWSER_ENGINE=fake）。
    if os.environ.get("GINNO_DEBUG") == "1":
        return True
    if (os.environ.get("GINNO_BROWSER_ENGINE") or "").strip():
        return True
    try:
        p = paths.settings_path()
        if p.exists():
            return bool((json.loads(p.read_text(encoding="utf-8") or "{}") or {}).get("debug"))
    except Exception:
        pass
    return False


def reset_debug_cache() -> None:
    """仅测试隔离用：强制下次 ``debug_enabled()`` 重新读盘。"""
    global _value
    with _lock:
        _value = None