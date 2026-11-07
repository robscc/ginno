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
import os
import shlex
import shutil
import subprocess

from ..paths import settings_path

log = logging.getLogger(__name__)

# 命令重写白名单（保守起步）：token 前缀精确匹配。2026-11-07 按 rtk 1.1.0
# 逐条实测复核（rg/find/diff 为新增；其余沿用 0.42.1 实测结论）：
#   ✅ rg    — 1.1.0 已非透传：分组头 "N matches in M files" + 截断保计数，
#              5000 行样本 111 匹配压到 28 行；无匹配 rc=1 与原生一致
#   ✅ find  — compact 树形输出；危险 flag（-delete 等）实测透传且生效
#              （行为与原生一致，无静默吞掉）
#   ✅ diff  — -u/普通格式输出与原生逐字一致，rc=1 一致；大 diff 才触发压缩
#   ❌ err/test/read/summary — 均为「rtk 包装任意命令」的 runner 形态，而
#              首 token 同名的 shell 内建（test -f / read -r）或原生命令
#              会被改写破坏语义，且 err/summary 会吞掉 stdout——不加
#   ❌ log   — rtk log 是 macOS 系统 log 命令的代理（show/stream/collect），
#              不是文件日志过滤器；首 token 为 log 的场景无压缩收益——不加
#   ❌ terraform — 无子命令（0.42.1 实测，1.1.0 help 未列出）
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
    ("rg",),
    ("find",),
    ("diff",),
)

# 段级命令元字符：拆段后段内出现任一（引号内也算，保守）即该段不重写——
# 管道/重定向会破坏 rtk 的输出语义；单 &（后台）、< 同理。
_SEG_META = "|<>&\n"
# 顶层拆分符：&& 与 ;（引号内不拆）。反引号/换行 → 整条不重写。
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
    if not path:
        # GUI 应用不继承登录 shell 的 PATH（launchd 只给 /usr/bin:/bin:…），
        # Homebrew 安装的 rtk 位于 which 找不到的目录（2026-11-04 实案：
        # app 内启动 sidecar detect 失败 → 集成整体静默失效）。补探两个
        # Homebrew 标准前缀；仅 when which 未命中，不改变 PATH 语义。
        for cand in ("/opt/homebrew/bin/rtk", "/usr/local/bin/rtk"):
            if os.access(cand, os.X_OK):
                path = cand
                break
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


def _split_segments(command: str) -> list[tuple[str, str]] | None:
    """按顶层 ``&&`` / ``;`` 拆段（引号内不拆）。

    返回 ``[(segment_text, separator), ...]``——段文本自带分隔符两侧的空白，
    重组时 ``"".join(seg + sep)`` 即可逐字节还原原命令。反引号（命令替换）、
    换行、引号不闭合 → None（整条不重写，保持保守：改写失败的方向永远是
    「一切照旧」）。双引号内的 ``\\"`` 按转义处理；多字节转义边界最多误判
    到「不闭合」一侧，同样只是放弃重写。
    """
    segs: list[tuple[str, str]] = []
    cur: list[str] = []
    quote: str | None = None
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote:
            cur.append(c)
            if c == quote and (quote == "'" or command[i - 1] != "\\"):
                quote = None
            i += 1
            continue
        if c in "'\"":
            quote = c
            cur.append(c)
            i += 1
            continue
        if c == "`" or c == "\n":
            return None
        if c == "&" and command.startswith("&&", i):
            segs.append(("".join(cur), "&&"))
            cur = []
            i += 2
            continue
        if c == ";":
            segs.append(("".join(cur), ";"))
            cur = []
            i += 1
            continue
        cur.append(c)
        i += 1
    if quote:
        return None
    segs.append(("".join(cur), ""))
    return segs


def _seg_whitelisted(seg: str) -> bool:
    """单段是否命中白名单（含 ``git -C <path> <sub>`` 形态）。

    段内含任何段级元字符（``| < > &``）即 False——管道/重定向段整段原样。
    """
    seg = seg.strip()
    if not seg or any(c in seg for c in _SEG_META):
        return False
    try:
        tokens = shlex.split(seg)
    except ValueError:  # 引号不闭合等 — 交给原 shell 报错，我们不动
        return False
    if not tokens:
        return False
    for prefix in _RTK_WHITELIST:
        if tuple(tokens[: len(prefix)]) == prefix:
            return True
    # git -C <path> status|diff|log（B2）：目录参数形态，rtk 1.1.0 实测透传正常
    return bool(
        len(tokens) >= 4
        and tokens[0] == "git"
        and tokens[1] == "-C"
        and tokens[3] in ("status", "diff", "log")
    )


def rewrite(command: str) -> str | None:
    """白名单命中且有 rtk 时返回改写后的命令，否则 None（一切照旧）。

    B3（2026-11-07）复合段感知：命令按顶层 ``&&`` / ``;`` 拆段，每段独立
    匹配白名单，命中段前插 ``rtk ``，未命中段（含管道/重定向段）原样保留，
    重组后整条返回——``cd X && git log`` 里第二段现在能命中（此前含 ``&&``
    一律不重写，实际会话模型几乎全写复合命令，rtk 100% 未命中）。
    """
    if not command or not command.strip():
        return None
    if not _rtk_enabled():
        return None
    rtk = detect_rtk()
    if rtk is None:
        return None
    segs = _split_segments(command)
    if segs is None:
        return None
    # 用绝对路径调用 rtk：GUI sidecar 的最小 PATH 环境（或测试环境）下登录
    # shell 未必含 rtk 所在目录——裸 `rtk` 会 command not found（exit 127），
    # detect_rtk 的 Homebrew fallback 探到的正是这种环境。
    parts: list[str] = []
    changed = False
    for seg, sep in segs:
        if _seg_whitelisted(seg):
            # 保留段两侧原有空白，只在核心命令前插 rtk —— 重组结果与原命令
            # 逐字节对齐（仅多出 "<rtk> "），`cd X && git log` → `cd X && <rtk> git log`
            lead = seg[: len(seg) - len(seg.lstrip())]
            trail = seg[len(seg.rstrip()):]
            parts.append(f"{lead}{rtk} {seg.strip()}{trail}")
            parts.append(sep)
            changed = True
        else:
            parts.append(seg)
            parts.append(sep)
    return "".join(parts) if changed else None
