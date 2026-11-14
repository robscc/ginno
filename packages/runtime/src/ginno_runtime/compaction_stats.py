"""压缩时长历史 + 进度预估（2026-11-14 用户需求：看历史压缩时长预估进度）。

每次压缩的摘要 LLM 调用是耗时大头（compaction.py on_summary_start→done 之间）。
本模块记录历史时长，并在新压缩开始时按消息量给出预估秒数，供前端把进度条从
「盲目缓动」升级为「按预估时长走 + 显示 ETA」。

存储：home()/compaction_history.jsonl（append-only，保留最近 50 条）。
零 LLM、纯统计；历史不足（<2 条）时 predict 返回 None，前端回退旧缓动。
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

from .paths import home

_HISTORY = "compaction_history.jsonl"
_KEEP = 50
_MIN_SAMPLES = 2


def _path() -> Path:
    return home() / _HISTORY


def record(duration_s: float, n_messages: int) -> None:
    """追加一条压缩时长记录（秒 + 被压缩消息数），并裁剪到最近 _KEEP 条。"""
    try:
        if duration_s <= 0:
            return
        p = _path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"d": round(float(duration_s), 2), "n": int(n_messages)}) + "\n")
        # 裁剪：超出 _KEEP 行时重写为尾部 _KEEP 行
        lines = p.read_text(encoding="utf-8").splitlines()
        if len(lines) > _KEEP:
            p.write_text("\n".join(lines[-_KEEP:]) + "\n", encoding="utf-8")
    except Exception:
        pass


def _load() -> list[dict]:
    try:
        p = _path()
        if not p.exists():
            return []
        out = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if not ln:
                continue
            try:
                r = json.loads(ln)
                if isinstance(r.get("d"), (int, float)) and r["d"] > 0:
                    out.append(r)
            except Exception:
                continue
        return out
    except Exception:
        return []


def predict(n_messages: int) -> float | None:
    """按历史预估本次压缩秒数；样本不足返回 None（前端回退旧缓动）。

    策略：取最近样本时长的中位数为基准，再按「本次消息数 / 历史消息数中位数」
    线性缩放（夹在 0.5~2.0 倍，防异常样本拉飞）。中位数对离群值稳健。
    """
    hist = _load()
    if len(hist) < _MIN_SAMPLES:
        return None
    durs = [r["d"] for r in hist]
    base = statistics.median(durs)
    ns = [r["n"] for r in hist if isinstance(r.get("n"), int) and r["n"] > 0]
    if ns and n_messages > 0:
        med_n = statistics.median(ns)
        if med_n > 0:
            scale = max(0.5, min(2.0, n_messages / med_n))
            base *= scale
    return round(base, 1)


def stats() -> dict:
    """诊断用：样本数 + 中位/最小/最大时长。"""
    hist = _load()
    if not hist:
        return {"samples": 0}
    durs = [r["d"] for r in hist]
    return {
        "samples": len(durs),
        "median_s": round(statistics.median(durs), 1),
        "min_s": round(min(durs), 1),
        "max_s": round(max(durs), 1),
    }
