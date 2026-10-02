"""定时任务配置 + 执行记录存储（scheduled-tasks-design.md §4）。

两块持久化，全部「文件即状态、无数据库」：

* 任务定义：``~/.ginno/schedules.json``（§4.1）——temp+rename 原子写；
  读侧容错（坏文件按空配置起，不崩 runtime）。
* 执行记录：``~/.ginno/schedule-runs/runs-YYYY-MM-DD.jsonl``（§4.2）——逐行
  append、never raises、坏行跳过、(path, mtime, size) 读缓存、90 天保留清理。
  一次执行可产生两行（running → 终态），**同 run_id 末行胜出**；模式完全对齐
  ``usage_store.py``。

§4/§5 的字段名是跨 agent 契约（前端/验证 agent 依赖），不得改名。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import paths

_log = logging.getLogger("ginno.schedule")

RETENTION_DAYS = 90
_FILE_PREFIX = "runs-"

# 间隔下限（§10 决议 2）：5 分钟，保存时与触发时双侧校验。
MIN_INTERVAL_MIN = 5

TARGET_TYPES = ("prompt", "workflow")
SCHEDULE_KINDS = ("interval", "daily", "weekly", "once")
RUN_STATUSES = ("running", "ok", "error", "missed", "skipped_overlap")

# (path, mtime_ns, size) -> 已解析的完成日记录（当天文件持续增长，永远重读）。
_DAY_CACHE: dict[tuple, list[dict]] = {}
_LAST_CLEANUP_DAY: str | None = None


def reset_cache() -> None:
    """Drop in-process caches (tests switch $GINNO_HOME between cases)."""
    global _LAST_CLEANUP_DAY
    _DAY_CACHE.clear()
    _LAST_CLEANUP_DAY = None


# --------------------------------------------------------------------------- #
# schedules.json（§4.1）
# --------------------------------------------------------------------------- #
def _default_config() -> dict:
    return {"enabled": True, "keep_awake": False, "tasks": []}


def load_config() -> dict:
    """读整体配置。坏文件/缺字段按空配置起（或逐字段补默认），不崩 runtime。"""
    p = paths.schedules_path()
    try:
        data = json.loads(p.read_text() or "{}")
    except (OSError, json.JSONDecodeError):
        return _default_config()
    if not isinstance(data, dict):
        return _default_config()
    cfg = _default_config()
    cfg["enabled"] = bool(data.get("enabled", True))
    cfg["keep_awake"] = bool(data.get("keep_awake", False))
    tasks = data.get("tasks")
    cfg["tasks"] = [t for t in tasks if isinstance(t, dict)] if isinstance(tasks, list) else []
    return cfg


def save_config(cfg: dict) -> None:
    """整体写回。temp+rename 原子写；磁盘失败 never raises（§7 兼容边界）。"""
    try:
        p = paths.schedules_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
        os.replace(tmp, p)
    except OSError:
        _log.warning("schedules.json write failed", exc_info=True)


def list_tasks() -> list[dict]:
    return load_config().get("tasks") or []


def get_task(task_id: str) -> dict | None:
    return next((t for t in list_tasks() if t.get("id") == task_id), None)


def upsert_task(task: dict) -> dict:
    """插入或整体替换一个任务（id 定位）。"""
    cfg = load_config()
    tasks = [t for t in (cfg.get("tasks") or []) if t.get("id") != task["id"]]
    tasks.append(task)
    cfg["tasks"] = tasks
    save_config(cfg)
    return task


def patch_task(task_id: str, patch: dict) -> dict | None:
    """部分更新一个任务；id 不存在返回 None。``None`` 值不落（同 meta patch 约定），
    唯 ``next_run_at=None`` 例外——「无下次」是调度器的真实状态，必须能显式清除。"""
    cfg = load_config()
    target = None
    for t in cfg.get("tasks") or []:
        if t.get("id") == task_id:
            t.update({
                k: v for k, v in patch.items()
                if v is not None or k == "next_run_at"
            })
            t["updated"] = time.time()
            target = t
    if target is not None:
        save_config(cfg)
    return target


def delete_task(task_id: str) -> bool:
    """删除任务。不动执行记录与会话（§3.2：账单性质数据不销毁）。"""
    cfg = load_config()
    tasks = cfg.get("tasks") or []
    kept = [t for t in tasks if t.get("id") != task_id]
    if len(kept) == len(tasks):
        return False
    cfg["tasks"] = kept
    save_config(cfg)
    return True


def new_task_id() -> str:
    return f"st-{uuid.uuid4().hex[:6]}"


# ---- 校验 -----------------------------------------------------------------
def _valid_hhmm(s) -> bool:
    if not isinstance(s, str):
        return False
    try:
        h, m = s.split(":")
        return 0 <= int(h) <= 23 and 0 <= int(m) <= 59 and len(h) == 2 and len(m) == 2
    except (ValueError, AttributeError):
        return False


def validate_task(data: dict) -> list[str]:
    """保存时校验（POST/PATCH 共用）。返回错误列表；空列表 = 合法。"""
    errs: list[str] = []
    if not isinstance(data.get("name"), str) or not data.get("name", "").strip():
        errs.append("任务名称不能为空")
    target = data.get("target")
    if not isinstance(target, dict) or target.get("type") not in TARGET_TYPES:
        errs.append("target.type 必须是 prompt 或 workflow")
    else:
        if target["type"] == "prompt" and not str(target.get("prompt") or "").strip():
            errs.append("prompt 目标必须填写提示词")
        if target["type"] == "workflow" and not str(target.get("workflow_id") or "").strip():
            errs.append("workflow 目标必须选择配方")
    sch = data.get("schedule")
    if not isinstance(sch, dict) or sch.get("kind") not in SCHEDULE_KINDS:
        errs.append("schedule.kind 必须是 interval/daily/weekly/once")
    else:
        kind = sch["kind"]
        if kind == "interval":
            minutes = sch.get("minutes")
            bad_minutes = not isinstance(minutes, int) or isinstance(minutes, bool)
            if bad_minutes or minutes < MIN_INTERVAL_MIN:
                errs.append(f"间隔不能小于 {MIN_INTERVAL_MIN} 分钟")
        elif kind == "daily":
            if not _valid_hhmm(sch.get("at")):
                errs.append("daily 计划的 at 必须是 HH:MM")
        elif kind == "weekly":
            wd = sch.get("weekday")
            if not isinstance(wd, int) or isinstance(wd, bool) or not 0 <= wd <= 6:
                errs.append("weekly 计划的 weekday 必须是 0-6（0=周日）")
            if not _valid_hhmm(sch.get("at")):
                errs.append("weekly 计划的 at 必须是 HH:MM")
        elif kind == "once":
            try:
                datetime.fromisoformat(str(sch.get("at") or ""))
            except ValueError:
                errs.append("once 计划的 at 必须是本地时间 ISO 格式（YYYY-MM-DDTHH:MM[:SS]）")
    return errs


def missing_required_inputs(
    schema: dict | None, initial: dict | None, override: dict | None
) -> list[str]:
    """workflow 必填输入缺失检查（§10 决议 4）：schema.required 中未由
    context.initial + context_override 提供有效值的键。None/空串视为缺失。"""
    required = (schema or {}).get("required") or []
    merged = dict(initial or {})
    if isinstance(override, dict):
        merged.update(override)
    missing = []
    for key in required:
        if not isinstance(key, str):
            continue
        v = merged.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            missing.append(key)
    return missing


# --------------------------------------------------------------------------- #
# 执行记录 jsonl（§4.2）—— 模式对齐 usage_store.py
# --------------------------------------------------------------------------- #
def _date_str(ts: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def _day_path(date_str: str) -> Path:
    return paths.schedule_runs_dir() / f"{_FILE_PREFIX}{date_str}.jsonl"


def _today() -> str:
    return _date_str(time.time())


def new_run_id() -> str:
    return f"sr-{uuid.uuid4().hex[:8]}"


def record_run(entry: dict) -> None:
    """Append one execution-record line. Never raises（调度主流程不被记账拖垮）。"""
    try:
        ts = float(entry.get("started_at") or entry.get("scheduled_at") or time.time())
        day = _date_str(ts)
        paths.schedule_runs_dir().mkdir(parents=True, exist_ok=True)
        with open(_day_path(day), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _maybe_cleanup(day)
    except Exception:  # noqa: BLE001 — 执行记录永远不拖垮调度
        _log.warning("schedule run record failed", exc_info=True)


def _maybe_cleanup(today: str) -> None:
    global _LAST_CLEANUP_DAY
    if _LAST_CLEANUP_DAY == today:
        return
    _LAST_CLEANUP_DAY = today
    try:
        cleanup(RETENTION_DAYS)
    except Exception:  # noqa: BLE001
        _log.warning("schedule runs cleanup failed", exc_info=True)


def cleanup(retention_days: int = RETENTION_DAYS) -> int:
    """Delete per-day run logs older than ``retention_days``. Returns count."""
    cutoff = (datetime.now() - timedelta(days=retention_days)).strftime("%Y-%m-%d")
    removed = 0
    d = paths.schedule_runs_dir()
    if not d.is_dir():
        return 0
    for p in d.glob(f"{_FILE_PREFIX}*.jsonl"):
        day = p.name[len(_FILE_PREFIX):-len(".jsonl")]
        if day < cutoff:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def load_day(date_str: str) -> list[dict]:
    """All run lines of one day (empty when absent). 坏行跳过；完成日进进程缓存。"""
    p = _day_path(date_str)
    if not p.exists():
        return []
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns, st.st_size)
    cached = _DAY_CACHE.get(key)
    if cached is not None:
        return cached
    entries: list[dict] = []
    try:
        with open(p, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(e, dict):
                    entries.append(e)
    except OSError:
        return []
    if date_str != _today():
        if len(_DAY_CACHE) > 130:
            _DAY_CACHE.pop(next(iter(_DAY_CACHE)))
        _DAY_CACHE[key] = entries
    return entries


def dedupe_runs(entries: list[dict]) -> list[dict]:
    """同 run_id 末行胜出（design §4.2）；输出按出现顺序（末行替位）。"""
    out: dict[str, dict] = {}
    for e in entries:
        rid = e.get("run_id") or ""
        out[rid] = e
    return list(out.values())


def query_runs(
    date_str: str | None = None,
    task_id: str | None = None,
    target_type: str | None = None,
    status: str | None = None,
    trigger: str | None = None,
    sort: str = "desc",
    page: int = 1,
    page_size: int = 50,
) -> dict:
    """执行记录分页查询（§5.1 GET /api/schedule/runs）。同 run_id 去重后过滤。

    ``date_str`` 给定 → 只查该日；缺省 → 聚合保留期内全部日期（前端「近
    N 天」范围与 footer 状态点不带 date 拉取，靠客户端过滤收窄）。跨日同
    run_id（执行跨午夜）按日期升序去重，末行（最新日）胜出（§4.2）。
    响应行键为 ``rows``（对齐 usage 请求日志；timeline 用 ``runs``，勿混）。"""
    page = max(1, page)
    page_size = max(1, min(page_size, 200))
    if date_str:
        days = [date_str]
    else:
        d = paths.schedule_runs_dir()
        days = sorted(
            p.name[len(_FILE_PREFIX):-len(".jsonl")]
            for p in d.glob(f"{_FILE_PREFIX}*.jsonl")
        ) if d.is_dir() else []
    rows: list[dict] = []
    for day in days:
        rows.extend(load_day(day))
    rows = dedupe_runs(rows)
    if task_id:
        rows = [e for e in rows if e.get("task_id") == task_id]
    if target_type:
        rows = [e for e in rows if e.get("target_type") == target_type]
    if status:
        rows = [e for e in rows if e.get("status") == status]
    if trigger:
        rows = [e for e in rows if e.get("trigger") == trigger]
    def _ts(e: dict):
        return e.get("finished_at") or e.get("started_at") or e.get("scheduled_at") or 0

    rows.sort(key=_ts, reverse=(sort != "asc"))
    total = len(rows)
    start = (page - 1) * page_size
    return {
        "date": date_str,
        "total": total,
        "page": page,
        "page_size": page_size,
        "rows": rows[start:start + page_size],
    }


def iter_range(from_date: str, to_date: str):
    """Yield run lines for each day in [from_date, to_date] (inclusive)."""
    d = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    while d <= end:
        yield from load_day(d.strftime("%Y-%m-%d"))
        d += timedelta(days=1)
