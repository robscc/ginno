"""Memory distillation: pool excerpts → LLM draft → human review → MEMORY.md.

Quality gate (design doc G3 spirit, "agent drafts, human approves"): the LLM
never overwrites MEMORY.md silently. ``create_draft`` produces a pending draft
under ``~/.ginno/memory/drafts/``; ``apply_draft`` writes MEMORY.md and clears
the pool only after the user reviewed the diff. Entries captured while a draft
is pending survive via the cutoff timestamp.

Draft queue (docs/dream-mode-design.md §5): the old single-slot
``memory/draft.md`` blocked every later distillation while it waited for
review — the 2026-10-01 draft sat unreviewed for 24 days and the pool grew to
290 entries. Drafts now live one file pair per run, expire after
``draft_ttl_days``, and every run (success / failed / skipped) appends a line to
``memory/runs.jsonl`` so a stalled pipeline is visible instead of silent.

The distillation prompt is KB-aware: the recent wiki usage ledger is fed in so
facts already covered by knowledge pages are not duplicated into memory.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import os
import secrets
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from ginno_runtime import jsonx as json

from .. import paths
from ..graph import text_of_content
from ..lang import t
from ..models import build_model
from .pool import clear_pool, read_pool, sanitize_for_memory

SUMMARIZE_PROMPT = """\
你是一个知识提炼器。你的任务是从对话摘录中提取可长期复用的知识，合并进现有的全局记忆中。

## 提取标准
提取以下类型的知识：
1. **技术决策** — 架构选型、API 约定、配置变更、技术栈选择
2. **问题诊断** — 排查过的 bug、根因、解决方案（只保留通用结论，不要保留调试过程）
3. **用户偏好** — 工作习惯、沟通风格、工具偏好、命名规范
4. **项目上下文** — 正在进行的工作、里程碑、依赖关系、阻塞项
5. **可复用模式** — 反复出现的代码模式、流程、最佳实践

## 过滤标准（不提取）
- 一次性操作细节（"我帮你修了这个文件"这类执行过程）
- 临时调试步骤和错误堆栈
- 闲聊、寒暄、过渡性对话
- 已被后续对话推翻的结论
- 过于具体以至于跨 session 无法复用的信息

## 证据权重
- 摘录中带有 [有引用验证] 标记的内容经过知识库/网页引用核实，证据更强，优先保留
- 无引用支撑的结论仅在反复出现或属于用户偏好时保留

## 与知识库的关系
- 输入若附有「近期知识库使用台账」，说明这些知识页近期被检索或引用过
- 若某条事实已被台账中的知识库页面覆盖，不要重复写入记忆；可在条目后追加\
「（已收录于 KB: <页名>）」，由用户决定是否删除或沉淀为知识页

## 输出要求
- 直接输出合并后的完整记忆，不要输出分析过程
- 按主题分组，使用 Markdown 标题（## 主题）
- 每条知识一行 bullet point
- 如果新知识与现有记忆冲突，以新知识为准，更新旧条目
- 保持精炼，总量控制在 {budget} 字以内
- 使用与源内容相同的语言
"""

SUMMARIZE_PROMPT_EN = """\
You are a knowledge distiller. Your task is to extract knowledge that is reusable \
long-term from conversation excerpts and merge it into the existing global memory.

## Extraction criteria
Extract knowledge of the following kinds:
1. **Technical decisions** — architecture choices, API conventions, configuration \
changes, stack selections
2. **Problem diagnoses** — bugs investigated, root causes, solutions (keep only \
generalizable conclusions; do not keep the debugging trail)
3. **User preferences** — work habits, communication style, tool preferences, naming \
conventions
4. **Project context** — work in progress, milestones, dependencies, blockers
5. **Reusable patterns** — recurring code patterns, workflows, best practices

## Filter criteria (do not extract)
- One-off operational details (execution narration such as "I fixed this file for you")
- Temporary debugging steps and error stack traces
- Small talk, greetings, transitional exchanges
- Conclusions later overturned by the conversation
- Information too specific to be reusable across sessions

## Evidence weight
- Excerpt content carrying the [citation-verified] marker was verified against \
knowledge-base/web citations; the evidence is stronger — keep it with priority
- Conclusions without citation support are kept only when they recur or express user \
preferences

## Relationship to the knowledge base
- If the input carries a "Recent knowledge-base usage ledger", the KB pages in it were \
recently retrieved or cited
- If a fact is already covered by a KB page in that ledger, do not duplicate it into \
memory; you may append "(already in KB: <page name>)" after the entry — the user \
decides whether to delete it or promote it into a knowledge page

## Output requirements
- Output the merged full memory directly; do not narrate your analysis
- Group by topic with Markdown headings (## Topic)
- One bullet point per knowledge item
- When new knowledge conflicts with existing memory, the new knowledge wins; update \
the old entry
- Stay terse; keep the total within {budget} characters
- Use the same language as the source content
"""


# ---------------------------------------------------------------------------
# Draft queue (one file pair per run, single pending at a time)
# ---------------------------------------------------------------------------

PENDING = "pending"
APPLIED = "applied"
DISCARDED = "discarded"
EXPIRED = "expired"


def _atomic_write(path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _drafts_dir():
    d = paths.memory_drafts_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def _draft_paths(draft_id: str):
    d = _drafts_dir()
    return d / f"{draft_id}.md", d / f"{draft_id}.json"


def _save_meta(draft_id: str, meta: dict[str, Any]) -> None:
    _md, meta_path = _draft_paths(draft_id)
    _atomic_write(meta_path, json.dumps(meta, ensure_ascii=False))


def _load_meta(draft_id: str) -> dict[str, Any] | None:
    _md, meta_path = _draft_paths(draft_id)
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return None
    if not isinstance(meta, dict):
        return None
    meta.setdefault("id", draft_id)
    return meta


def _draft_ids() -> list[str]:
    """Draft ids, newest first (file names start with the creation timestamp)."""
    d = paths.memory_drafts_dir()
    if not d.exists():
        return []
    ids = [p.stem for p in d.glob("*.json") if p.is_file()]
    return sorted(ids, reverse=True)


# ---- legacy single-slot migration ------------------------------------------

_MIGRATED = False


def migrate_legacy_draft() -> str | None:
    """Move ``memory/draft.md`` + ``draft.json`` into the queue (once).

    Old builds (and any binary still in a user's app bundle) wrote the single
    slot; folding it in keeps a pending review across the upgrade instead of
    silently losing it or leaving the old path to confuse ``has_draft``.
    """
    md = paths.memory_draft_path()
    if not md.exists():
        # A stale meta without the body is meaningless — drop it.
        paths.memory_draft_meta_path().unlink(missing_ok=True)
        return None
    try:
        text = md.read_text(encoding="utf-8")
        try:
            old = json.loads(paths.memory_draft_meta_path().read_text() or "{}")
        except (OSError, json.JSONDecodeError):
            old = {}
        created = float(old.get("created_at") or md.stat().st_mtime)
        draft_id = f"{int(created)}-legacy"
        meta = {
            **old,
            "id": draft_id,
            "created_at": created,
            "status": PENDING,
            "chars": len(text),
            "trigger": old.get("trigger") or "manual",
        }
        _atomic_write(_draft_paths(draft_id)[0], text)
        _save_meta(draft_id, meta)
        md.unlink(missing_ok=True)
        paths.memory_draft_meta_path().unlink(missing_ok=True)
        return draft_id
    except OSError:
        return None


def _ensure_layout() -> None:
    global _MIGRATED
    if _MIGRATED:
        return
    _MIGRATED = True
    try:
        migrate_legacy_draft()
    except Exception:  # noqa: BLE001 — a broken legacy slot must never break memory
        pass


# ---- reads -----------------------------------------------------------------


def list_drafts(status: str | None = None, limit: int = 0) -> list[dict[str, Any]]:
    """Draft metadata rows, newest first. ``status`` filters by lifecycle."""
    _ensure_layout()
    rows: list[dict[str, Any]] = []
    for did in _draft_ids():
        meta = _load_meta(did)
        if not meta:
            continue
        if status and meta.get("status") != status:
            continue
        rows.append(meta)
        if limit and len(rows) >= limit:
            break
    return rows


def pending_drafts() -> list[dict[str, Any]]:
    return list_drafts(status=PENDING)


def has_draft() -> bool:
    return bool(pending_drafts())


def _latest_pending_id() -> str | None:
    rows = pending_drafts()
    return rows[0]["id"] if rows else None


def _read_draft_text(draft_id: str) -> str | None:
    md, _meta = _draft_paths(draft_id)
    try:
        return md.read_text(encoding="utf-8")
    except OSError:
        return None


def read_draft(draft_id: str | None = None) -> dict[str, Any] | None:
    """Return ``{id, draft, previous, diff, meta}`` or None when nothing pending."""
    _ensure_layout()
    did = draft_id or _latest_pending_id()
    if not did:
        return None
    meta = _load_meta(did)
    draft = _read_draft_text(did)
    if not meta or draft is None:
        return None
    previous = str(meta.get("previous") or "")
    diff = "\n".join(
        difflib.unified_diff(
            previous.splitlines(),
            draft.splitlines(),
            fromfile="MEMORY.md（当前）",
            tofile="MEMORY.md（草稿）",
            lineterm="",
        )
    )
    return {"id": did, "draft": draft, "previous": previous, "diff": diff, "meta": meta}


def _live_budget() -> int:
    """Current configured draft budget. Pending drafts are judged by THIS, not
    the budget frozen into their meta at creation — a config change must
    re-label the queue immediately (and apply_draft's interception line moves
    with it), instead of every pending row staying stale until the next run."""
    try:
        from ..knowledge.config import load_knowledge_config

        return int(load_knowledge_config().memory_budget_chars or 3000)
    except Exception:  # noqa: BLE001
        return 3000


def draft_payload(draft_id: str | None = None) -> dict[str, Any]:
    """Draft review payload shared by the list/detail endpoints and create/reuse."""
    d = read_draft(draft_id)
    if not d:
        return {"draft": None}
    meta = d["meta"]
    pending = meta.get("status", PENDING) == PENDING
    budget = _live_budget() if pending else int(meta.get("budget") or 3000)
    chars = len(d["draft"])
    return {
        "id": d["id"],
        "draft": d["draft"],
        "previous": d["previous"],
        "diff": d["diff"],
        "pool_entries": meta.get("pool_entries", 0),
        "pool_cutoff": meta.get("pool_cutoff"),
        "created_at": meta.get("created_at"),
        "trigger": meta.get("trigger", "manual"),
        "model": meta.get("model") or "",
        "provider": meta.get("provider") or "",
        "status": meta.get("status", PENDING),
        "budget": budget,
        "chars": chars,
        "over_budget": chars > budget,
    }


def draft_summary(meta: dict[str, Any]) -> dict[str, Any]:
    """List-row projection (no draft body / diff)."""
    # Pending rows follow the live config budget; resolved rows keep the budget
    # frozen at creation — it is the historical fact of what they were judged by.
    pending = meta.get("status", PENDING) == PENDING
    budget = _live_budget() if pending else int(meta.get("budget") or 3000)
    chars = int(meta.get("chars") or 0)
    return {
        "id": meta.get("id"),
        "created_at": meta.get("created_at"),
        "resolved_at": meta.get("resolved_at"),
        "trigger": meta.get("trigger", "manual"),
        "provider": meta.get("provider") or "",
        "model": meta.get("model") or "",
        "pool_entries": meta.get("pool_entries", 0),
        "budget": budget,
        "chars": chars,
        "over_budget": chars > budget,
        "status": meta.get("status", PENDING),
        "expired_reason": meta.get("expired_reason"),
    }


# ---- run log ---------------------------------------------------------------


def record_run(**fields: Any) -> None:
    """Append one distillation-run line. Never raises (telemetry)."""
    try:
        entry = {"ts": time.time(), **fields}
        p = paths.memory_runs_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass


def run_history(limit: int = 50) -> list[dict[str, Any]]:
    """Newest-first run rows (bounded read: only the tail is parsed)."""
    p = paths.memory_runs_path()
    if not p.exists():
        return []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()[-max(limit, 1) * 2 :]
    except OSError:
        return []
    rows: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    # Appends are chronological in production, but a hand-edited/imported log
    # must not make "上次蒸馏" show an older row — order by timestamp.
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return rows[:limit] if limit > 0 else rows


def trim_runs(days: int = 90) -> int:
    """Drop run rows older than ``days`` (called at startup). Returns removed."""
    p = paths.memory_runs_path()
    if not p.exists():
        return 0
    cutoff = time.time() - days * 86400
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    kept, removed = [], 0
    for line in lines:
        if not line.strip():
            continue
        try:
            ts = float(json.loads(line).get("ts") or 0)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        if ts >= cutoff:
            kept.append(line)
        else:
            removed += 1
    if removed:
        _atomic_write(p, "\n".join(kept) + ("\n" if kept else ""))
    return removed


# ---- lifecycle ---------------------------------------------------------------


def _resolve(draft_id: str, status: str, reason: str | None = None) -> bool:
    meta = _load_meta(draft_id)
    if not meta or meta.get("status") != PENDING:
        return False
    meta["status"] = status
    meta["resolved_at"] = time.time()
    if reason:
        meta["expired_reason"] = reason
    _save_meta(draft_id, meta)
    return True


def expire_stale_drafts(ttl_days: int | None = None, max_pending: int | None = None) -> list[str]:
    """Expire pending drafts past their TTL (and beyond the pending cap).

    This is the guard that makes the 2026-10-01 stall impossible: a draft
    nobody reviews stops blocking the pipeline instead of freezing it forever.
    Returns the expired ids.

    ``ttl_days = 0`` disables the TTL; ``max_pending = 0`` disables the cap.
    """
    _ensure_layout()
    from ..knowledge.config import load_knowledge_config

    cfg = load_knowledge_config()
    ttl = int(cfg.draft_ttl_days if ttl_days is None else ttl_days)
    cap = int(cfg.draft_max_pending if max_pending is None else max_pending)
    now = time.time()
    expired: list[str] = []

    def _expire(meta: dict[str, Any], reason: str) -> None:
        did = meta["id"]
        if _resolve(did, EXPIRED, reason):
            expired.append(did)
            record_run(
                run_id=f"r-{int(now)}",
                trigger=meta.get("trigger") or "auto",
                status="skipped",
                reason=reason,
                draft_id=did,
                pool_entries=meta.get("pool_entries", 0),
                chars=meta.get("chars", 0),
            )

    for meta in pending_drafts():
        created = float(meta.get("created_at") or 0)
        if ttl > 0 and created and now - created > ttl * 86400:
            _expire(meta, "expired_ttl")

    remaining = pending_drafts()  # newest first
    if cap > 0 and len(remaining) > cap:
        for meta in remaining[cap:]:
            _expire(meta, "evicted_capacity")

    _trim_draft_files()
    try:
        from .status import record_snapshot

        record_snapshot()
    except Exception:  # noqa: BLE001 — a snapshot is cosmetic
        pass
    return expired


def _trim_draft_files() -> None:
    """Keep only the newest ``draft_keep`` drafts on disk (resolved ones go first)."""
    from ..knowledge.config import load_knowledge_config

    keep = int(load_knowledge_config().draft_keep or 0)
    if keep <= 0:
        return
    rows = list_drafts()  # newest first, includes resolved
    if len(rows) <= keep:
        return
    for meta in rows[keep:]:
        if meta.get("status") == PENDING:  # never drop an unreviewed draft here
            continue
        for p in _draft_paths(str(meta["id"])):
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# MEMORY.md read/write
# ---------------------------------------------------------------------------


def _read_existing_memory() -> str:
    """Read existing MEMORY.md (or empty string if not present)."""
    p = paths.memory_index_path()
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    # Skip the default boilerplate
    if text.startswith("# Ginno Memory"):
        return ""
    return text


def _write_memory(text: str) -> None:
    """Write sanitized memory text to MEMORY.md."""
    sanitized = sanitize_for_memory(text)
    paths.memory_index_path().write_text(sanitized, encoding="utf-8")


# ---------------------------------------------------------------------------
# Distillation (draft creation)
# ---------------------------------------------------------------------------

_DISTILL_LOCK = asyncio.Lock()


def _kb_digest() -> str:
    """Recent KB usage ledger digest (B1). Best-effort: a broken KB must never
    break distillation."""
    try:
        from ..knowledge.config import load_knowledge_config

        cfg = load_knowledge_config()
        if not cfg.usable:
            return ""
        from ..knowledge import usage as kb_usage

        rows = kb_usage.top(sort="cited", limit=10)
        lines = [
            t(
                f"- {r['path']} (injected x{r.get('injected', 0)}, cited x{r.get('cited', 0)})",
                f"- {r['path']}（注入×{r.get('injected', 0)}，引用×{r.get('cited', 0)}）",
            )
            for r in rows
        ]
        if not lines:
            return ""
        return (
            t(
                "## Recent knowledge-base usage ledger (injected/cited counts)\n",
                "## 近期知识库使用台账（注入/引用次数）\n",
            )
            + "\n".join(lines)
            + t(
                "\n(Facts already covered by the pages above do not need to be written "
                "into memory again.)\n",
                "\n（与上述页面重复的事实不必再写入记忆）\n",
            )
        )
    except Exception:
        return ""


def _model_name(model: Any, cfg: dict[str, Any] | None = None) -> str:
    """Best-effort model label for telemetry (never raises)."""
    for attr in ("model_name", "model", "model_id"):
        v = getattr(model, attr, None)
        if isinstance(v, str) and v:
            return v
    try:
        from .. import providers as prov_mod

        if cfg:
            return prov_mod.model_for_config(cfg)
    except Exception:  # noqa: BLE001
        pass
    return ""


async def create_draft(trigger: str = "manual", provider: str | None = None) -> dict:
    """Distill the pool into a pending draft (never touches MEMORY.md/pool).

    Returns the draft review payload, ``draft_exists`` when a draft is already
    pending (reused, no second LLM call), or ``{message: "pool empty"}``.
    """
    _ensure_layout()
    expire_stale_drafts()

    if has_draft():
        return {"ok": True, "draft_exists": True, **draft_payload()}

    pool = read_pool()
    if not pool:
        return {"ok": True, "pool_entries": 0, "message": "pool empty"}

    if _DISTILL_LOCK.locked():
        # Another distillation is mid-flight (manual while auto runs, etc.).
        if trigger == "auto":
            return {"ok": False, "skipped": "distill_in_progress"}
        return {"ok": False, "error": "distill_in_progress"}

    async with _DISTILL_LOCK:
        if has_draft():  # raced another trigger while waiting for the lock
            return {"ok": True, "draft_exists": True, **draft_payload()}
        pool = read_pool()
        if not pool:
            return {"ok": True, "pool_entries": 0, "message": "pool empty"}

        from .. import providers as prov_mod
        from ..knowledge.config import load_knowledge_config

        cfg = load_knowledge_config()
        budget = int(cfg.memory_budget_chars or 3000)
        chosen = provider or (cfg.summarize_model or None) or _get_default_provider()
        # summarize_model 选供应商,summarize_model_id 才是具体模型;空 = 供应商默认
        model_override = (cfg.summarize_model_id or "").strip() or None
        run_id = f"r-{int(time.time())}"
        started = time.time()
        prov_cfg: dict[str, Any] = {}
        try:
            prov_cfg = prov_mod.load_providers().get(chosen) or {}
        except Exception:  # noqa: BLE001
            prov_cfg = {}
        model_label = ""
        try:
            model = build_model(chosen, model_override)
        except Exception as e:
            record_run(
                run_id=run_id,
                trigger=trigger,
                status="failed",
                reason="model_build_failed",
                error=str(e)[:300],
                provider=chosen,
                pool_entries=len(pool),
            )
            return {"ok": False, "error": f"model build failed: {e}", "pool_entries": len(pool)}
        model_label = _model_name(model, prov_cfg)

        existing = _read_existing_memory()
        excerpt_lines = []
        for e in pool:
            content = e.get("content", "")
            if not content:
                continue
            if e.get("cited"):
                content = t("[citation-verified] ", "[有引用验证] ") + content
            excerpt_lines.append(content)
        excerpts = "\n\n---\n\n".join(excerpt_lines)

        digest = _kb_digest()
        digest_block = f"\n\n{digest}" if digest else ""

        usage: dict[str, Any] = {}
        try:
            response = await model.ainvoke([
                SystemMessage(
                    content=t(SUMMARIZE_PROMPT_EN, SUMMARIZE_PROMPT).format(budget=budget)
                ),
                HumanMessage(content=(
                    f"## Existing Memory\n{existing or '(empty)'}\n\n"
                    f"## New Conversation Excerpts\n{excerpts}"
                    f"{digest_block}\n\n"
                    "Please produce an updated memory summary."
                )),
            ])
            from .. import usage as usage_mod

            usage = usage_mod.extract_usage(response) or {}
        except Exception as e:
            record_run(
                run_id=run_id,
                trigger=trigger,
                status="failed",
                reason="summarization_failed",
                error=str(e)[:300],
                provider=chosen,
                model=model_label,
                duration_ms=int((time.time() - started) * 1000),
                pool_entries=len(pool),
            )
            return {"ok": False, "error": f"summarization failed: {e}", "pool_entries": len(pool)}

        # Usage telemetry: distillation has its own bucket (source="memory") so
        # its cost is visible next to chat in Settings → 用量统计.
        try:
            from .. import usage_store

            if usage:
                usage_store.record(
                    input_tokens=usage.get("input_tokens") or 0,
                    output_tokens=usage.get("output_tokens") or 0,
                    cache_read_tokens=usage.get("cache_read_tokens") or 0,
                    cache_creation_tokens=usage.get("cache_creation_tokens") or 0,
                    provider=chosen or "",
                    model=model_label,
                    source="memory",
                    turn_id=run_id,
                    latency_ms=int((time.time() - started) * 1000),
                )
        except Exception:  # noqa: BLE001 — telemetry never breaks distillation
            pass

        new_memory = sanitize_for_memory(text_of_content(response.content))
        if not new_memory.strip():
            record_run(
                run_id=run_id,
                trigger=trigger,
                status="failed",
                reason="empty_draft",
                provider=chosen,
                model=model_label,
                duration_ms=int((time.time() - started) * 1000),
                pool_entries=len(pool),
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
            )
            return {"ok": False, "error": "summarization returned empty", "pool_entries": len(pool)}

        cutoff = max(float(e.get("timestamp") or 0) for e in pool)
        draft_id = f"{int(time.time())}-{secrets.token_hex(2)}"
        meta = {
            "id": draft_id,
            "created_at": time.time(),
            "trigger": trigger,
            "provider": chosen,
            "model": model_label,
            "pool_entries": len(pool),
            "pool_cutoff": cutoff,
            "budget": budget,
            "chars": len(new_memory),
            "previous": existing,
            "previous_sha1": hashlib.sha1(existing.encode("utf-8")).hexdigest(),
            "status": PENDING,
        }
        _atomic_write(_draft_paths(draft_id)[0], new_memory)
        _save_meta(draft_id, meta)
        record_run(
            run_id=run_id,
            trigger=trigger,
            status="success",
            provider=chosen,
            model=model_label,
            duration_ms=int((time.time() - started) * 1000),
            input_tokens=usage.get("input_tokens") or 0,
            output_tokens=usage.get("output_tokens") or 0,
            cache_read_tokens=usage.get("cache_read_tokens") or 0,
            pool_entries=len(pool),
            draft_id=draft_id,
            chars=len(new_memory),
        )
        return {"ok": True, **draft_payload(draft_id)}


def split_draft(draft_id: str, keep_headings: list[str]) -> dict:
    """Turn a subset of the draft's sections into a NEW pending draft.

    The over-budget escape hatch that beats mid-line truncation: the user keeps
    whole ``##`` sections, a smaller draft is queued (no extra LLM call), and
    the original is marked discarded. Returns the new draft payload.
    """
    d = read_draft(draft_id)
    if not d:
        return {"ok": False, "error": "no_draft"}
    meta = d["meta"]
    if meta.get("status") != PENDING:
        return {"ok": False, "error": "not_pending"}

    want = {h.strip() for h in (keep_headings or []) if h and h.strip()}
    if not want:
        return {"ok": False, "error": "empty_selection"}
    # Accept headings with or without the leading "## ".
    want = {h if h.startswith("## ") else f"## {h.lstrip('# ')}" for h in want}

    preamble: list[str] = []
    sections: list[tuple[str, list[str]]] = []
    cur: tuple[str, list[str]] | None = None
    for line in d["draft"].splitlines():
        if line.startswith("## "):
            cur = (line, [])
            sections.append(cur)
        elif cur is None:
            preamble.append(line)
        else:
            cur[1].append(line)

    parts: list[str] = []
    if any(ln.strip() for ln in preamble):
        parts.append("\n".join(preamble).strip("\n"))
    for heading, body in sections:
        if heading in want:
            parts.append("\n".join([heading, *body]).strip("\n"))
    new_text = "\n\n".join(parts).strip()
    if not new_text:
        return {"ok": False, "error": "empty_selection"}

    new_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    new_meta = {
        **meta,
        "id": new_id,
        "created_at": time.time(),
        "trigger": "split",
        "chars": len(new_text),
        "split_from": draft_id,
        "status": PENDING,
        "resolved_at": None,
        "expired_reason": None,
    }
    _atomic_write(_draft_paths(new_id)[0], new_text)
    _save_meta(new_id, new_meta)
    _resolve(draft_id, DISCARDED)
    return {"ok": True, **draft_payload(new_id)}


def truncate_to_budget(text: str, budget: int) -> str:
    """Cut ``text`` to ``budget`` characters on a line boundary (never mid-line)."""
    if budget <= 0 or len(text) <= budget:
        return text
    out, used = [], 0
    for line in text.splitlines():
        if used + len(line) + 1 > budget:
            break
        out.append(line)
        used += len(line) + 1
    if not out:  # single huge line — hard cut is the only option
        return text[:budget]
    return "\n".join(out)


def apply_draft(
    content: str | None = None,
    force: bool = False,
    draft_id: str | None = None,
    truncate: bool = False,
) -> dict:
    """Write the (optionally edited) draft into MEMORY.md and clear the pool
    up to the draft's cutoff. Refuses when MEMORY.md changed since the draft
    was created, unless ``force``; refuses an over-budget draft unless
    ``truncate`` or ``force``."""
    d = read_draft(draft_id)
    if not d:
        return {"ok": False, "error": "no_draft"}
    meta = d["meta"]

    current = _read_existing_memory()
    current_sha = hashlib.sha1(current.encode("utf-8")).hexdigest()
    if not force and meta.get("previous_sha1") and current_sha != meta["previous_sha1"]:
        return {"ok": False, "error": "memory_changed"}

    text = content if content is not None else d["draft"]
    budget = _live_budget()  # 拦截线跟当前配置走,不用创建时冻结的 meta 值
    if budget > 0 and len(text) > budget and not force:
        if not truncate:
            return {
                "ok": False,
                "error": "over_budget",
                "chars": len(text),
                "budget": budget,
            }
        text = truncate_to_budget(text, budget)

    _write_memory(text)
    cutoff = meta.get("pool_cutoff")
    clear_pool(before_ts=float(cutoff) if cutoff else None)
    remaining = len(read_pool())
    _resolve(str(meta["id"]), APPLIED)
    written = sanitize_for_memory(text)
    return {
        "ok": True,
        "draft_id": meta["id"],
        "summarized_chars": len(written),
        "pool_entries": meta.get("pool_entries", 0),
        "pool_remaining": remaining,
    }


def discard_draft(draft_id: str | None = None) -> dict:
    """Drop the pending draft; the pool is kept and can be re-distilled."""
    _ensure_layout()
    did = draft_id or _latest_pending_id()
    if not did:
        return {"ok": True, "had_draft": False}
    had = _resolve(did, DISCARDED)
    return {"ok": True, "had_draft": had, "draft_id": did}


def remove_memory_lines(lines: list[str]) -> int:
    """Remove exact-match lines from MEMORY.md (a section promoted into the
    knowledge base is no longer memory). Returns the number of lines removed."""
    targets = {ln.strip() for ln in (lines or []) if ln and ln.strip()}
    if not targets:
        return 0
    p = paths.memory_index_path()
    if not p.exists():
        return 0
    text = p.read_text(encoding="utf-8")
    src_lines = text.splitlines()
    kept = [ln for ln in src_lines if ln.strip() not in targets]
    removed = len(src_lines) - len(kept)
    if removed:
        suffix = "\n" if text.endswith("\n") else ""
        p.write_text("\n".join(kept) + suffix, encoding="utf-8")
    return removed


def _get_default_provider() -> str:
    """Get the default enabled provider (fallback to 'custom')."""
    from .. import providers as prov_mod

    return prov_mod.get_default_provider()
