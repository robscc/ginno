"""rtk 零分发集成（跟随用户已装的 rtk CLI）。

rtk（https://github.com/... 本机 /opt/homebrew/bin/rtk）是 CLI 输出压缩代理：
``rtk git status`` / ``rtk pytest`` 等子命令以 token 优化的格式代理原生命令。
本模块只做一件事：当 bash 工具收到的命令**完整命中白名单**且本机装有 rtk 时，
把命令改写为 ``rtk <原命令>`` 再交给既有 shell 执行链路；输出截断、退出码、
超时处理全部不变，对模型透明。

纪律（源自方案评审）：
- 检测每进程只做一次（``detect_rtk`` 进程级缓存），失败静默返回 None；
- crates.io 有同名无关 crate，用 ``rtk --version`` 输出以 "rtk " 开头做弱校验；
- 只重写「首 token 序列精确匹配」的简单命令行——含管道/重定向/组合（``| & ; < >
  `` ` ``）的复合命令一律不重写；
- settings.json 的 ``rtk.enabled`` 开关（默认 true）可整体关闭；
- 任何异常都不允许阻塞 bash 工具——最坏情况就是「一切照旧」。
"""

from __future__ import annotations

import json
import logging
import shlex
import shutil
import subprocess

from ..paths import settings_path

log = logging.getLogger(__name__)

# 命令重写白名单（保守起步）：token 前缀精确匹配。全部条目经 rtk 0.42.1
# `rtk <cmd> --help` 实测确认有专用压缩子命令（2026-11-04）；terraform 无
# 子命令、rg 被透传原生执行（无压缩且输出更差）——均不入白名单。
_RTK_WHITELIST: tuple[tuple[str, ...], ...] = (
    ("git", "status"),
    ("git", "diff"),
    ("git", "log"),
    ("pytest",),
    ("npm", "test"),
    ("pnpm", "test"),
    ("docker", "ps"),
    ("ls",),
    ("tree",),
    ("cargo", "build"),
    ("cargo", "test"),
    ("go", "test"),
    ("kubectl", "get"),
    ("kubectl", "pods"),
    ("gh", "pr"),
    ("grep",),
)

# 复合命令特征：出现任一字符即不重写（管道/重定向/列表/命令替换/换行）。
_SHELL_META = "|&;<>`\n"

_UNSET = object()
_detect_cache: object = _UNSET


def detect_rtk() -> str | None:
    """返回 rtk 可执行文件路径；未安装 / 版本输出不符 / 任何异常 → None。

    进程级缓存一次：首个成功/失败结论固化，后续调用零开销。PATH 无 rtk 时
    一切照旧，绝不阻塞调用方。
    """
    global _detect_cache
    if _detect_cache is not _UNSET:
        return _detect_cache  # type: ignore[return-value]
    path = shutil.which("rtk")
    result: str | None = None
    if path:
        try:
            proc = subprocess.run(
                [path, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # 弱校验：crates.io 有同名无关 crate，真 rtk 的 --version 以 "rtk " 开头
            if proc.returncode == 0 and proc.stdout.strip().startswith("rtk "):
                result = path
        except (OSError, subprocess.SubprocessError):
            result = None
    _detect_cache = result
    if result is None:
        log.info("rtk_not_detected — bash 工具按原样执行命令")
    return result


def _rtk_enabled() -> bool:
    """读 settings.json 的 ``rtk.enabled``（默认 true）。文件缺失/坏 JSON 视为开启。"""
    try:
        p = settings_path()
        if not p.exists():
            return True
        data = json.loads(p.read_text(encoding="utf-8") or "{}")
        rtk = data.get("rtk")
        if isinstance(rtk, dict):
            return rtk.get("enabled", True) is not False
        return True
    except Exception:  # noqa: BLE001 — 开关读取失败绝不能挡住 bash 工具
        return True


def reset_rtk_cache() -> None:
    """仅测试用：清空进程级检测缓存。"""
    global _detect_cache
    _detect_cache = _UNSET


def rewrite(command: str) -> str | None:
    """白名单命中且有 rtk 时返回 ``rtk <原命令>``，否则 None（一切照旧）。"""
    if not command or not command.strip():
        return None
    if not _rtk_enabled():
        return None
    if detect_rtk() is None:
        return None
    if any(c in command for c in _SHELL_META):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:  # 引号不闭合等 — 交给原 shell 报错，我们不动
        return None
    if not tokens:
        return None
    for prefix in _RTK_WHITELIST:
        if tuple(tokens[: len(prefix)]) == prefix:
            return f"rtk {command}"
    return None
