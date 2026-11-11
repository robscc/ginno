"""Instincts 捕获管道 P0：L1 确定性事件 + 模板规则生成（零 LLM）。

观察事件 → observations.jsonl（脱敏）；turn 收尾 maybe_analyze() 做
模板化规则生成：同类 tool_denied 观察 ≥ template_threshold 次 →
pending 条目（trigger/behavior 全模板，不调模型）。
"""

from __future__ import annotations

import json
import re
import threading
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import re as _re

from ..memory.pool import sanitize_for_memory

# 敏感串脱敏（密钥类；注入类模式由 sanitize_for_memory 清洗）。
_SECRET_RE = _re.compile(
    r"(Bearer\s+)[\w.\-]+|(sk|pk|rk)-[\w\-]{8,}|(api[_-]?key|token|password"
    r"|secret)([\s=:']+)[^\s',;]+",
    _re.IGNORECASE,
)
from .store import observations_path, get_instinct, list_instincts, save_instinct
from .types import Instinct, InstinctsConfig, load_instincts_config

_LOCK = threading.Lock()

# 模板化 pattern：工具名 + 参数前 40 字符（脱敏后）作为同类签名。
_SIG_LEN = 40


def _ts() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def observe(event: dict[str, Any]) -> None:
    """幂等追加观察事件到 observations.jsonl。异常全吞——观察失败绝不影响主 turn。"""
    try:
        cfg = load_instincts_config()
        if not (cfg.enabled and cfg.capture):
            return
        p = observations_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        line = dict(event)
        line["ts"] = line.get("ts") or _ts()
        for k in ("args_repr", "before", "after", "action"):
            if isinstance(line.get(k), str):
                v = sanitize_for_memory(line[k])
                line[k] = _SECRET_RE.sub(
                    lambda m: (m.group(1) or m.group(3) or "") + "***", v
                )[:400]
        with _LOCK, p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent_observations(limit: int = 50) -> list[dict[str, Any]]:
    """观察 feed（管理页只读；尾部 limit 条）。"""
    p = observations_path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return []
    out = []
    for ln in lines[-limit:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            continue
    return out


def _tool_denied_signature(ev: dict[str, Any]) -> str:
    tool = str(ev.get("tool") or "?")
    args = str(ev.get("args_repr") or "")[:_SIG_LEN]
    return f"{tool}:{args}"


_TEMPLATE_BEHAVIOR = (
    "执行前先向用户说明将做什么并获得确认；该操作模式此前被用户拒绝过多次"
)


def maybe_analyze(project_slug: str = "", session_id: str = "") -> None:
    """turn 收尾钩子（P0：同步、确定性、零 LLM）。

    同类 tool_denied 签名出现 ≥ template_threshold 次且尚无对应条目 →
    生成 pending 条目；已有条目 → observed+1（封顶 0.9 置信度升级）。
    异常全吞（对齐 maybe_compact_history 容错风格）。
    """
    try:
        cfg = load_instincts_config()
        if not cfg.enabled:
            return
        p = observations_path()
        if not p.exists():
            return
        events: list[dict[str, Any]] = []
        for ln in p.read_text(encoding="utf-8").splitlines():
            try:
                events.append(json.loads(ln))
            except Exception:
                continue
        denied = [e for e in events if e.get("type") == "tool_denied"]
        if not denied:
            return
        counts: Counter[str] = Counter()
        last_by_sig: dict[str, dict[str, Any]] = {}
        for e in denied:
            sig = _tool_denied_signature(e)
            counts[sig] += 1
            last_by_sig[sig] = e

        existing = {i.id: i for i in list_instincts()}
        for sig, n in counts.items():
            if n < cfg.template_threshold:
                continue
            ev = last_by_sig[sig]
            slug = ev.get("project") or project_slug or "default"
            iid = _id_for_sig(sig)
            cur = existing.get(iid) or get_instinct("project", slug, iid)
            if cur is not None and cur.status in ("active", "evolved", "pending"):
                # 强化（P0 幂等口径）：observed = 全量计数；置信度按观察档位
                # 确定性映射（threshold→0.5，每多 1 次 +0.1 封顶 0.9，§7）。
                cur.evidence["observed"] = n
                cur.evidence["last_seen"] = _ts()
                if cur.status != "evolved":
                    cur.confidence = _confidence_for(n, cfg.template_threshold)
                save_instinct(cur)
            elif cur is None:
                tool = str(ev.get("tool") or "?")
                args = str(ev.get("args_repr") or "")[:120]
                inst = Instinct(
                    id=iid,
                    trigger=f"即将执行 {tool}（参数形如「{args}」类）时",
                    behavior=_TEMPLATE_BEHAVIOR,
                    confidence=_confidence_for(n, cfg.template_threshold),
                    domain="tool-usage",
                    scope="project",
                    project_slug=slug,
                    source="permission-deny",
                    status="pending",
                    evidence={
                        "observed": n,
                        "applied": 0,
                        "uncorrected": 0,
                        "corrected": 0,
                        "last_seen": _ts(),
                    },
                )
                save_instinct(inst)
    except Exception:
        pass


def _confidence_for(observed: int, threshold: int) -> float:
    """观察次数 → 置信度档（threshold 次=0.5，每多 1 次 +0.1，封顶 0.9）。"""
    return min(0.9, round(0.5 + 0.1 * max(0, observed - threshold), 2))


def decay_scan(cfg: InstinctsConfig | None = None) -> int:
    """衰减治理（§9 prune + §6 防停摆）：启动维护跑一次，幂等。

    - active 条目 last_seen/updated 超 decay_days 且未再观察 → 置信度 -0.1，
      低于 0.3 自动转 pruned（软删，管理页可找回）；
    - pending 滞留 > 14 天 → 自动过期转 pruned。
    返回处理条数。异常由调用方吞（维护失败不挡启动）。
    """
    from datetime import datetime, timedelta

    from .store import list_instincts, save_instinct
    from .types import load_instincts_config

    c = cfg or load_instincts_config()
    now = datetime.now().astimezone()
    n = 0
    for inst in list_instincts():
        stamp = inst.evidence.get("last_seen") or inst.updated or ""
        try:
            ts = datetime.fromisoformat(stamp)
        except (ValueError, TypeError):
            continue
        if inst.status in ("active", "pending"):
            age = now - ts
            if inst.status == "pending" and age > timedelta(days=14):
                inst.status = "pruned"
                save_instinct(inst)
                n += 1
            elif inst.status == "active" and age > timedelta(days=c.decay_days):
                inst.confidence = round(max(0.0, inst.confidence - 0.1), 2)
                if inst.confidence < 0.3:
                    inst.status = "pruned"
                save_instinct(inst)
                n += 1
    return n


def _id_for_sig(sig: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", sig.lower()).strip("-")[:48]
    return f"deny-{slug}"
