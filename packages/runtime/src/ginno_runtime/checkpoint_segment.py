"""Checkpoint 日志批量切分器（2026-11-08 切换会话卡顿治理·存量治理）。

长会话的 checkpoint 事件日志（实测 521MB）拖慢冷路径（list/clone/迁移的
全量 fold）并占盘。切分把日志拆成「主文件（最后一个 full 锚点起，自包
含，热路径尾窗只读它）+ 只读归档段」：

    <sid>.jsonl           主文件：首行 {"op":"seg","archives":[...]} 引用
                          归档段（发现机制），其后是锚点起的全部事件
    <sid>.seg.1.jsonl     只读归档段（按 seq 排序，fold 顺序在主段之前）
    <sid>.jsonl.bak-first 首次切分的原始全量备份（--backup first 默认开）

崩溃安全（单写者 + temp+rename）：
- 归档段先写完（temp+rename）——此时主文件未变，归档是"孤儿"（header
  未引用它）→ 读侧忽略，数据语义仍为未切分；
- 主文件最后 rewrite（新 header + 保留段，temp+rename 原子替换）——
  header 引用生效瞬间即切分完成点。中途任何崩溃都落在两个稳定态之一。
- 二次切分：新归档段 seq 递增，header 重写追加引用；旧归档段不动。

语义保证（test_checkpoint_segment 等价性回归）：
- `_read_log`（全量语义）= 归档段（按 seq）拼接主段后 fold——与切分前
  的 entries 完全一致（checkpoint_id 序列与物化消息等价）；
- `_read_tail`（热路径）只读主文件：锚 full 保证 delta 链自包含；
- truncate（rerun_from）target 在归档段：append 的 truncate 事件在主段
  fold 序尾部，del 对拼接视图生效——语义天然正确，仅性能冗余（log 一条
  提示）。
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

_log = logging.getLogger(__name__)

SEG_OP = "seg"
DEFAULT_MIN_MB = 32.0
DEFAULT_KEEP_MB = 8.0


@dataclass
class SegmentPlan:
    path: Path
    total_bytes: int
    anchor_offset: int          # 主文件保留段的起始字节（锚 full 那行）
    archive_bytes: int          # 将进归档段的字节数 [0, anchor_offset)
    keep_bytes: int             # 保留段字节数（含新 header 行）
    archive_seq: int            # 本次新归档段 seq（已有归档之后）
    existing_archives: list[str] = field(default_factory=list)
    reason: str = ""

    @property
    def worth_it(self) -> bool:
        return self.archive_bytes > 0 and (
            self.archive_bytes >= (DEFAULT_KEEP_MB * 1024 * 1024)
            or self.reason == "forced")


def _iter_line_spans(path: Path):
    """流式迭代 (start_offset, end_offset, line_str)——不整读大文件。"""
    with open(path, "rb") as fh:
        offset = 0
        for raw in fh:
            end = offset + len(raw)
            yield offset, end, raw.decode("utf-8", "replace")
            offset = end


def read_seg_header(path: Path) -> dict | None:
    """主文件首行/次行的 seg 事件（归档引用）。无则 None。"""
    try:
        with open(path, "rb") as fh:
            for i, raw in enumerate(fh):
                if i > 2:
                    break
                try:
                    ev = json.loads(raw)
                except Exception:
                    return None
                if isinstance(ev, dict) and ev.get("op") == SEG_OP:
                    return ev
                if isinstance(ev, dict) and ev.get("op") == "cp":
                    return None  # 首个事件就是 cp：无 seg header
    except OSError:
        return None
    return None


def plan_segment(path: Path, *, keep_mb: float = DEFAULT_KEEP_MB) -> SegmentPlan | None:
    """计算切分计划：锚点 = 最后一个 full cp 行，且其后字节 ≥ keep 下限。"""
    size = path.stat().st_size
    keep_floor = int(keep_mb * 1024 * 1024)
    header = read_seg_header(path)
    existing = list((header or {}).get("archives") or [])
    next_seq = 1
    if existing:
        for name in existing:
            try:
                next_seq = max(next_seq, int(name.rsplit(".seg.", 1)[-1].split(".")[0]) + 1)
            except Exception:  # noqa: BLE001
                pass
    full_offsets: list[tuple[int, str]] = []  # (行起点, checkpoint_id)
    with open(path, "rb") as fh:
        offset = 0
        for raw in fh:
            start = offset
            if b'"op":"cp"' in raw or b'"op": "cp"' in raw:
                try:
                    ev = json.loads(raw)
                    if ev.get("op") == "cp" and ev.get("mode") != "delta":
                        full_offsets.append((start, str(ev.get("checkpoint_id"))))
                except Exception:  # noqa: BLE001 — 非 full 行不关心
                    pass
            offset += len(raw)
    if not full_offsets:
        return None
    # 从后往前找满足「其后字节 ≥ floor」的 full 锚
    anchor = None
    for off, cid in reversed(full_offsets):
        if size - off >= keep_floor:
            anchor = off
            break
    if anchor is None:
        anchor = full_offsets[0][0]
        if size - anchor < 1024 * 1024:
            return None  # 无可归档余量（整文件都是热段）
    return SegmentPlan(
        path=path, total_bytes=size, anchor_offset=anchor,
        archive_bytes=anchor - 0, keep_bytes=size - anchor,
        archive_seq=next_seq, existing_archives=existing,
        reason="",
    )


def apply_segment(plan: SegmentPlan, *, backup: str = "first") -> dict:
    """执行切分（事务顺序：归档 → 主文件 rewrite → 删旧备份规则）。"""
    path = plan.path
    sid = path.name[: -len(".jsonl")]
    dirp = path.parent
    archive_name = f"{sid}.seg.{plan.archive_seq}.jsonl"
    archive_path = dirp / archive_name
    ts = time.strftime("%Y%m%d-%H%M%S")

    # 1) 备份（首次切分且策略允许）：原始全量
    backup_path: Path | None = None
    if backup == "first":
        backup_path = dirp / f"{sid}.jsonl.bak-first"
        if not backup_path.exists():
            import shutil

            shutil.copy2(path, backup_path)

    # 2) 归档段：[0, anchor) 字节
    tmp = dirp / f".{archive_name}.tmp-{ts}"
    with open(path, "rb") as src, open(tmp, "wb") as dst:
        dst.write(src.read(plan.anchor_offset))
    os.replace(tmp, archive_path)

    # 3) 主文件 rewrite：seg header + 保留段 [anchor, EOF)
    archives = plan.existing_archives + [archive_name]
    header_line = json.dumps(
        {"op": SEG_OP, "v": 1, "archives": archives}, ensure_ascii=False
    ) + "\n"
    tmp2 = dirp / f".{sid}.jsonl.tmp-{ts}"
    with open(path, "rb") as src, open(tmp2, "wb") as dst:
        dst.write(header_line.encode("utf-8"))
        src.seek(plan.anchor_offset)
        while True:
            chunk = src.read(4 * 1024 * 1024)
            if not chunk:
                break
            dst.write(chunk)
    os.replace(tmp2, path)

    _log.info(
        "checkpoint_segmented path=%s archive=%s archive_mb=%.1f keep_mb=%.1f backup=%s",
        path.name, archive_name,
        plan.archive_bytes / 1e6, plan.keep_bytes / 1e6,
        backup_path.name if backup_path else "-",
    )
    return {
        "archive": archive_name,
        "archive_mb": round(plan.archive_bytes / 1e6, 1),
        "keep_mb": round(plan.keep_bytes / 1e6, 1),
        "backup": str(backup_path) if backup_path else None,
    }


def segment_all(
    home: Path | None = None,
    *,
    min_mb: float = DEFAULT_MIN_MB,
    keep_mb: float = DEFAULT_KEEP_MB,
    apply: bool = False,
    backup: str = "first",
    project: str | None = None,
) -> dict:
    """遍历所有项目的 sessions/*.jsonl 做切分（dry-run 默认）。

    apply 流程每文件：读切分前 checkpoint_id 序列 → plan → apply →
    `_read_log`（归档拼接语义）复验序列一致，不一致立即从备份回滚。"""
    from .checkpointer import _read_log

    root = (home or Path(os.environ.get("GINNO_HOME", Path.home() / ".ginno"))).resolve()
    projects = [project] if project else [
        p.name for p in (root / "projects").iterdir() if p.is_dir()
    ] if (root / "projects").is_dir() else []
    report: dict = {"dry_run": not apply, "scanned": 0, "planned": [],
                    "done": [], "skipped": [], "rolled_back": []}
    for slug in projects:
        sdir = root / "projects" / slug / "sessions"
        if not sdir.is_dir():
            continue
        for path in sorted(sdir.glob("*.jsonl")):
            report["scanned"] += 1
            size_mb = path.stat().st_size / 1e6
            if size_mb < min_mb:
                report["skipped"].append({"path": str(path), "mb": round(size_mb, 1),
                                          "why": f"< min_mb={min_mb}"})
                continue
            try:
                plan = plan_segment(path, keep_mb=keep_mb)
            except Exception:  # noqa: BLE001
                report["skipped"].append({"path": str(path), "why": "plan_failed"})
                continue
            if plan is None:
                report["skipped"].append({"path": str(path), "why": "no_splittable_anchor"})
                continue
            entry = {"path": str(path), "total_mb": round(size_mb, 1),
                     "archive_mb": round(plan.archive_bytes / 1e6, 1),
                     "keep_mb": round(plan.keep_bytes / 1e6, 1)}
            if not apply:
                report["planned"].append(entry)
                continue
            before_ids = [str(c.get("checkpoint_id")) for c in _read_log(path)]
            sid = path.name[: -len(".jsonl")]
            res = apply_segment(plan, backup=backup)
            try:
                after_ids = [str(c.get("checkpoint_id")) for c in _read_log(path)]
                if after_ids != before_ids:
                    raise AssertionError("fold-equivalence check failed")
                entry.update(res)
                report["done"].append(entry)
            except Exception:  # noqa: BLE001 — 验证失败：从备份回滚
                _log.exception("checkpoint_segment_verify_failed path=%s", path)
                bak = path.parent / f"{sid}.jsonl.bak-first"
                if bak.exists():
                    os.replace(bak, path)
                    _log.warning("checkpoint_segment_rolled_back path=%s", path)
                report["rolled_back"].append(entry)
    return report


def cli_main(argv: list[str]) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="ginno checkpoints",
        description="Checkpoint 日志维护（切分归档）",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    seg = sub.add_parser("segment", help="把超阈值日志切分为「主文件 + 只读归档段」")
    seg.add_argument("--min-mb", type=float, default=DEFAULT_MIN_MB,
                     help=f"只处理 ≥ 此 MB 的日志（默认 {DEFAULT_MIN_MB}）")
    seg.add_argument("--keep-mb", type=float, default=DEFAULT_KEEP_MB,
                     help=f"主文件至少保留此 MB 热段（默认 {DEFAULT_KEEP_MB}）")
    seg.add_argument("--apply", action="store_true",
                     help="默认 dry-run；--apply 才动文件")
    seg.add_argument("--backup", choices=["first", "none"], default="first",
                     help="首次切分备份原始全量（默认 first）")
    seg.add_argument("--project", default=None, help="只处理某个 project slug")
    seg.add_argument("--home", default=None, help="GINNO_HOME（默认读取环境/缺省）")
    a = p.parse_args(argv)
    if a.cmd == "segment":
        rep = segment_all(Path(a.home).resolve() if a.home else None,
                          min_mb=a.min_mb, keep_mb=a.keep_mb,
                          apply=a.apply, backup=a.backup, project=a.project)
        print(json.dumps(rep, ensure_ascii=False, indent=1))
        return 0
    return 2
