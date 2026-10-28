"""后台任务持久层（background-tasks-design.md §3/D3）。

目录布局::

    ~/.ginno/tasks/
      index.json            # 全部任务元数据（原子写）
      <task_id>/
        meta.json           # 单任务元数据（终态含 exit_code）
        output.log          # stdout+stderr 合并追加 spool
        exit_code           # detached wrapper 落盘的退出码（P2）

数值默认值全部来自 settings.json 的 ``background_tasks`` 键（评审决策 2：
可配置、不做设置页面）。读侧每次现读——spawn/stop 时各一次，开销可忽略，
换来的 setings 热改立即生效。
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from .. import paths

_log = logging.getLogger("ginno.tasks")

DEFAULTS: dict[str, Any] = {
    "max_concurrent": 5,
    "default_timeout_s": 7200,
    "log_cap_mb": 10,
    "inject_tail_lines": 40,
    "retention_days": 7,
    "retention_keep": 50,
}

# index 里 status 为 running 的记录重启后的处置（registry 启动迁移用）：
#   detached → 认领（exit_code 文件在 → finished；进程没了 → interrupted）
#   非 detached → 一律 interrupted（sidecar 退出杀掉了所有子进程）
STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_STOPPED = "stopped"
STATUS_TIMEOUT = "timeout"
STATUS_FAILED_TO_START = "failed_to_start"
STATUS_INTERRUPTED = "interrupted"


def config() -> dict[str, Any]:
    """settings.json ``background_tasks`` 键合入默认值（坏形状静默回默认）。"""
    try:
        raw = json.loads(paths.settings_path().read_text() or "{}")
    except Exception:  # noqa: BLE001 — 配置坏不挡任务系统
        return dict(DEFAULTS)
    v = raw.get("background_tasks")
    if not isinstance(v, dict):
        return dict(DEFAULTS)
    out = dict(DEFAULTS)
    for k, d in DEFAULTS.items():
        if isinstance(v.get(k), type(d)):
            out[k] = v[k]
    return out


def tasks_dir() -> Path:
    return paths.home() / "tasks"


def task_dir(task_id: str) -> Path:
    return tasks_dir() / task_id


def log_path(task_id: str) -> Path:
    return task_dir(task_id) / "output.log"


def meta_path(task_id: str) -> Path:
    return task_dir(task_id) / "meta.json"


def exit_code_path(task_id: str) -> Path:
    return task_dir(task_id) / "exit_code"


def index_path() -> Path:
    return tasks_dir() / "index.json"


def _atomic_write_json(p: Path, obj: Any) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False))
    os.replace(tmp, p)


def read_index() -> dict[str, dict]:
    try:
        raw = json.loads(index_path().read_text() or "{}")
    except Exception:  # noqa: BLE001 — 首启/坏文件都当空索引
        return {}
    return raw if isinstance(raw, dict) else {}


def write_index(records: dict[str, dict]) -> None:
    _atomic_write_json(index_path(), records)


def write_meta(task_id: str, record: dict) -> None:
    _atomic_write_json(meta_path(task_id), record)


def append_log(task_id: str, data: str | bytes) -> None:
    p = log_path(task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    mode = "ab" if isinstance(data, bytes) else "a"
    with open(p, mode) as f:
        f.write(data)
        f.flush()


def shrink_log_if_needed(task_id: str, record: dict) -> bool:
    """超 log_cap_mb 截头保尾（原地重写保留下半段），置 truncated 标记。

    返回是否发生了截断（调用方负责把标记写回 index/meta）。
    """
    cap = int(config()["log_cap_mb"]) * 1024 * 1024
    p = log_path(task_id)
    try:
        if p.stat().st_size <= cap:
            return False
        keep = cap // 2
        with open(p, "rb") as f:
            f.seek(-keep, os.SEEK_END)
            tail = f.read()
        tmp = p.with_suffix(".log.tmp")
        tmp.write_bytes(b"[... log truncated (head dropped) ...]\n" + tail)
        os.replace(tmp, p)
        record["truncated"] = True
        record["log_bytes"] = p.stat().st_size
        return True
    except OSError:
        _log.exception("task_log_shrink_failed task=%s", task_id)
        return False


def read_log_tail(task_id: str, tail_lines: int) -> list[str]:
    """spool 尾部 N 行（utf-8 容错解码）——注入消息与 bg_logs 的数据源。"""
    p = log_path(task_id)
    try:
        with open(p, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 256 * 1024))
            data = f.read()
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines()[-tail_lines:]


def read_log(task_id: str, offset_line: int = 0) -> tuple[list[str], int]:
    """全量读取（bg_logs full / 任务中心日志视图）。返回 (行列表, 总行数)。"""
    p = log_path(task_id)
    try:
        lines = p.read_text(errors="replace").splitlines()
    except OSError:
        return [], 0
    return lines[offset_line:], len(lines)


def remove_task_dir(task_id: str) -> None:
    import shutil

    shutil.rmtree(task_dir(task_id), ignore_errors=True)


def startup_migrate(records: dict[str, dict]) -> list[tuple[str, str]]:
    """重启对账（lifespan 调用）：running 记录的处置决策。

    返回 [(task_id, new_status)]（registry 对 detached 的认领在此之后细化）。
    非 detached running → interrupted；detached 的初值也先置 interrupted，
    registry.startup_adopt() 再按 exit_code 文件 / pid 存活翻回 running /
    finished——迁移先落一个安全终值，认领失败也不会留下「永远 running」。
    """
    now = time.time()
    changed: list[tuple[str, str]] = []
    for tid, rec in records.items():
        if rec.get("status") != STATUS_RUNNING:
            continue
        rec["status"] = STATUS_INTERRUPTED
        rec["ended_at"] = rec.get("ended_at") or now
        rec["interrupted_note"] = "app quit"
        rec["adoptable"] = bool(rec.get("detached"))
        changed.append((tid, STATUS_INTERRUPTED))
        write_meta(tid, rec)
    if changed:
        write_index(records)
        _log.info("tasks_startup_migrated count=%d", len(changed))
    return changed


def cleanup_expired(records: dict[str, dict]) -> int:
    """保留策略：终态任务超过 retention_days 或超出 retention_keep 条数即删。"""
    cfg = config()
    days = float(cfg["retention_days"])
    keep = int(cfg["retention_keep"])
    now = time.time()
    horizon = now - days * 86400
    finished: list[tuple[float, str]] = [
        (r.get("ended_at") or r.get("started_at") or now, tid)
        for tid, r in records.items()
        if r.get("status") not in (STATUS_RUNNING,)
    ]
    doomed: set[str] = {tid for ended, tid in finished if ended < horizon}
    if len(finished) - len(doomed) > keep:
        ordered = sorted(finished)  # 最旧在前
        for _ended, tid in ordered[: len(finished) - keep]:
            doomed.add(tid)
    for tid in doomed:
        records.pop(tid, None)
        remove_task_dir(tid)
    if doomed:
        write_index(records)
        _log.info("tasks_retention_removed count=%d", len(doomed))
    return len(doomed)
