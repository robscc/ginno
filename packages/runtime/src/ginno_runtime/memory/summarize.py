"""Memory distillation: pool excerpts → LLM draft → human review → MEMORY.md.

Quality gate (design doc G3 spirit, "agent drafts, human approves"): the LLM
no longer overwrites MEMORY.md silently. ``create_draft`` produces a pending
draft (``~/.ginno/memory/draft.md`` + ``draft.json``); ``apply_draft`` writes
MEMORY.md and clears the pool only after the user reviewed the diff. Entries
captured while a draft is pending survive via the cutoff timestamp.

The distillation prompt is KB-aware: the recent wiki usage ledger is fed in so
facts already covered by knowledge pages are not duplicated into memory.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import time
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

from .. import paths
from ..graph import text_of_content
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


# ---------------------------------------------------------------------------
# Draft slot (single pending draft — one distillation reviewed at a time)
# ---------------------------------------------------------------------------

def _atomic_write(path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def has_draft() -> bool:
    return paths.memory_draft_path().exists()


def _read_meta() -> dict[str, Any]:
    p = paths.memory_draft_meta_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}


def read_draft() -> dict[str, Any] | None:
    """Return ``{draft, previous, diff, meta}`` or None when no draft pending."""
    if not has_draft():
        return None
    try:
        draft = paths.memory_draft_path().read_text(encoding="utf-8")
    except OSError:
        return None
    meta = _read_meta()
    previous = meta.get("previous", "")
    diff = "\n".join(difflib.unified_diff(
        previous.splitlines(), draft.splitlines(),
        fromfile="MEMORY.md（当前）", tofile="MEMORY.md（草稿）", lineterm="",
    ))
    return {"draft": draft, "previous": previous, "diff": diff, "meta": meta}


def draft_payload() -> dict[str, Any]:
    """Draft review payload shared by GET /api/memory/draft and create/reuse."""
    d = read_draft()
    if not d:
        return {"draft": None}
    meta = d["meta"]
    budget = int(meta.get("budget") or 3000)
    chars = len(d["draft"])
    return {
        "draft": d["draft"],
        "previous": d["previous"],
        "diff": d["diff"],
        "pool_entries": meta.get("pool_entries", 0),
        "pool_cutoff": meta.get("pool_cutoff"),
        "created_at": meta.get("created_at"),
        "trigger": meta.get("trigger", "manual"),
        "budget": budget,
        "chars": chars,
        "over_budget": chars > budget,
    }


def _delete_draft() -> None:
    for p in (paths.memory_draft_path(), paths.memory_draft_meta_path()):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


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
            f"- {r['path']}（注入×{r.get('injected', 0)}，引用×{r.get('cited', 0)}）"
            for r in rows
        ]
        if not lines:
            return ""
        return (
            "## 近期知识库使用台账（注入/引用次数）\n"
            + "\n".join(lines)
            + "\n（与上述页面重复的事实不必再写入记忆）\n"
        )
    except Exception:
        return ""


async def create_draft(trigger: str = "manual", provider: str | None = None) -> dict:
    """Distill the pool into a pending draft (never touches MEMORY.md/pool).

    Returns the draft review payload, ``draft_exists`` when a draft is already
    pending (reused, no second LLM call), or ``{message: "pool empty"}``.
    """
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

        from ..knowledge.config import load_knowledge_config

        cfg = load_knowledge_config()
        budget = int(cfg.memory_budget_chars or 3000)
        chosen = provider or (cfg.summarize_model or None) or _get_default_provider()
        try:
            model = build_model(chosen)
        except Exception as e:
            return {"ok": False, "error": f"model build failed: {e}", "pool_entries": len(pool)}

        existing = _read_existing_memory()
        excerpt_lines = []
        for e in pool:
            content = e.get("content", "")
            if not content:
                continue
            if e.get("cited"):
                content = "[有引用验证] " + content
            excerpt_lines.append(content)
        excerpts = "\n\n---\n\n".join(excerpt_lines)

        digest = _kb_digest()
        digest_block = f"\n\n{digest}" if digest else ""

        try:
            response = await model.ainvoke([
                SystemMessage(content=SUMMARIZE_PROMPT.format(budget=budget)),
                HumanMessage(content=(
                    f"## Existing Memory\n{existing or '(empty)'}\n\n"
                    f"## New Conversation Excerpts\n{excerpts}"
                    f"{digest_block}\n\n"
                    "Please produce an updated memory summary."
                )),
            ])
        except Exception as e:
            return {"ok": False, "error": f"summarization failed: {e}", "pool_entries": len(pool)}

        new_memory = sanitize_for_memory(text_of_content(response.content))
        if not new_memory.strip():
            return {"ok": False, "error": "summarization returned empty", "pool_entries": len(pool)}

        cutoff = max(float(e.get("timestamp") or 0) for e in pool)
        meta = {
            "created_at": time.time(),
            "trigger": trigger,
            "provider": chosen,
            "pool_entries": len(pool),
            "pool_cutoff": cutoff,
            "budget": budget,
            "previous": existing,
            "previous_sha1": hashlib.sha1(existing.encode("utf-8")).hexdigest(),
        }
        _atomic_write(paths.memory_draft_path(), new_memory)
        _atomic_write(paths.memory_draft_meta_path(), json.dumps(meta, ensure_ascii=False))
        return {"ok": True, **draft_payload()}


def apply_draft(content: str | None = None, force: bool = False) -> dict:
    """Write the (optionally edited) draft into MEMORY.md and clear the pool
    up to the draft's cutoff. Refuses when MEMORY.md changed since the draft
    was created, unless ``force``."""
    d = read_draft()
    if not d:
        return {"ok": False, "error": "no_draft"}
    meta = d["meta"]

    current = _read_existing_memory()
    current_sha = hashlib.sha1(current.encode("utf-8")).hexdigest()
    if not force and meta.get("previous_sha1") and current_sha != meta["previous_sha1"]:
        return {"ok": False, "error": "memory_changed"}

    text = content if content is not None else d["draft"]
    _write_memory(text)
    cutoff = meta.get("pool_cutoff")
    clear_pool(before_ts=float(cutoff) if cutoff else None)
    remaining = len(read_pool())
    _delete_draft()
    written = sanitize_for_memory(text)
    return {
        "ok": True,
        "summarized_chars": len(written),
        "pool_entries": meta.get("pool_entries", 0),
        "pool_remaining": remaining,
    }


def discard_draft() -> dict:
    """Drop the pending draft; the pool is kept and can be re-distilled."""
    existed = has_draft()
    _delete_draft()
    return {"ok": True, "had_draft": existed}


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
