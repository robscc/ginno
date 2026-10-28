"""TaskRegistry：后台任务进程管理（background-tasks-design.md §2 D1/D4）。

每个任务 = Popen + 一个 reader 线程（spool 落盘 + watch 行匹配）+ reaper
轮询（退出/超时/detached 认领/watch 事件投递）。进程语义：

- 普通（默认）：sidecar 子进程，app 退出即死，重启对账标 interrupted（D4）。
- detached（P2）：start_new_session 独立进程组 + wrapper 把退出码落盘到
  exit_code 文件；sidecar 死后进程继续跑，重启按 exit 文件/pid 存活认领。

注入（完成/healthy/watch）由 inject 模块在 reaper 的 asyncio 上下文里触发
——registry 只管进程与状态机，消息怎么进 session 是 inject 的事。
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from typing import Any

from . import store

_log = logging.getLogger("ginno.tasks")

# watch 事件节流（P2 pattern watch）：同任务两次注入最小间隔 + 总量上限，
# 超限自动静音（一次终局通知）——防 firehose（Monitor 同款教训）。
WATCH_MIN_INTERVAL_S = 15.0
WATCH_MAX_EVENTS = 50
STOP_GRACE_S = 2.0


def _fmt_dur(secs: float) -> str:
    s = max(0, int(secs))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h {m}m"
    if m:
        return f"{m}m {sec:02d}s"
    return f"{sec}s"


def _user_shell() -> str:
    # 与 tools/builtin.bash 同口径：GUI sidecar 没有 login env，优先用户
    # shell 的 -lc，让 zshrc 的 PATH/exports 可见。
    return os.environ.get("SHELL") or "/bin/sh"


class _WatchState:
    """一个 watch 条目的运行时状态（持久化只存定义，不存计数）。"""

    __slots__ = ("pattern", "label", "every", "fired", "count")

    def __init__(self, pattern: str, label: str, every: bool) -> None:
        self.pattern = pattern
        self.label = label
        self.every = every
        self.fired = False
        self.count = 0

    def to_record(self) -> dict:
        return {"pattern": self.pattern, "label": self.label, "every": self.every}


class Task:
    """运行时任务对象：持久化 record + 进程/线程把手 + watch 状态。"""

    def __init__(self, record: dict) -> None:
        self.record = record
        self.proc: subprocess.Popen | None = None
        self.reader: threading.Thread | None = None
        self.watches: list[_WatchState] = []
        if record.get("healthy_pattern"):
            self.watches.append(_WatchState(record["healthy_pattern"], "healthy", False))
        for w in record.get("watches") or []:
            if isinstance(w, dict) and w.get("pattern"):
                self.watches.append(
                    _WatchState(str(w["pattern"]), str(w.get("label") or ""), bool(w.get("every")))
                )
        # reader → reaper 的匹配行队列（线程安全；注入只在 asyncio 侧发生）
        self.watch_q: "queue.Queue[tuple[str, str]]" = queue.Queue()
        self.last_watch_at = 0.0
        self.watch_events = 0
        self.watch_muted = record.get("watch_muted") or False
        self.pending_stop = False
        # detached 认领：非子进程，只能轮询 exit 文件 + pid 存活
        self.adopted = False
        self.stopping = False
        # remove() 置位：reader 见此旗即停，避免给已删目录重建 spool
        self.removed = False


class TaskRegistry:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._reaper: Any = None  # asyncio.Task
        self._running_loop: Any = None
        self.started = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """lifespan 启动：载入索引 + detached 认领 + 起 reaper。"""
        import asyncio

        records = store.read_index()
        store.startup_migrate(records)
        self._adopt_detached(records)
        with self._lock:
            self._tasks.clear()
            for tid, rec in records.items():
                if rec.get("status") == store.STATUS_RUNNING:
                    t = Task(rec)
                    t.adopted = bool(rec.get("detached"))
                    self._tasks[tid] = t
        self._running_loop = asyncio.get_running_loop()
        self._reaper = asyncio.get_running_loop().create_task(self._reap_loop())
        self.started = True
        _log.info("tasks_registry_started live=%d", len(self._tasks))

    def _adopt_detached(self, records: dict[str, dict]) -> None:
        """P2 认领：adoptable（detached 且重启前 running）按事实翻状态。"""
        now = time.time()
        for tid, rec in list(records.items()):
            if not rec.get("adoptable"):
                continue
            exit_code: int | None = None
            try:
                raw = store.exit_code_path(tid).read_text().strip()
                exit_code = int(raw)
            except (OSError, ValueError):
                pass
            pid = rec.get("pid")
            alive = False
            if pid:
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except (ProcessLookupError, PermissionError, ValueError, TypeError):
                    alive = False
            if exit_code is not None:
                rec["status"] = store.STATUS_FINISHED
                rec["exit_code"] = exit_code
                rec["ended_at"] = rec.get("ended_at") or now
                rec["dur"] = _fmt_dur((rec["ended_at"] or now) - (rec.get("started_at") or now))
                rec["adoptable"] = False
            elif alive:
                # 进程还在跑：翻回 running，reaper 轮询 exit 文件收尾。
                rec["status"] = store.STATUS_RUNNING
                rec["interrupted_note"] = ""
                _log.info("task_adopted task=%s pid=%s", tid, pid)
            else:
                # 进程没了且没留退出码（被 SIGKILL / 机器重启）：保持
                # interrupted，但注明不是本次 app quit 杀的。
                rec["adoptable"] = False
                rec["interrupted_note"] = "process gone (no exit code recorded)"
            store.write_meta(tid, rec)
        store.write_index(records)

    async def stop_all(self) -> None:
        """lifespan 关闭：全体 SIGTERM，宽限后 SIGKILL（D4：子进程随 sidecar 死）。"""
        with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            self._signal(t, signal.SIGTERM)
        deadline = time.time() + STOP_GRACE_S
        for t in tasks:
            remaining = max(0.0, deadline - time.time())
            if t.proc is not None:
                try:
                    t.proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    self._signal(t, signal.SIGKILL)
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None
        self.started = False
        _log.info("tasks_registry_stopped")

    # ------------------------------------------------------------------ #
    # spawn / query
    # ------------------------------------------------------------------ #

    def spawn(
        self,
        *,
        command: str,
        title: str,
        cwd: str | None,
        origin_session: str | None,
        origin_turn: str | None,
        notify: bool = True,
        timeout_s: int | None = None,
        mode: str = "run",
        healthy: str = "",
        watch: list[dict] | None = None,
        detached: bool = False,
    ) -> dict:
        """起一个后台任务，立即返回 record（失败抛 RuntimeError/ValueError）。"""
        if mode == "server" and not healthy:
            raise ValueError('mode="server" requires healthy= (the ready line to match)')
        cfg = store.config()
        timeout_s = cfg["default_timeout_s"] if timeout_s is None else int(timeout_s)
        with self._lock:
            live = sum(1 for t in self._tasks.values() if t.record.get("status") == store.STATUS_RUNNING)
            if live >= int(cfg["max_concurrent"]):
                raise RuntimeError(
                    f"max_concurrent={cfg['max_concurrent']} reached; stop or wait for a running task first"
                )
            task_id = "t_" + uuid.uuid4().hex[:8]
            now = time.time()
            record = {
                "id": task_id,
                "command": command,
                "title": title,
                "cwd": cwd or "",
                "status": store.STATUS_RUNNING,
                "exit_code": None,
                "pid": None,
                "origin_session": origin_session,
                "origin_turn": origin_turn,
                "notify": bool(notify),
                "mode": mode,
                "healthy_pattern": healthy or "",
                "watches": [
                    {"pattern": w.get("pattern"), "label": w.get("label") or "", "every": bool(w.get("every"))}
                    for w in (watch or [])
                    if isinstance(w, dict) and w.get("pattern")
                ],
                "timeout_s": timeout_s,
                "detached": bool(detached),
                "created_at": now,
                "started_at": now,
                "ended_at": None,
                "log_bytes": 0,
                "truncated": False,
                "watch_muted": False,
            }
            task = Task(record)
            # detached wrapper 会在进程退出时写 exit_code 文件——目录必须
            # 先于 Popen 存在（无输出的短命令退出时 append_log 还没跑过）。
            store.task_dir(task_id).mkdir(parents=True, exist_ok=True)
            try:
                self._popen(task)
            except OSError as e:
                record["status"] = store.STATUS_FAILED_TO_START
                record["ended_at"] = time.time()
                record["exit_note"] = f"cannot execute: {type(e).__name__}: {e}"
                store.write_meta(task_id, record)
                records = store.read_index()
                records[task_id] = record
                store.write_index(records)
                raise RuntimeError(record["exit_note"]) from e
            record["pid"] = task.proc.pid if task.proc else None
            self._tasks[task_id] = task
            records = store.read_index()
            records[task_id] = record
            store.write_index(records)
            store.write_meta(task_id, record)
        task.reader = threading.Thread(
            target=self._read_pipe, args=(task,), name=f"task-{task_id}", daemon=True
        )
        task.reader.start()
        _log.info(
            "task_spawned task=%s pid=%s detached=%s session=%s cmd=%r",
            task_id, record["pid"], detached, origin_session, command[:120],
        )
        return dict(record)

    def _popen(self, task: Task) -> None:
        rec = task.record
        shell = _user_shell()
        cmd_text = rec["command"]
        if rec.get("detached"):
            # P2：wrapper 落盘退出码（sidecar 死后进程继续，退出码不丢）。
            # 必须用 subshell 而非 brace group——用户命令里的 ``exit`` 在
            # brace group 里会退出整个 shell、跳过 printf（首轮单测踩中）；
            # subshell 里只退子壳，$? 仍透传。$$ 不变（POSIX），BASHPID 会
            # 变——极少数依赖 BASHPID 的脚本不适用 detached，可接受。
            cmd_text = (
                f"(\n{rec['command']}\n)\n"
                f'__rc=$?\nprintf %s "$__rc" > {store.exit_code_path(rec["id"])}\nexit "$__rc"'
            )
        kwargs: dict[str, Any] = dict(
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=False,
            cwd=rec.get("cwd") or None,
        )
        if rec.get("detached"):
            kwargs["start_new_session"] = True
        task.proc = subprocess.Popen([shell, "-lc", cmd_text], **kwargs)

    # ------------------------------------------------------------------ #
    # reader thread
    # ------------------------------------------------------------------ #

    def _read_pipe(self, task: Task) -> None:
        """排空管道 → spool 追加 + watch 行匹配。进程退出后由 reaper 收尾。

        必须用 os.read 而非 BufferedReader.read(n)：后者攒满 n 字节或 EOF
        才返回，healthy/watch 的匹配行会被压到进程退出才可见（首轮单测
        实测踩中）。os.read 有多少返回多少，行匹配即时。"""
        rec = task.record
        buf = b""
        assert task.proc is not None and task.proc.stdout is not None
        try:
            fd = task.proc.stdout.fileno()
            while True:
                try:
                    chunk = os.read(fd, 16384)
                except OSError:
                    break
                if not chunk or task.removed:
                    break
                store.append_log(rec["id"], chunk)
                rec["log_bytes"] = rec.get("log_bytes", 0) + len(chunk)
                if store.shrink_log_if_needed(rec["id"], rec):
                    pass  # 截断标记在 rec 上，终态时随 meta 落盘
                if task.watches:
                    buf += chunk
                    *lines, buf = buf.split(b"\n")
                    for line in lines:
                        text = line.decode("utf-8", "replace")
                        for w in task.watches:
                            if w.pattern in text and (w.every or not w.fired):
                                w.fired = True
                                w.count += 1
                                task.watch_q.put((w.label or w.pattern, text))
                                break
        except Exception:  # noqa: BLE001 — reader 死不能拖垮进程，reaper 兜底
            _log.exception("task_reader_failed task=%s", rec["id"])

    # ------------------------------------------------------------------ #
    # reaper
    # ------------------------------------------------------------------ #

    async def _reap_loop(self) -> None:
        import asyncio

        try:
            while True:
                await asyncio.sleep(0.5)
                try:
                    self._reap_once()
                except Exception:  # noqa: BLE001
                    _log.exception("tasks_reap_iteration_failed")
        except asyncio.CancelledError:
            raise

    def _reap_once(self) -> None:
        now = time.time()
        with self._lock:
            tasks = list(self._tasks.values())
        for t in tasks:
            rec = t.record
            if rec.get("status") != store.STATUS_RUNNING:
                self._drain_watch_events(t, inject=False)
                continue
            # 超时（bg_run 显式 timeout=null → timeout_s None → 永不超时）。
            # 只发一次信号，随后照常走 _poll_exit——否则每轮 continue 会让
            # 已杀进程永远轮不到终结（首轮测试实测踩中）。
            timeout_s = rec.get("timeout_s")
            if (
                timeout_s is not None
                and not t.pending_stop
                and now - (rec.get("started_at") or now) > float(timeout_s)
            ):
                t.pending_stop = "timeout"
                self._signal_stop(t)
            exited, exit_code = self._poll_exit(t)
            if exited:
                status = (
                    store.STATUS_STOPPED
                    if t.pending_stop == "user"
                    else store.STATUS_TIMEOUT
                    if t.pending_stop == "timeout"
                    else store.STATUS_FINISHED
                )
                self._finalize(t, status, exit_code)
            else:
                self._drain_watch_events(t, inject=True)

    def _poll_exit(self, t: Task) -> tuple[bool, int | None]:
        if t.adopted:
            # 认领的 detached 进程：非子进程不能 waitpid——exit 文件优先，
            # pid 消失次之（SIGKILL 不会有 exit 文件，退出码未知）。
            try:
                raw = store.exit_code_path(t.record["id"]).read_text().strip()
                return True, int(raw)
            except (OSError, ValueError):
                pass
            pid = t.record.get("pid")
            try:
                os.kill(int(pid), 0)  # type: ignore[arg-type]
                return False, None
            except (ProcessLookupError, TypeError, ValueError):
                return True, None
        if t.proc is None:
            return True, t.record.get("exit_code")
        rc = t.proc.poll()
        if rc is None:
            return False, None
        # 子进程退出后 join reader（孙进程握管道的兜底与 bash 同款：reader
        # 读到 EOF 自然结束；超时也已有部分 spool）。
        if t.reader is not None:
            t.reader.join(timeout=2)
        return True, rc

    # ------------------------------------------------------------------ #
    # watch 事件（healthy / P2 pattern watch）
    # ------------------------------------------------------------------ #

    def _drain_watch_events(self, t: Task, *, inject: bool) -> None:
        now = time.time()
        events: list[tuple[str, str]] = []
        while True:
            try:
                events.append(t.watch_q.get_nowait())
            except queue.Empty:
                break
        if not events:
            return
        if t.watch_muted:
            return
        t.watch_events += len(events)
        if not inject:
            return
        label, line = events[-1]
        throttled = now - t.last_watch_at < WATCH_MIN_INTERVAL_S
        over_limit = t.watch_events > WATCH_MAX_EVENTS
        if over_limit:
            t.watch_muted = True
            t.record["watch_muted"] = True
        kind = "healthy" if label == "healthy" else "watch"
        if throttled or over_limit:
            reason = "rate-limited" if throttled else f"muted (> {WATCH_MAX_EVENTS} events)"
            _log.info("task_watch_event_skipped task=%s reason=%s", t.record["id"], reason)
            if over_limit:
                self._emit_event(t, "watch_muted", line=line)
            return
        t.last_watch_at = now
        self._emit_event(t, kind, line=line, label=label)

    def _emit_event(self, t: Task, kind: str, *, line: str = "", label: str = "") -> None:
        """把事件交给 inject 模块（reaper 的 asyncio 上下文里调用）。"""
        if not t.record.get("notify"):
            return
        try:
            from . import inject

            inject.on_task_event(t.record, kind, line=line, label=label)
        except Exception:  # noqa: BLE001 — 注入失败不影响任务状态机
            _log.exception("task_event_inject_failed task=%s kind=%s", t.record["id"], kind)

    # ------------------------------------------------------------------ #
    # 终态
    # ------------------------------------------------------------------ #

    def _finalize(self, t: Task, status: str, exit_code: int | None) -> None:
        now = time.time()
        rec = t.record
        rec["status"] = status
        rec["exit_code"] = exit_code
        rec["ended_at"] = now
        rec["dur"] = _fmt_dur(now - (rec.get("started_at") or now))
        with self._lock:
            records = store.read_index()
            records[rec["id"]] = rec
            store.write_index(records)
        store.write_meta(rec["id"], rec)
        _log.info(
            "task_finalized task=%s status=%s exit=%s dur=%s",
            rec["id"], status, exit_code, rec["dur"],
        )
        self._emit_event(t, status)

    # ------------------------------------------------------------------ #
    # 对外操作（tools / REST 共用）
    # ------------------------------------------------------------------ #

    def _signal(self, t: Task, sig: int) -> None:
        try:
            if t.record.get("detached") and t.record.get("pid"):
                os.killpg(os.getpgid(int(t.record["pid"])), sig)
            elif t.proc is not None:
                t.proc.send_signal(sig)
        except (ProcessLookupError, PermissionError, OSError):
            pass

    def _signal_stop(self, t: Task) -> None:
        if t.stopping:
            return
        t.stopping = True
        self._signal(t, signal.SIGTERM)
        # 宽限交 reaper：下下轮还没退就 SIGKILL（_reap_once 里 timeout/stop
        # 分支会先 poll；这里只负责升级信号）。
        def _escalate() -> None:
            time.sleep(STOP_GRACE_S)
            if t.proc is not None and t.proc.poll() is None:
                self._signal(t, signal.SIGKILL)

        threading.Thread(target=_escalate, daemon=True, name=f"task-stop-{t.record['id']}").start()

    def stop(self, task_id: str) -> dict:
        t = self._get(task_id)
        if t.record.get("status") != store.STATUS_RUNNING:
            return dict(t.record)
        t.pending_stop = "user"
        self._signal_stop(t)
        _log.info("task_stop_requested task=%s", task_id)
        return dict(t.record)

    def remove(self, task_id: str) -> None:
        """停止（如在跑）并删除记录与日志。"""
        with self._lock:
            t = self._tasks.pop(task_id, None)
        if t is not None:
            t.removed = True
            if t.record.get("status") == store.STATUS_RUNNING:
                t.pending_stop = "user"
                self._signal_stop(t)
        records = store.read_index()
        records.pop(task_id, None)
        store.write_index(records)
        store.remove_task_dir(task_id)
        _log.info("task_removed task=%s", task_id)

    def _get(self, task_id: str) -> Task:
        with self._lock:
            t = self._tasks.get(task_id)
        if t is None:
            raise KeyError(f"unknown task: {task_id}")
        return t

    def get(self, task_id: str) -> dict:
        t = self._get(task_id)
        return dict(t.record)

    def try_get(self, task_id: str) -> dict | None:
        with self._lock:
            t = self._tasks.get(task_id)
        return dict(t.record) if t else None

    def list(self) -> list[dict]:
        """活任务（内存，最新状态）+ 历史终态（index；重启后任务中心的
        Recent 全靠这里），running 置顶、新在前。"""
        with self._lock:
            items = [dict(t.record) for t in self._tasks.values()]
        seen = {r.get("id") for r in items}
        for rec in store.read_index().values():
            if rec.get("id") not in seen:
                items.append(dict(rec))
                seen.add(rec.get("id"))
        items.sort(key=lambda r: (r.get("status") != store.STATUS_RUNNING, -(r.get("started_at") or 0)))
        return items


_REGISTRY: TaskRegistry | None = None


def get_registry() -> TaskRegistry:
    """进程内单例。未 start（单测/CLI）时也可用——spawn/reaper 按需工作。"""
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = TaskRegistry()
    return _REGISTRY


def reset_registry() -> None:
    """单测隔离用：丢弃单例（不碰磁盘）。"""
    global _REGISTRY
    if _REGISTRY is not None and _REGISTRY._reaper is not None:
        _REGISTRY._reaper.cancel()
    _REGISTRY = None
