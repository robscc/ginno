"""Built-in tools: Read / Grep / Glob / Write / Edit / Bash.

The session workspace is BOUND into the tools at construction time
(``build_builtin_tools(workspace)`` — plan F1, unfrozen 2026-08 after the
skill-install incident: the model had no way to know the workspace, omitted
the param, and the tools fell back to the sidecar cwd ``/``). The model
never sees a ``workspace`` parameter: relative paths resolve against the
session workspace, absolute paths pass through unchanged.

Context folders (docs/context-folders-design.md, M0): a session may mount
additional directories (``context_dirs``). Effects:

* ``primary_path`` — one mounted dir may be the PRIMARY working dir: it
  becomes the base for relative paths and the bash cwd. Falls back to the
  session files dir when unset.
* ``ro`` mounts are a HARD write constraint (write_file / edit_file refuse),
  independent of ``bypass_permissions``. Reads/greps work everywhere mounted.
* Paths OUTSIDE the mounted set keep the pre-feature behaviour (M0 decision:
  status quo — the only fences are the hard-deny roots below).

Every tool returns an ``[error] ...`` string instead of raising, so a bad
path degrades to a tool result the agent can react to — never a dead turn
(the 2026-08 incident ended in ``OSError: [Errno 22]`` from a whole-disk
glob and killed the whole turn).
"""

from __future__ import annotations

import contextlib
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
import weakref
from collections import deque
from collections.abc import Callable
from pathlib import Path

from langchain_core.tools import tool

from ginno_runtime import jsonx as json

from .. import paths
from ..files.code_changes import encode_code_marker
from ..files.images import diff_images, encode_images_marker, snapshot_images
from ..lang import t

# A runaway glob (e.g. ``**`` under a huge tree) used to stream the entire
# filesystem listing into the context; cap it like grep_files does.
GLOB_MAX_HITS = 500
# Wall-clock + traversal budgets for glob/grep walks (2026-09-25: a pattern
# matching NOTHING walks the entire root before giving up — with a bad cwd
# (e.g. "/") that was the whole filesystem). Matches are capped separately;
# these bound the WALK itself so a fruitless search returns partial+warning
# instead of burning the tool timeout.
GLOB_TIME_BUDGET_S = 10.0
GLOB_MAX_DIRS = 20000

# Dependency/build dirs pruned from glob/grep walks by default (pass
# include_ignored=True to search them). Mirrors api/code.py's HEAVY_DIRS:
# without this the walk budget is burned inside node_modules/target/… and the
# matches the model actually wants drown in generated files.
_WALK_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".ginno-bash-out",
        "node_modules",
        ".next",
        "dist",
        "build",
        "out",
        "target",
        "coverage",
        "__pycache__",
        ".venv",
        "venv",
        ".turbo",
        ".pytest_cache",
        ".mypy_cache",
        ".gradle",
    }
)
# grep never reads a file larger than this (and passes the same value to rg as
# --max-filesize): one giant generated artifact must not eat the walk budget.
GREP_MAX_FILE_BYTES = 5 * 1024 * 1024
# read_file pagination guards. A file read without offset/limit longer than
# READ_MAX_LINES / READ_MAX_CHARS comes back truncated with a "pass offset=…"
# note — one huge read used to flood the context and bloat every later turn.
READ_MAX_LINES = 2000
READ_MAX_CHARS = 200_000
# Even an explicit limit= stays bounded by this (context protection).
READ_HARD_MAX_LINES = 5000

# ─── bash 工具的 rg 输入类报错提示 ──────────────────────────────────────────
# 管道场景（``rg … | head``）里 rg 的解析失败只落 stderr，退出码被尾命令吃成
# 0，模型当轮极难察觉（2026-10-19 会话 7a9bbb08：backreference 正则报错但
# [exit 0]，靠下一轮自愈）。Claude Code 对同一问题按 stderr 文本分类成
# RipgrepUsageError 整次拒绝；bash 场景无法拒绝整次调用，改为在输出尾部追加
# 显式 [hint]，让失败第一眼可见、且给出自愈方向。只提示，不改写退出码。
_RG_USAGE_ERROR_RE = re.compile(
    r"rg: (?:regex parse error|error parsing glob|unrecognized file type"
    r"|error parsing flag|compiled regex exceeds size limit)"
)
_RG_PCRE2_LIMIT_RE = re.compile(r"PCRE2: error matching: match limit exceeded")
_RG_NO_PCRE2_RE = re.compile(r"PCRE2 is not available in this build")


def _rg_usage_hint(stderr_text: str) -> str | None:
    """Classify captured stderr; return an actionable hint when it contains a
    known rg input-class failure, else None."""
    if _RG_USAGE_ERROR_RE.search(stderr_text):
        return t(
            "[hint] rg rejected the pattern (see stderr above). A pipeline's exit "
            "code is the LAST command's, so this failure was masked by exit 0. rg's "
            "default engine (Rust regex) has no backreferences or look-around — "
            "re-run with --pcre2, or rewrite the pattern.",
            "[hint] rg 拒绝了该输入（见上方 stderr）。管道退出码只取最后一个命令，"
            "这次失败被 exit 0 掩盖了。rg 默认引擎（Rust regex）不支持反向引用/"
            "环视——加 --pcre2 重试，或改写正则。",
        )
    if _RG_PCRE2_LIMIT_RE.search(stderr_text):
        return t(
            "[hint] PCRE2 match limit exceeded — the pattern backtracks "
            "catastrophically; simplify it (fewer nested/ambiguous quantifiers).",
            "[hint] 触发 PCRE2 回溯上限——正则存在灾难性回溯，请简化"
            "（减少嵌套/歧义量词）。",
        )
    if _RG_NO_PCRE2_RE.search(stderr_text):
        return t(
            "[hint] this rg build has no PCRE2 — rewrite the pattern without "
            "backreferences/look-around.",
            "[hint] 本机 rg 未编入 PCRE2——请改写正则，避免反向引用/环视。",
        )
    return None


def _fs_root_guard(search_root) -> str | None:
    """Refuse to walk the filesystem root outright — even within budget it is
    never what the caller wants; the model must name a real root."""
    try:
        import os as _os
        if _os.path.realpath(search_root) == "/":
            return (
                "[error] refusing to search from the filesystem root — pass an "
                "explicit root=<directory> (this session's working directory "
                f"is {search_root!s})"
            )
    except Exception:
        pass
    return None


def _always_deny_roots() -> list[str]:
    """Directories NO file/shell tool may ever touch, regardless of workspace
    (credentials — never a legitimate work location).

    The three config files carry plaintext provider keys. They stay denied
    even under a privileged session (a read-only mount of the runtime data
    home): always-deny is checked BEFORE mount exemptions in ``_DenyPolicy``,
    so masked reads stay the job of the config API / settings UI
    (runtime-data-access-design.md §3).
    """
    roots = [os.path.expanduser("~/.ssh"), os.path.expanduser("~/Library/Keychains")]
    try:
        roots += [
            str(paths.settings_path()),
            str(paths.home() / "config.json"),
            str(paths.mcp_config_path()),
        ]
    except Exception:  # pragma: no cover - paths only fails on broken env
        pass
    return roots


def _skill_exempt_roots() -> list[Path]:
    """Skill trees the model is allowed to read and execute from.

    Global + every project's ``skills/`` dir. Walking ``projects/*/skills``
    (instead of taking a slug) keeps bash token checks working even when
    the session slug is unknown at tool-build time.
    """
    roots: list[Path] = [paths.global_skills_dir()]
    projects = paths.home() / "projects"
    if projects.is_dir():
        for child in projects.iterdir():
            if child.is_dir():
                roots.append(child / "skills")
    return roots


# User shell rc / profile files. GUI-launched sidecars don't inherit an
# interactive login env, so skills that need `export`s (Aliyun / Volc
# keys, PATH extras) source these. They live in $HOME, not ~/.ginno, so
# the home-deny does not cover them — but we still list them so bash
# token scans and an explicit `source ~/.zshrc` stay allowed, and so
# ~/.ssh / keychains remain the only always-deny roots.
_USER_RC_NAMES = (
    ".zshrc",
    ".zprofile",
    ".zshenv",
    ".bashrc",
    ".bash_profile",
    ".profile",
)


def _is_user_rc(p: Path) -> bool:
    """True for a user's own shell rc/profile file (not ~/.ssh, not ~/.ginno)."""
    try:
        real = Path(os.path.realpath(Path(p).expanduser()))
    except OSError:
        real = Path(p).expanduser()
    if real.name not in _USER_RC_NAMES:
        return False
    return real.parent == Path.home()


def _inside(child_real: str, parent_real: str) -> bool:
    return child_real == parent_real or child_real.startswith(parent_real + os.sep)


def _realpath_str(p) -> str:
    try:
        return os.path.realpath(p)
    except OSError:
        return str(p)


# Revalidation cadence for the one LIVE value in _DenyPolicy: a project
# skills/ dir can appear mid-session, so the exempt set cannot be frozen at
# tool-build time — but it also must not cost an iterdir per checked path.
_SKILL_ROOTS_TTL_S = 5.0


class _DenyPolicy:
    """Precomputed deny/exempt regions for one (base_dir, extra_roots) pair.

    The pre-2026-10 `_path_denied` recomputed its static inputs on EVERY
    call — `_skill_exempt_roots()` alone (iterdir of ~/.ginno/projects plus
    one is_dir per project) plus realpath(home) and one realpath per mount —
    ~74µs per call, paid per directory AND per file in the glob/grep walks:
    walking this repo's ~158k files spent 21s in the check against 1.5s of
    actual traversal, blowing GLOB_TIME_BUDGET_S by itself. Everything that
    does not depend on the checked path is computed once here and reused via
    `_deny_policy`'s cache; branch order is reordered so paths OUTSIDE the
    sidecar home (the common repo case) never touch the skill-tree lookup.
    """

    __slots__ = ("always_deny", "extra_real", "home_real", "base_real", "own_ws_real", "_skill_real", "_skill_at")

    def __init__(self, base_dir: Path | None, extra_roots: list[Path] | None,
                 own_ws: Path | None = None):
        self.always_deny: list[str] = [_realpath_str(r) for r in _always_deny_roots()]
        self.extra_real: list[str] = sorted({_realpath_str(r) for r in (extra_roots or [])})
        self.home_real = _realpath_str(paths.home())
        self.base_real = _realpath_str(base_dir) if base_dir is not None else None
        # 当前会话自己的 workspace（uploads/产物/memory 所在地）。挂了
        # primary 挂载后 base_dir 指向挂载目录，但会话自己的目录仍应
        # 可读写（2026-11-10 实录：ec0c5453 读自己的 uploads/ 被拒）。
        # 与 base_real 同样的「home 的真子目录」防线上限：workspace==home
        # 的 workflow 兜底 cwd 不给豁免。
        _own = _realpath_str(own_ws) if own_ws is not None else None
        self.own_ws_real: str | None = (
            _own
            if _own is not None
            and _own != self.home_real
            and _own.startswith(self.home_real + os.sep)
            else None
        )
        self._skill_real: list[str] | None = None
        self._skill_at = 0.0

    def _skill_roots(self) -> list[str]:
        now = time.monotonic()
        if self._skill_real is None or now - self._skill_at > _SKILL_ROOTS_TTL_S:
            self._skill_real = sorted({_realpath_str(r) for r in _skill_exempt_roots()})
            self._skill_at = now
        return self._skill_real

    def denied(self, p: Path) -> bool:
        real = _realpath_str(p)
        for root in self.always_deny:
            if _inside(real, root):
                return True
        for root in self.extra_real:
            if _inside(real, root):
                return False  # explicitly mounted → reachable
        # Skill trees and session workspaces all live INSIDE the sidecar
        # home — a path outside it needs no further checks. This fast path is
        # what keeps repo-wide walks cheap.
        if not (real == self.home_real or real.startswith(self.home_real + os.sep)):
            return False
        if (
            self.base_real is not None
            # Exempt only a workspace that is a PROPER subdir of home (a real
            # session workspace). base_dir == home is the workflow cwd fallback
            # and must stay protected, else a cwd=home run reads all of ~/.ginno.
            and self.base_real != self.home_real
            and self.base_real.startswith(self.home_real + os.sep)
            and _inside(real, self.base_real)
        ):
            return False  # inside the active session workspace → allowed
        if (
            self.own_ws_real is not None
            and _inside(real, self.own_ws_real)
        ):
            return False  # inside the session's OWN workspace → allowed even
            # when base_dir was overridden by a primary mount
        for root in self._skill_roots():
            if _inside(real, root):
                return False  # skill trees are user content, not runtime secrets
        return True


_POLICY_CACHE: dict[tuple[str, ...], _DenyPolicy] = {}


def _deny_policy(base_dir: Path | None, extra_roots: list[Path] | None,
                 own_ws: Path | None = None) -> _DenyPolicy:
    """Per-(workspace, mounts) `_DenyPolicy`, built once and reused.

    api/code.py's tree/search fences call `_path_denied` with the same
    (workspace, mount_roots) pair, so they hit this cache too and get the
    same speedup for free. ``own_ws``（会话自己的 workspace，primary 挂载
    场景下与 base_dir 不同）也参与缓存键。
    """
    key = (
        str(base_dir) if base_dir is not None else "",
        *sorted(str(p) for p in (extra_roots or [])),
        str(own_ws) if own_ws is not None else "",
    )
    pol = _POLICY_CACHE.get(key)
    if pol is None:
        pol = _POLICY_CACHE[key] = _DenyPolicy(base_dir, extra_roots, own_ws)
    return pol


def _path_denied(p: Path, base_dir: Path | None, extra_roots: list[Path] | None = None,
                 own_ws: Path | None = None) -> bool:
    """True when ``p`` sits in a protected region (master-plan §2.3 baseline).

    Layers (semantics identical to the original inline version — see
    ``_DenyPolicy`` for why the static inputs are precomputed now):
    * ``_always_deny_roots`` (ssh / keychains) are denied unconditionally.
    * EXEMPT: the active session workspace ``base_dir`` AND any mounted
      context dir (``extra_roots``) — user-chosen work locations win over the
      blanket home-dir deny below.
    * the sidecar home (``paths.home()``, e.g. ~/.ginno) is denied otherwise:
      session workspaces live *inside* the home dir
      (``projects/<slug>/sessions/<id>/``), so without the exemption a
      blanket deny would break legitimate file tools, while checkpoints,
      settings and memory stay protected.
    * EXEMPT: skill trees (``~/.ginno/skills`` and
      ``~/.ginno/projects/<slug>/skills``). Skills are user-authored
      playbooks + companion scripts; the model must be able to read
      SKILL.md and ``python3`` them. Settings / sessions / mcp / keys
      stay behind the home deny.
    """
    return _deny_policy(base_dir, extra_roots, own_ws).denied(p)


def _deny_msg(p: str) -> str:
    return t(
        f"[error] path {p!r} is in a denied region (runtime data/credential "
        "directories are protected)",
        f"[error] 路径 {p!r} 在拒绝访问区域内（运行时数据/凭据目录受保护）")


def _base(workspace: str | None) -> Path:
    return Path(workspace).expanduser() if workspace else Path.cwd()


def _ws(base: Path, p: str) -> Path:
    return (base / p).resolve() if not Path(p).is_absolute() else Path(p)


# 内置文件写操作的进程级串行锁。langgraph ToolNode 会并发执行同一条 AIMessage
# 里的所有 tool call（asyncio.gather / 线程池），而 edit_file 是 read-modify-write、
# write_file 是 truncate-write，均无版本校验 —— 2026-10-08 事故：模型一条消息里
# 对同一文件发了 4 个 edit_file，四线程互相踩踏，文件被写成"缝合怪"（一整段替换
# 丢失 + 错位插入 + size=0 撕裂窗口），且每个调用各自返回 ok，工具层毫无冲突信号。
# 锁必须覆盖 edit 的 read→match→replace→write 全程：分两段拿锁仍会丢更新
# （B 在 A 的 read 和 write 之间写入，A 再用旧快照写回）。bash 不持锁——命令
# 任意且可能长跑，持锁会串行死全部会话；跨进程并发本就不在此机制的守备范围。
# 锁粒度（2026-10-17）：原来是全局一把，A 会话的大文件写会让 B 会话的 edit
# 在锁上排队——线程池共享本该「隔壁零感知」，这里成了唯一反例。改为按规范
# 路径（realpath，归一化 ../ 别名与挂载视角）建读写锁：read_file 拿读锁
# （同路径并发读互不阻塞），write/edit 拿写锁（写独占；edit 的 read 被
# 写锁覆盖——见上，read→replace→write 必须全程持锁）。写优先（等待中的
# 写者挡住新读者）防读者饿死。注册表用 WeakValueDictionary：无持有者即
# 蒸发（长跑 sidecar 读过几万个路径也不会累积锁对象）。安全性：guard 的
# bound method 持锁的强引用直到 with 退出，使用中的锁不可能被回收、也不
# 可能出现「同路径两把活锁」的竞态。
class _RWLock:
    """单进程读写锁：读共享、写独占、写优先。"""

    def __init__(self) -> None:
        self._cond = threading.Condition()
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    def acquire_read(self) -> None:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1

    def release_read(self) -> None:
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()

    def acquire_write(self) -> None:
        with self._cond:
            self._waiting_writers += 1
            while self._readers or self._writer:
                self._cond.wait()
            self._waiting_writers -= 1
            self._writer = True

    def release_write(self) -> None:
        with self._cond:
            self._writer = False
            self._cond.notify_all()

    def read_guard(self) -> "contextlib.AbstractContextManager[None]":
        return _guard(self.acquire_read, self.release_read)

    def write_guard(self) -> "contextlib.AbstractContextManager[None]":
        return _guard(self.acquire_write, self.release_write)


@contextlib.contextmanager
def _guard(acquire: Callable[[], None], release: Callable[[], None]):
    acquire()
    try:
        yield
    finally:
        release()


_PATH_RW_LOCKS: "weakref.WeakValueDictionary[str, _RWLock]" = weakref.WeakValueDictionary()
_PATH_RW_LOCKS_GUARD = threading.Lock()


def _path_rw_lock(p: Path) -> _RWLock:
    key = _realpath_str(p)
    with _PATH_RW_LOCKS_GUARD:
        lk = _PATH_RW_LOCKS.get(key)
        if lk is None:
            lk = _PATH_RW_LOCKS[key] = _RWLock()
        return lk


# bash 输出采集预算（2026-10-17）：capture_output=True 全量进内存——cat 2GB
# 日志先吞 2GB 再被 E2 截断（默认 20k/工具），OOM 发生在截断之前。改读线程
# 流式 head+tail 采集：head 攒满后只滚动保留 tail，中间段丢弃并计数，内存
# O(预算)。超时也不再丢弃已产出内容（旧行为超时只回一行字）。
BASH_OUT_HEAD = 20_000
BASH_OUT_TAIL = 10_000
BASH_ERR_HEAD = 10_000
BASH_ERR_TAIL = 5_000

# 中间段落盘（2026-11-07）：超预算被丢弃的 stdout 中间段流式写入 base_dir
# 下 ``.ginno-bash-out/``，省略行给出 read_file 分页取回提示——「中间省略」
# 从死路变成可翻页。仅 stdout 落盘（stderr 预算 10k+5k 小、翻页价值低，
# 决策记录：P0 只做 stdout）。文件是恢复窗口不是归档：目录内文件数与单文件
# 大小双重封顶，超限即删（最老先删）。
BASH_SPILL_DIR = ".ginno-bash-out"
BASH_SPILL_MAX_FILES = 20
BASH_SPILL_MAX_FILE_BYTES = 5 * 1024 * 1024


def _sweep_spill_dir(d: Path, keep: Path) -> None:
    """目录内文件数 > BASH_SPILL_MAX_FILES 时按 mtime 删最老（keep 刚创建不删）。

    best-effort：任何 OSError 静默跳过——清理失败绝不能砸掉正在执行的命令。
    """
    files: list[tuple[int, Path]] = []
    try:
        for p in d.iterdir():
            try:
                files.append((p.stat().st_mtime_ns, p))
            except OSError:
                continue
    except OSError:
        return
    if len(files) <= BASH_SPILL_MAX_FILES:
        return
    files.sort()
    excess = len(files) - BASH_SPILL_MAX_FILES
    for _, p in files:
        if excess <= 0:
            break
        if p == keep:
            continue
        try:
            p.unlink()
            excess -= 1
        except OSError:
            continue


def _open_spill(base_dir: Path) -> tuple | None:
    """懒创建一个新的中间段文件，返回 ``(fh, rel_path_text, abs_path)``。

    仅在读线程首次丢弃 chunk 时被调用一次（短命令不触发 → 零残留）。
    mkdir/open/清理任一失败 → None，调用方降级为纯省略行（goal：落盘失败
    绝不砸命令）。返回相对路径文本（相对 base_dir）供省略行与 read_file 直用。
    """
    try:
        d = base_dir / BASH_SPILL_DIR
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"bash-{uuid.uuid4().hex[:8]}.log"
        fh = p.open("w", encoding="utf-8", errors="replace")
    except OSError:
        return None
    _sweep_spill_dir(d, p)
    return fh, f"{BASH_SPILL_DIR}/{p.name}", p


def _abandon_spill(fh, abs_path: Path | None, state: dict) -> None:
    """写失败/超大小上限：关闭并删除半成品，state 退回纯省略行。"""
    try:
        fh.close()
    except OSError:
        pass
    if abs_path is not None:
        try:
            abs_path.unlink()
        except OSError:
            pass
    state["spill_path"] = None


def _drain_pipe(stream, cap_head: int, cap_tail: int, state: dict, spill_opener=None) -> None:
    """读线程主体。``state`` 由调用方在线程启动【前】建好、本线程原地更新
    （GIL 下字段读写原子），主线程 join 超时也能安全拿到部分快照——proc.
    kill 只杀直接子进程，握住管道不放的孙进程会让读线程迟迟不 EOF。

    ``spill_opener``（2026-11-07）：首个被丢弃的中间 chunk 出现时调用一次，
    返回 ``(fh, rel, abs)`` 或 None；此后每个被挤出 tail 窗口的 chunk 流式
    write+flush 到盘（join 超时也已可读）。写 OSError（磁盘满/权限）或累计
    超 BASH_SPILL_MAX_FILE_BYTES → 删除半成品、state["spill_path"] 置
    None，省略行退化为无恢复路径的旧格式——落盘永远只是增强，不是依赖。"""
    head: list = state["head"]
    tail: deque = state["tail"]
    spill = None
    spill_abs: Path | None = None
    spill_bytes = 0
    opener_called = False
    while True:
        chunk = stream.read(8192)
        if not chunk:
            break
        state["total"] += len(chunk)
        if state["head_len"] < cap_head:
            head.append(chunk)
            state["head_len"] += len(chunk)
        else:
            tail.append(chunk)
            state["tail_len"] += len(chunk)
            while tail and state["tail_len"] > cap_tail:
                state["tail_len"] -= len(tail[0])
                dropped = tail.popleft()
                if spill is None and not opener_called:
                    opener_called = True
                    if spill_opener is not None:
                        opened = spill_opener()
                        if opened is not None:
                            spill, state["spill_path"], spill_abs = opened
                if spill is None:
                    continue
                try:
                    spill.write(dropped)
                    spill.flush()  # join 超时也能从盘上读到已产出部分
                except OSError:
                    _abandon_spill(spill, spill_abs, state)
                    spill = None
                    spill_abs = None
                    continue
                spill_bytes += len(dropped)
                if spill_bytes > BASH_SPILL_MAX_FILE_BYTES:
                    _abandon_spill(spill, spill_abs, state)
                    spill = None
                    spill_abs = None
    if spill is not None:
        try:
            spill.close()
        except OSError:
            pass


def _compose_capped(state: dict, label: str) -> str:
    head_len = state["head_len"]
    if state["total"] <= head_len:
        return "".join(state["head"])
    dropped = state["total"] - head_len - state["tail_len"]
    omit = f"[{label}: 中间省略 {dropped} chars"
    spill_rel = state.get("spill_path")
    if spill_rel:
        # 建议 limit 用 head 的实际行数（同规模窗口）；chunk 边界与行边界无关，
        # 精确行号以 read_file 返回的 "lines a–b of N" 为准。
        head_lines = len("".join(state["head"]).splitlines()) or 1
        omit += (
            f" → 全量已存 {spill_rel}，read_file offset=1 limit={head_lines} 可取回"
        )
    return f"{''.join(state['head'])}\n{omit}]\n{''.join(state['tail'])}"


def _atomic_write_text(p: Path, text: str) -> None:
    """tempfile → os.replace 原子写。外部观察者（code panel / bash）因此不会
    读到 write_text 的截断窗口（事故里 edit 结果的 version 标记出现过
    ``0:<mtime>`` 的 stat 快照，即他线程刚 truncate 未写入的瞬间）。"""
    tmp = p.with_name(p.name + ".ginno-tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)
    finally:
        tmp.unlink(missing_ok=True)


def _code_marker(p: Path, op: str) -> str:
    """``<!--ginno-code:[...]-->`` trailer for a file this tool just wrote.

    Code-panel S3 (design §4.2-3): the marker is the transport for
    ``code.changed`` — the WS layer parses it, everyone else strips it (agent
    node, tool bubbles). ``op`` distinguishes ``write_file`` from ``edit_file``.

    ``version`` is re-``stat``-ed HERE, after the write, as
    ``f"{size}:{st_mtime_ns}"`` — byte-identical to what ``api/code.py``'s
    ``read``/``write`` return, which is what lets the panel reuse its conflict
    bar for a pushed change (brief §0). ``""`` when the stat fails (the tree
    mark is still worth emitting; the panel is merely conservative).
    """
    try:
        st = p.stat()
        version = f"{st.st_size}:{st.st_mtime_ns}"
    except OSError:
        version = ""
    return "\n" + encode_code_marker([{"path": str(p), "op": op, "version": version}])


def _baseline_snapshot(p: Path, session_id: str | None) -> None:
    """Code-panel live-diff baseline (docs/code-panel-diff-design.md §6.1).

    Snapshot the pre-write content of ``p`` on this session's FIRST write to it
    (files/baselines.py keeps the first-write-wins rule). Decoration only:
    never break a tool call over the snapshot.
    """
    if not session_id:
        return
    try:
        from ..files.baselines import snapshot_before_write

        snapshot_before_write(session_id, p)
    except Exception:  # noqa: BLE001 — best-effort decoration
        pass


def resolve_mounts(context_dirs: list[dict] | None) -> list[tuple[Path, str]]:
    """Turn serializable context-dir dicts into ``[(resolved_path, access)]``.

    ``missing`` entries and blank paths are skipped; the tier is ``rw`` only
    when explicitly requested, so an unknown value degrades to the safe ``ro``.
    Module-level (not a closure) so the code panel resolves the exact same
    mount set as the tools do — one implementation, no drift.
    """
    mounts: list[tuple[Path, str]] = []
    for d in context_dirs or []:
        if not isinstance(d, dict) or d.get("missing"):
            continue
        raw = (d.get("path") or "").strip()
        if not raw:
            continue
        try:
            rp = Path(raw).expanduser().resolve()
        except OSError:
            continue
        mounts.append((rp, "rw" if d.get("access") == "rw" else "ro"))
    return mounts


def mount_access(p: Path, mounts: list[tuple[Path, str]]) -> str | None:
    """Access tier of the MOST SPECIFIC mount containing ``p`` (or None).

    Nested mounts keep their own tier: an ``rw`` dir inside an ``ro`` mount
    wins for paths under it because the longest matching prefix is chosen.
    """
    try:
        real = os.path.realpath(p)
    except OSError:
        return None
    best: str | None = None
    best_len = -1
    for mp, acc in mounts:
        r = str(mp)
        if _inside(real, r) and len(r) > best_len:
            best, best_len = acc, len(r)
    return best


def _grep_via_rg(
    pattern: str, search_root: Path, max_hits: int, include_ignored: bool, context: int = 0
) -> list[str] | None:
    """Delegate grep_files' search to ripgrep when it is on PATH.

    Returns ``rel:line:text`` hit lines (with a trailing ``... (truncated)``
    marker when capped), or ``None`` when rg is unavailable or failed — the
    caller then runs its Python walk, which re-reports a genuinely invalid
    pattern as ``[error] invalid regex`` itself.

    Flags pin rg to the Python fallback's semantics: ``--hidden`` (the walk
    visits dotfiles), ``--no-ignore`` (.gitignore is NOT honoured — the
    fallback can't honour it either), ``--max-filesize`` mirrors
    GREP_MAX_FILE_BYTES, and the ``_WALK_IGNORE_DIRS`` prune is expressed as
    ``-g '!dir/**'`` globs so both paths skip the same trees. rg never
    follows symlinks — matching the fallback, which skips them too.

    Output is read on a daemon thread pumping a queue, so a silent rg on a
    slow/huge tree still hits the GLOB_TIME_BUDGET_S wall instead of
    blocking the tool call forever.
    """
    rg = shutil.which("rg")
    if not rg:
        return None
    cmd = [
        rg,
        "--json",
        "--hidden",
        "--no-ignore",
        "--max-filesize", f"{GREP_MAX_FILE_BYTES // (1024 * 1024)}M",
    ]
    if context > 0:
        cmd += ["-C", str(context)]
    if not include_ignored:
        for d in sorted(_WALK_IGNORE_DIRS):
            cmd += ["-g", f"!{d}/**"]
    cmd += ["-e", pattern, str(search_root)]
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            errors="replace",
            # rg 的 -g glob 按相对 cwd 的路径匹配：cwd 不落在搜索根下时 rg
            # 走绝对路径，`!node_modules/**` 这类相对剪枝静默失效
            # （2026-10-20：test_grep_prunes_build_dirs_by_default 从别的
            # cwd 跑必漏 node_modules）。钉在搜索根上，glob/输出路径双稳。
            cwd=search_root,
        )
    except OSError:
        return None
    assert proc.stdout is not None
    lines: queue.Queue[str | None] = queue.Queue()

    def _pump() -> None:
        try:
            for raw in proc.stdout:  # type: ignore[union-attr]
                lines.put(raw)
        finally:
            lines.put(None)

    threading.Thread(target=_pump, daemon=True).start()
    hits: list[str] = []
    match_count = 0
    truncated = False
    t0 = time.monotonic()
    try:
        while True:
            try:
                raw = lines.get(timeout=max(0.05, GLOB_TIME_BUDGET_S - (time.monotonic() - t0)))
            except queue.Empty:
                hits.append(
                    f"... (search budget exhausted after {time.monotonic() - t0:.1f}s — "
                    "narrow the pattern or pass a smaller root=)"
                )
                return hits
            if raw is None:
                break
            try:
                obj = json.loads(raw)
            except ValueError:
                continue
            evtype = obj.get("type")
            if evtype not in ("match", "context"):
                continue
            data = obj.get("data") or {}
            ptext = (data.get("path") or {}).get("text")
            if ptext is None:
                continue  # non-UTF-8 path ({"bytes": …}) — skip like a binary
            lnum = data.get("line_number")
            ltext = ((data.get("lines") or {}).get("text") or "").rstrip("\r\n")
            try:
                rel = str(Path(ptext).relative_to(search_root))
            except ValueError:
                rel = ptext
            # context 行（rg -C）：行号后缀 "-" 与 match 区分（rg 文本风格）
            mark = "-" if evtype == "context" else ""
            hits.append(f"{rel}:{lnum}{mark}:{ltext}")
            # max_hits 只数 match——context 行是陪同不是命中，不占配额
            if evtype == "match":
                match_count += 1
                if match_count >= max_hits:
                    truncated = True
                    break
    finally:
        proc.kill()
        proc.wait()
    if proc.returncode not in (0, 1) and not hits:
        # rc 2: rg-level error (unreadable root, or a Python-only regex
        # construct like lookahead that Rust's regex cannot compile) — let
        # the Python walk decide what the right error/result is.
        return None
    if truncated:
        hits.append("... (truncated)")
    return hits


def build_builtin_tools(
    workspace: str | None = None,
    context_dirs: list[dict] | None = None,
    primary_path: str | None = None,
    observe_path: Callable[[Path], str | None] | None = None,
    session_id: str | None = None,
) -> list:
    """Build the six file/shell tools with ``workspace`` (and optionally the
    session's mounted context dirs) bound in.

    ``session_id`` (when given) keys the code-panel live-diff baselines
    (files/baselines.py): write_file / edit_file snapshot the pre-write content
    on a file's FIRST agent write of the session.

    Called per session (create_session / _ensure_session / mount changes) so
    each session's graph carries tools scoped to its own dirs. Callers
    without a workspace (workflow engine, unit tests) get the process cwd
    fallback — the pre-F1 behaviour.

    ``context_dirs`` items are the serializable dicts from
    ``context_folders.resolve_session_dirs`` (path/access/missing); entries
    marked ``missing`` are ignored. ``primary_path`` (a mounted dir's path)
    switches the relative-path base + bash cwd to that dir.

    ``observe_path`` is the project-discovery hook (see ``projects.py``): every
    absolute path these tools touch is offered to it, and a non-empty return is
    appended to the tool result. This is how a repo the agent only ever reached
    by absolute path — never mounted — becomes visible in the SAME turn.
    """
    ws_root = _base(workspace)

    def _observe(p) -> str:
        """Never let discovery break a tool call."""
        if observe_path is None:
            return ""
        try:
            return observe_path(Path(p)) or ""
        except Exception:
            return ""

    # Mounts → [(resolved_path, access)] — most-specific match wins for
    # nested mounts (an rw dir inside an ro mount keeps its own tier).
    mounts: list[tuple[Path, str]] = resolve_mounts(context_dirs)

    base_dir = ws_root
    if primary_path:
        cand = Path(primary_path).expanduser()
        if cand.is_dir():
            base_dir = cand

    # Mounts only for _path_denied extra_roots — the session workspace is
    # exempted separately (and ONLY when it is a proper subdir of ~/.ginno).
    # Putting ws_root into extra_roots would punch a hole through the home
    # deny whenever workspace == home (workflow cwd fallback).
    mount_roots: list[Path] = [p for p, _ in mounts]
    # Workspace + mounts: used only to validate an explicit glob/grep `root=`.
    search_roots: list[Path] = [ws_root] + mount_roots

    def _ro_write_msg(p: str) -> str:
        return t(
            f"[error] path {p!r} is inside a read-only mounted directory and "
            "cannot be written. Read-only dirs are for read_file / grep_files / "
            "glob_files reference only; to modify files there, ask the user to "
            "remount it as read-write.",
            f"[error] 路径 {p!r} 位于只读挂载目录内，不可写入。"
            "只读目录仅供 read_file / grep_files / glob_files 参考；"
            "如需修改请让用户将其改为读写挂载。")

    @tool
    def read_file(path: str, offset: int = 1, limit: int = 0) -> str:
        """Read a UTF-8 text file. Relative paths resolve against the session
        working directory. ``offset`` (1-based line number) and ``limit`` (max
        line count) paginate long files; without them a file longer than 2000
        lines / 200k chars returns only its head plus a ``pass offset=… to
        continue`` note, so one huge read cannot flood the context. Returns
        contents or an ``[error]`` string.

        流式读取（2026-10-17）：按行窗口扫描，不再整文件 read_text 进内存
        （200MB 日志从「白吞 200MB 再截断」变 O(窗口) 内存）；行计数器零保留
        走完全程，「lines a–b of N」提示与旧行为逐字节一致。读期间持路径
        读锁（与 write/edit 的写锁互斥，读者之间共享）。"""
        try:
            p = _ws(base_dir, path)
            if _path_denied(p, base_dir, mount_roots, ws_root):
                return _deny_msg(path)
            start = max(int(offset), 1) - 1
            cap = READ_HARD_MAX_LINES if limit else READ_MAX_LINES
            take = min(max(int(limit), 1), cap) if limit else READ_MAX_LINES
            # 窗口行（已去行尾 \n）；chars 按原始行（含 \n）计，等于旧行为的
            # len(text)。window_done 之后只计数不保留——total 需要 EOF。
            lines: list[str] = []
            chars = 0
            ends_nl = False
            window_done = False
            last_i = -1
            with _path_rw_lock(p).read_guard(), p.open(
                "r", encoding="utf-8", errors="replace"
            ) as fh:
                for last_i, raw in enumerate(fh):
                    if last_i < start:
                        continue
                    if window_done:
                        continue  # 只计数
                    if lines and chars + len(raw) > READ_MAX_CHARS:
                        # Char guard（一条巨行会顶穿行数帽）：整行切，至少保一行
                        window_done = True
                        continue
                    ends_nl = raw.endswith("\n")
                    lines.append(raw[:-1] if ends_nl else raw)
                    chars += len(raw)
                    if len(lines) >= take:
                        window_done = True
            total = last_i + 1
        except FileNotFoundError:
            return f"[error] file not found: {path}"
        except OSError as e:
            return f"[error] cannot read {path}: {type(e).__name__}: {e}"
        # Fast path — small file, no pagination: byte-identical to the
        # pre-pagination behaviour (raw text, trailing newline included).
        if offset <= 1 and not limit and total <= READ_MAX_LINES and chars <= READ_MAX_CHARS:
            return "\n".join(lines) + ("\n" if lines and ends_nl else "") + _observe(p)
        if start >= total:
            return f"[error] offset {offset} is beyond the end of the file ({total} lines)"
        body = "\n".join(lines)
        if start + len(lines) < total:
            body += (
                f"\n... (lines {start + 1}-{start + len(lines)} of {total} — "
                f"pass offset={start + len(lines) + 1} to continue)"
            )
        return body + _observe(p)

    @tool
    def write_file(path: str, content: str) -> str:
        """Write text content to a file (overwrite). Relative paths resolve
        against the session working directory.

        Never raises into the graph: an unwritable path (e.g. the knowledge raw dir
        resolving outside the workspace, a read-only mount, or a read-only
        parent) returns an ``[error]`` tool result so the agent can react,
        instead of crashing the whole turn.
        """
        p = _ws(base_dir, path)
        if _path_denied(p, base_dir, mount_roots, ws_root):
            return _deny_msg(path)
        if mount_access(p, mounts) == "ro":
            return _ro_write_msg(path)
        try:
            with _path_rw_lock(p).write_guard():
                _baseline_snapshot(p, session_id)
                p.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write_text(p, content)
        except OSError as e:
            return f"[error] cannot write {p}: {type(e).__name__}: {e}"
        # ``_code_marker`` before the discovery notes, like bash's image marker:
        # it describes THIS call's effect, the notes are a side-channel.
        return f"wrote {len(content)} bytes to {p}" + _code_marker(p, "write") + _observe(p)

    def _resolve_search_root(root: str) -> Path | str:
        """Validate an explicit glob/grep root: it must sit inside the session
        workspace or a mounted dir. Returns the resolved Path or an error."""
        rp = _ws(base_dir, (root or "").strip())
        if not rp.is_dir():
            return t(f"[error] root is not a directory: {root}",
                     f"[error] root 不是目录: {root}")
        try:
            real = os.path.realpath(rp)
        except OSError:
            real = str(rp)
        for rr in search_roots:
            try:
                rr_real = os.path.realpath(rr)
            except OSError:
                continue
            if _inside(real, rr_real):
                return rp
        avail = ", ".join(str(rr) for rr in search_roots)
        return t(
            f"[error] root {root!r} is outside this session's reachable "
            f"directories (working dir/mounted dirs: {avail})",
            f"[error] root {root!r} 不在本会话的可达目录内（工作目录/挂载目录：{avail}）")

    @tool
    def glob_files(pattern: str, root: str = "", include_ignored: bool = False) -> str:
        """Glob files under the session working directory (recursive), or
        under an explicit ``root`` (must be the workspace or a mounted
        context dir — use ``root`` to search a mounted folder). Returns a
        newline-joined list of paths relative to the searched root, capped at
        500 matches. Dependency/build dirs (``.git``, ``node_modules``,
        ``target``, ``dist``, ``__pycache__``, …) are skipped unless
        ``include_ignored`` is true. Never raises — a bad base dir or pattern
        yields an ``[error]`` string. Hard-denied dirs (runtime data /
        credentials) are pruned and never scanned (master-plan §2.3).
        Prefer this over `bash ls/find` when locating files: scoped root,
        match caps, dependency dirs already pruned."""
        import fnmatch

        try:
            search_root = base_dir
            if (root or "").strip():
                resolved = _resolve_search_root(root)
                if isinstance(resolved, str):
                    return resolved
                search_root = resolved
            if not search_root.is_dir():
                return f"[error] workspace is not a directory: {search_root}"
            guarded = _fs_root_guard(search_root)
            if guarded:
                return guarded
            policy = _deny_policy(base_dir, mount_roots, ws_root)
            hits: list[str] = []
            truncated = False
            budget_note = ""
            _t0 = time.monotonic()
            _dirs_seen = 0
            try:
                for r, dirs, files in os.walk(search_root):
                    _dirs_seen += 1
                    if _dirs_seen > GLOB_MAX_DIRS or time.monotonic() - _t0 > GLOB_TIME_BUDGET_S:
                        truncated = True
                        budget_note = (
                            f"... (search budget exhausted after {_dirs_seen} dirs / "
                            f"{time.monotonic() - _t0:.1f}s — narrow the pattern or pass a "
                            "smaller root=)"
                        )
                        break
                    # Prune hard-denied directories (never traversed) and —
                    # unless the caller opted in — dependency/build dirs.
                    dirs[:] = [
                        d
                        for d in dirs
                        if (include_ignored or d not in _WALK_IGNORE_DIRS)
                        and not policy.denied(Path(r) / d)
                    ]
                    for name in files:
                        fp = Path(r) / name
                        # The directory verdict already covers regular files
                        # (a non-symlink's realpath cannot leave its parent);
                        # only a symlink can point into a denied region, so
                        # only symlinks pay the full check.
                        if fp.is_symlink() and policy.denied(fp):
                            continue
                        rel = str(fp.relative_to(search_root))
                        try:
                            if (
                                not fnmatch.fnmatch(rel, pattern)
                                and not fnmatch.fnmatch(name, pattern)
                            ):
                                continue
                        except Exception:
                            continue
                        hits.append(rel)
                        if len(hits) >= GLOB_MAX_HITS:
                            truncated = True
                            break
                    if truncated:
                        break
            except (OSError, ValueError) as e:
                if not hits:
                    return f"[error] glob failed: {type(e).__name__}: {e}"
            hits.sort()
            header = f"(root: {search_root})\n" if str(search_root) != str(base_dir) else ""
            out = header + "\n".join(hits)
            if truncated:
                out += f"\n... (truncated at {GLOB_MAX_HITS} matches)"
            if budget_note:
                out += f"\n{budget_note}"
            return out or "(no matches)"
        except Exception as e:  # noqa: BLE001 — last-resort guard: never raise
            return f"[error] glob failed: {type(e).__name__}: {e}"

    @tool
    def grep_files(
        pattern: str,
        max_hits: int = 50,
        root: str = "",
        include_ignored: bool = False,
        context: int = 0,
    ) -> str:
        """Grep (regex) files under the session working directory (recursive),
        or under an explicit ``root`` (must be the workspace or a mounted
        context dir — use ``root`` to search a mounted folder). Returns
        ``file:line:match`` lines, capped at ``max_hits``. Dependency/build
        dirs (``.git``, ``node_modules``, ``target``, …) are skipped unless
        ``include_ignored`` is true; binary files, files over 5 MB and
        symlinked files are never read. When ripgrep (``rg``) is installed
        the search is delegated to it. Never raises — a bad pattern or base
        dir yields an ``[error]`` string.
        Prefer this over `bash grep` when scanning the workspace: scoped
        root, hit caps, no shell-quoting pitfalls, output already compact.
        ``context=N`` (0–10) includes N surrounding lines per hit (like
        ``rg -C`` / ``grep -C``), marked ``file:line-:text`` — read code
        around a hit directly here instead of a follow-up bash call."""
        import re

        try:
            rx = re.compile(pattern)
        except re.error as e:
            return f"[error] invalid regex {pattern!r}: {e}"
        try:
            search_root = base_dir
            if (root or "").strip():
                resolved = _resolve_search_root(root)
                if isinstance(resolved, str):
                    return resolved
                search_root = resolved
            if not search_root.is_dir():
                return f"[error] workspace is not a directory: {search_root}"
            guarded = _fs_root_guard(search_root)
            if guarded:
                return guarded
            header = f"(root: {search_root})" if str(search_root) != str(base_dir) else ""
            ctx_n = max(0, min(10, int(context or 0)))
            rg_hits = _grep_via_rg(pattern, search_root, max_hits, include_ignored, ctx_n)
            if rg_hits is not None:
                body = "\n".join(rg_hits) or "(no matches)"
                return (header + "\n" + body) if header else body
            # --- Python fallback (no rg on PATH, or rg failed) ---
            policy = _deny_policy(base_dir, mount_roots, ws_root)
            out: list[str] = []
            _t0 = time.monotonic()
            _dirs_seen = 0
            for r, dirs, files in os.walk(search_root):
                _dirs_seen += 1
                if _dirs_seen > GLOB_MAX_DIRS or time.monotonic() - _t0 > GLOB_TIME_BUDGET_S:
                    out.append(
                        f"... (search budget exhausted after {_dirs_seen} dirs / "
                        f"{time.monotonic() - _t0:.1f}s — narrow the pattern or pass a "
                        "smaller root=)"
                    )
                    return (header + "\n" if header else "") + "\n".join(out)
                # Prune hard-denied directories (master-plan §2.3) and —
                # unless the caller opted in — dependency/build dirs.
                dirs[:] = [
                    d
                    for d in dirs
                    if (include_ignored or d not in _WALK_IGNORE_DIRS)
                    and not policy.denied(Path(r) / d)
                ]
                for name in files:
                    f = Path(r) / name
                    # Symlinks can point outside the root (rg never follows
                    # them either); regular files inherit the directory's
                    # deny verdict — their realpath cannot leave its parent.
                    if f.is_symlink():
                        continue
                    try:
                        if f.stat().st_size > GREP_MAX_FILE_BYTES:
                            continue
                        data = f.read_bytes()
                    except OSError:
                        continue
                    if b"\x00" in data:
                        continue  # binary — never searched
                    text = data.decode("utf-8", errors="ignore")
                    all_lines = text.splitlines()
                    emitted: set[int] = set()
                    hits_in_file = 0
                    for i, line in enumerate(all_lines, 1):
                        if not rx.search(line):
                            continue
                        hits_in_file += 1
                        # context 窗口（rg -C 语义）：前后各 ctx_n 行，
                        # 行号带 "-" 与 match 区分；连续命中的窗口自然合并
                        lo = max(1, i - ctx_n)
                        hi = min(len(all_lines), i + ctx_n)
                        for j in range(lo, hi + 1):
                            if j in emitted:
                                continue
                            emitted.add(j)
                            mark = "" if j == i else "-"
                            out.append(f"{f.relative_to(search_root)}:{j}{mark}:{all_lines[j - 1]}")
                        if hits_in_file >= max_hits:
                            out.append("... (truncated)")
                            return (header + "\n" if header else "") + "\n".join(out)
            body = "\n".join(out) or "(no matches)"
            return (header + "\n" + body) if header else body
        except Exception as e:  # noqa: BLE001 — last-resort guard: never raise
            return f"[error] grep failed: {type(e).__name__}: {e}"

    @tool
    def edit_file(path: str, old: str, new: str) -> str:
        """Replace a unique occurrence of `old` with `new` in a file."""
        p = _ws(base_dir, path)
        if _path_denied(p, base_dir, mount_roots, ws_root):
            return _deny_msg(path)
        if mount_access(p, mounts) == "ro":
            return _ro_write_msg(path)
        with _path_rw_lock(p).write_guard():
            try:
                text = p.read_text(encoding="utf-8")
            except FileNotFoundError:
                return f"[error] file not found: {path}"
            except OSError as e:
                return f"[error] cannot read {path}: {type(e).__name__}: {e}"
            # find instead of count×2: one scan locates the match and checks
            # for a second one (huge files paid two full passes per edit).
            idx = text.find(old)
            if idx < 0:
                return "[error] not found"
            if text.find(old, idx + len(old)) != -1:
                return "[error] multiple matches"
            try:
                _baseline_snapshot(p, session_id)
                _atomic_write_text(p, text[:idx] + new + text[idx + len(old):])
            except OSError as e:
                return f"[error] cannot write {path}: {type(e).__name__}: {e}"
        # Same ``<!--ginno-code:-->`` trailer as write_file, with op="edit" so
        # the panel can tell an in-place edit from a full overwrite.
        return "ok" + _code_marker(p, "edit") + _observe(p)

    @tool
    def bash(command: str, timeout: int = 30) -> str:
        """Run a shell command with the session working directory (the primary
        context folder when one is set) as cwd. Returns stdout+stderr, capped
        head+tail（超预算中间段省略计数；stdout 的被省略中间段会存入工作目录
        ``.ginno-bash-out/``，省略行附 read_file offset/limit 取回提示；超时
        保留已产出内容）. Never raises.

        Prefer built-in directory args (`git -C <path> …`, `rg <pattern>
        <path>`, `pytest <path>`) over `cd <path> && …` — simpler commands
        keep output handling consistent.

        Shell gotchas worth knowing: a pipeline's exit code is the LAST
        command's — an rg failure behind ``| head`` shows up only in the
        stderr section (known rg input errors get a trailing [hint]);
        rg's default engine (Rust regex) has no backreferences or
        look-around, add ``--pcre2`` when a pattern needs them; run ad-hoc
        node scripts from inside the repo (ESM resolves imports from the
        script's own directory, so /tmp scripts cannot see the repo's
        node_modules)."""
        # Weak hard-deny guard (master-plan §2.3 decision-6 interim): scan the
        # command's path-like tokens through the same _path_denied check
        # (which exempts the workspace + mounted dirs). Not a sandbox —
        # dynamically built paths bypass this; full bash policy is TBD.
        from pathlib import Path as _P

        notes: list[str] = []
        for tok in command.split():
            tok = tok.strip("'\"`;|&")
            if not tok or not (tok.startswith("/") or tok.startswith("~")):
                continue
            candidate = _P(tok).expanduser()
            # `source ~/.zshrc` (and friends) must be allowed — GUI sidecars
            # don't inherit an interactive login env, so skills load user
            # exports this way. ~/.ssh / ~/.ginno stay denied.
            if _is_user_rc(candidate):
                continue
            if _path_denied(candidate, base_dir, mount_roots, ws_root):
                return _deny_msg(tok)
            # Project discovery: a repo reached only by absolute path in a bash
            # command is exactly the 2026-09-17 case (git clone into a repo the
            # agent had scaffolded) — it must become visible in this same turn.
            n = _observe(candidate)
            if n and n not in notes:
                notes.append(n)
        cwd = str(base_dir) if base_dir.is_dir() else None
        # Snapshot workspace images so we can detect pictures the command
        # generates (e.g. matplotlib savefig) and surface them inline in the
        # chat. Detection is workspace-scoped and display-only — the marker
        # is stripped from the model's view by the agent node.
        img_before = snapshot_images(base_dir) if cwd else {}
        try:
            # Prefer the user's login shell so PATH / exports from zshrc
            # (or bashrc) are present even when Ginno was launched from
            # Finder and the sidecar inherited a bare GUI environment.
            # Fall back to /bin/sh -lc if $SHELL is missing.
            user_shell = os.environ.get("SHELL") or "/bin/sh"
            # rtk 集成（tools/rtk_bridge.py）：白名单简单命令且本机装有 rtk 时
            # 改写为 `rtk <命令>` 压缩输出；未命中/无 rtk/开关关闭时原样执行。
            # 输出仍走下方 head+tail 采集链路，行为对模型透明。
            from .rtk_bridge import rewrite as _rtk_rewrite

            effective_command = _rtk_rewrite(command) or command
            # 环境变量注入（tools/env_inject.py）：settings.json 的 "env"
            # 键合并进子进程 env（GUI 启动不继承代理等场景）；无需注入时
            # 返回 None = 沿用继承语义，行为零变化。
            from .env_inject import build_child_env

            proc = subprocess.Popen(
                [user_shell, "-lc", effective_command],
                cwd=cwd,
                env=build_child_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as e:
            return f"[error] cannot execute: {type(e).__name__}: {e}"
        # 流式 head+tail 采集（预算见 _drain_pipe 上方注释）：state 先建好
        # 再起读线程，原地更新；join 超时（孙进程握管道）也拿得到部分快照。
        # stdout 的被丢弃中间段懒落盘到 base_dir/.ginno-bash-out/（短命令
        # 零残留），stderr 不落盘（预算小、价值低）。
        so = {
            "head": [], "tail": deque(), "head_len": 0,
            "tail_len": 0, "total": 0, "spill_path": None,
        }
        se = {"head": [], "tail": deque(), "head_len": 0, "tail_len": 0, "total": 0}
        spill_opener = (lambda: _open_spill(base_dir)) if cwd else None
        readers = [
            threading.Thread(
                target=_drain_pipe,
                args=(proc.stdout, BASH_OUT_HEAD, BASH_OUT_TAIL, so, spill_opener),
                daemon=True,
            ),
            threading.Thread(
                target=_drain_pipe, args=(proc.stderr, BASH_ERR_HEAD, BASH_ERR_TAIL, se), daemon=True
            ),
        ]
        for t in readers:
            t.start()
        timed_out = False
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            proc.kill()
            rc = proc.wait()
        for t in readers:
            t.join(10)
        out = (
            f"[exit {rc}]\n{_compose_capped(so, 'stdout')}"
            f"\n--- stderr ---\n{_compose_capped(se, 'stderr')}"
        )
        # stderr head+tail 重组后过 rg 输入类报错分类（见 _RG_USAGE_ERROR_RE
        # 注释）：管道把失败掩成 exit 0 时，靠这段 [hint] 显式暴露。
        hint = _rg_usage_hint("".join(se["head"]) + "".join(se["tail"]))
        if hint:
            out += f"\n{hint}"
        if timed_out:
            out = f"[timeout after {timeout}s — partial output kept]\n{out}"
        if cwd:
            new_imgs = diff_images(img_before, snapshot_images(base_dir))
            if new_imgs:
                out += "\n" + encode_images_marker(new_imgs)
        return out + "".join(notes)

    return [read_file, write_file, glob_files, grep_files, edit_file, bash]
