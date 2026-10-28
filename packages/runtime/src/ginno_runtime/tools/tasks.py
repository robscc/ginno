"""bg_* 后台任务工具（background-tasks-design.md §4）。

与 ``bash`` 的分工写在 bg_run 描述里：阻塞短命令用 bash（30s 超时），
分钟级或「跑完再叫我」的用 bg_run。deny-root 扫描与 workspace cwd 解析
复用 builtin 的同一套 guard（不另起炉灶），后台化不降权限。

bg_run 返回文本首行固定 ``task=<id> status=... pid=...``——前端 ToolBlock
按这个机器头把 bg_run 气泡升级为常驻 TaskCard（合并卡片，评审决策 4）。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import tool

from ..tasks import get_registry
from ..tasks import store as task_store
from .builtin import _deny_msg, _is_user_rc, _path_denied, resolve_mounts


def build_task_tools(
    workspace: str | None = None,
    context_dirs: list[dict] | None = None,
    primary_path: str | None = None,
    session_id: str | None = None,
) -> list:
    """Build the five bg_* tools bound to this session's workspace/origin."""
    from .builtin import _base

    ws_root = _base(workspace)
    mounts: list[tuple[Path, str]] = resolve_mounts(context_dirs)
    mount_roots: list[Path] = [p for p, _ in mounts]
    base_dir = ws_root
    if primary_path:
        cand = Path(primary_path).expanduser()
        if cand.is_dir():
            base_dir = cand

    def _deny_scan(command: str) -> str | None:
        """与 builtin.bash 同款弱硬拒绝扫描（含 user-rc 豁免）。"""
        for tok in command.split():
            tok = tok.strip("'\"`;|&")
            if not tok or not (tok.startswith("/") or tok.startswith("~")):
                continue
            candidate = Path(tok).expanduser()
            if _is_user_rc(candidate):
                continue
            if _path_denied(candidate, base_dir, mount_roots):
                return _deny_msg(tok)
        return None

    def _fmt_elapsed(rec: dict) -> str:
        if rec.get("status") != "running":
            return rec.get("dur") or ""
        s = max(0, int(time.time() - (rec.get("started_at") or time.time())))
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}h{m:02d}m" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")

    @tool
    def bg_run(
        command: str,
        name: str = "",
        cwd: str = "",
        timeout: int = 7200,
        notify: bool = True,
        mode: str = "run",
        healthy: str = "",
        watch: list[dict] | None = None,
        detached: bool = False,
    ) -> str:
        """Run a shell command in the BACKGROUND and return immediately. The
        command keeps running while you (and the user) continue working; when
        it exits its result is pushed back into this session automatically
        (exit code + output tail) — no polling needed. Use plain `bash` for
        quick commands (< ~1 min); use this for builds, long test suites, dev
        servers, watchers, training runs.

        Lifecycle: bg_list lists all tasks, bg_logs peeks at output anytime,
        bg_stop stops one, bg_remove deletes a finished one. Output is spooled
        to ~/.ginno/tasks/<id>/output.log (head-dropped past the size cap).

        Use mode="server" for processes that never exit on their own (dev
        servers, watchers): give `healthy` the line that marks readiness (e.g.
        "Ready in") — you get notified once when it appears; exit/stop/timeout
        are still reported. `watch` adds more patterns: each matching output
        line notifies you ({pattern, label?, every?}; default fires once,
        every=true on every match, rate-limited). `detached=true` lets the
        process survive a Ginno restart (exit code is recorded by a wrapper;
        the task is re-adopted on startup). `timeout` seconds (<= 0 disables
        the timeout — you must then bg_stop it yourself); `notify=false` skips
        the automatic result push (you'll have to bg_logs yourself)."""
        denied = _deny_scan(command)
        if denied:
            return denied
        if mode not in ("run", "server"):
            return f"[error] mode must be 'run' or 'server', got {mode!r}"
        if mode == "server" and not healthy:
            return '[error] mode="server" needs healthy= — the output line that means "ready" (e.g. "Ready in")'
        run_cwd = str(base_dir) if base_dir.is_dir() else None
        if cwd:
            cand = Path(cwd).expanduser()
            if not cand.is_dir():
                return f"[error] cwd is not a directory: {cwd}"
            run_cwd = str(cand)
        try:
            rec = get_registry().spawn(
                command=command,
                title=name or command.split("\n")[0][:40],
                cwd=run_cwd,
                origin_session=session_id,
                origin_turn=None,
                notify=notify,
                # <=0 → 不限时（描述里警告过须自行 bg_stop）
                timeout_s=None if timeout <= 0 else int(timeout),
                mode=mode,
                healthy=healthy,
                watch=watch,
                detached=detached,
            )
        except (RuntimeError, ValueError) as e:
            return f"[error] {e}"
        lines = [
            f"task={rec['id']} status=running pid={rec.get('pid')}",
            f"log={task_store.log_path(rec['id'])}",
        ]
        if notify:
            lines.append(
                "The result (exit code + output tail) will be pushed into this "
                "session automatically when it exits — no polling needed."
            )
            if mode == "server":
                lines.append(f'You will be notified once the "healthy" line appears ("{healthy}").')
        else:
            lines.append("notify=false: check with bg_logs / bg_list yourself.")
        return "\n".join(lines)

    @tool
    def bg_list() -> str:
        """List every background task (all sessions), running first: id,
        status, elapsed/exit, command."""
        rows = get_registry().list()
        if not rows:
            return "No background tasks."
        out = []
        for r in rows:
            status = r.get("status") or "?"
            extra = f"exit {r.get('exit_code')}" if status == "finished" else (
                r.get("dur") or "" if status != "running" else _fmt_elapsed(r)
            )
            out.append(
                f"{r['id']}  {status:<10} {extra:<10} {r.get('title') or r.get('command', '')[:40]}"
            )
        return f"{len(rows)} task(s):\n" + "\n".join(out)

    @tool
    def bg_logs(task_id: str, tail: int = 40, full: bool = False) -> str:
        """Read a background task's spooled output. Default: last `tail`
        lines; `full=true` reads from the start (head+tail capped on huge
        logs). Works on running tasks too."""
        reg = get_registry()
        rec = reg.try_get(task_id)
        if rec is None:
            return f"[error] unknown task: {task_id} (bg_list to see ids)"
        if full:
            lines, total = task_store.read_log(task_id)
            budget = 400
            if len(lines) > budget:
                head, tl = lines[: budget // 4], lines[-budget:]
                body = "\n".join(head) + f"\n[... {len(lines) - budget} lines omitted ...]\n" + "\n".join(tl)
            else:
                body = "\n".join(lines)
            head_line = f"[{task_id} {rec.get('status')} · {len(lines)}/{total} lines]"
            return f"{head_line}\n{body}" if body else f"{head_line}\n(no output)"
        lines = task_store.read_log_tail(task_id, max(1, int(tail)))
        return "\n".join(lines) if lines else "(no output yet)"

    @tool
    def bg_stop(task_id: str) -> str:
        """Stop a running background task (SIGTERM, escalates to SIGKILL after
        2s). Status settles as `stopped`; the stopped event is pushed to the
        origin session automatically."""
        reg = get_registry()
        try:
            rec = reg.stop(task_id)
        except KeyError:
            return f"[error] unknown task: {task_id}"
        if rec.get("status") != "running":
            return f"{task_id} already {rec.get('status')} (exit {rec.get('exit_code')})"
        return f"{task_id}: stop signal sent (SIGTERM, escalates to SIGKILL after 2s); status will settle as stopped."

    @tool
    def bg_remove(task_id: str) -> str:
        """Stop (if running) and delete a background task: its record and
        spooled log are removed. Use bg_stop instead when you may still want
        the log afterwards."""
        reg = get_registry()
        try:
            reg.remove(task_id)
        except KeyError:
            return f"[error] unknown task: {task_id}"
        return f"{task_id}: task removed, logs deleted."

    return [bg_run, bg_list, bg_logs, bg_stop, bg_remove]
