"""Session → reusable asset distillation (V2 P1-2).

Most sessions are one-shot Q&A that sink without a trace. This turns a good
one into something reusable:

* ``skill`` — a draft SKILL.md built from the tool-call sequence (deterministic,
  no LLM call, so it never fails offline);
* ``kb`` — a Markdown card written into the knowledge vault's Raw folder;
* ``workflow`` — not implemented here: the existing
  ``POST /api/workflows/summarize-from-session`` already drafts one, the UI
  links to it.

The session meta records ``distilled_to`` so the UI can stop offering it.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import paths
from ..session_meta import _find_meta, _session_meta_patch

router = APIRouter()


class DistillRequest(BaseModel):
    kind: str = "skill"  # "skill" | "kb"


def _slug(text: str) -> str:
    s = str(text or "").strip().lower()
    s = re.sub(r"[^\w一-鿿-]+", "-", s, flags=re.UNICODE)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return (s or "session-skill")[:60]


def _messages(slug: str, session_id: str) -> list:
    from ..checkpointer import FileCheckpointer

    tup = FileCheckpointer(slug).get_tuple(
        {"configurable": {"thread_id": session_id}}
    )
    if not tup or not tup.checkpoint:
        return []
    return (tup.checkpoint.get("channel_values") or {}).get("messages") or []


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(b.get("text") or "")
            for b in content
            if isinstance(b, dict) and b.get("text")
        )
    return ""


def _collect(messages: list) -> tuple[str, list[dict], list[str]]:
    first_user = ""
    steps: list[dict] = []
    tools: list[str] = []
    for m in messages:
        mt = getattr(m, "type", "")
        if mt == "human" and not first_user:
            t = _text(getattr(m, "content", "")).strip()
            if t and not t.startswith(("[turn context]", "[conversation summary]")):
                first_user = t
        for tc in getattr(m, "tool_calls", None) or []:
            name = str(tc.get("name") or "")
            if not name:
                continue
            if name not in tools:
                tools.append(name)
            args = json.dumps(tc.get("args") or {}, ensure_ascii=False)
            steps.append({"name": name, "args": args[:200]})
    return first_user, steps, tools


def _skill_md(name: str, title: str, description: str, steps: list[dict], tools: list[str], sid: str) -> str:
    lines = [
        "---",
        f"name: {name}",
        f"description: {description[:200]}",
        "trigger: user-invocable",
        f"tools: [{', '.join(tools[:8])}]",
        "---",
        "",
        f"# {title}",
        "",
        f"> 由会话 `{sid[:8]}` 于 {time.strftime('%Y-%m-%d')} 沉淀为草稿，使用前请按实际需要调整。",
        "",
        "## 工作流",
        "",
    ]
    if steps:
        for i, s in enumerate(steps[:20], 1):
            lines.append(f"{i}. `{s['name']}` — `{s['args']}`")
    else:
        lines.append("（该会话没有工具调用，请补充步骤。）")
    lines += [
        "",
        "## 完成标准",
        "",
        "按上面步骤执行，产出与原始会话一致的可用结果；遇到输入缺失时先询问用户。",
        "",
    ]
    return "\n".join(lines)


@router.post("/api/sessions/{session_id}/distill")
async def distill_session(session_id: str, req: DistillRequest) -> dict:
    found = _find_meta(session_id)
    if not found:
        raise HTTPException(
            status_code=404, detail={"ok": False, "error": "unknown session"}
        )
    meta, slug = found
    kind = (req.kind or "").strip()
    title = str(meta.get("title") or session_id)
    messages = _messages(slug, session_id)
    first_user, steps, tools = _collect(messages)

    if kind == "skill":
        name = _slug(title)
        target = paths.global_skills_dir() / name / "SKILL.md"
        if target.exists():
            name = f"{name}-{session_id[:6]}"
            target = paths.global_skills_dir() / name / "SKILL.md"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                _skill_md(
                    name,
                    title,
                    first_user or title,
                    steps,
                    tools,
                    session_id,
                ),
                encoding="utf-8",
            )
        except OSError as e:
            raise HTTPException(
                status_code=500, detail={"ok": False, "error": f"write failed: {e}"}
            ) from e
        _mark(meta, slug, session_id, "skill", str(target))
        return {"ok": True, "kind": "skill", "name": name, "path": str(target)}

    if kind == "kb":
        try:
            from ..knowledge.config import load_knowledge_config

            cfg = load_knowledge_config()
        except Exception as e:  # noqa: BLE001
            raise HTTPException(
                status_code=400, detail={"ok": False, "error": f"knowledge unavailable: {e}"}
            ) from e
        vault = Path(cfg.vault_path).expanduser()
        raw = vault / cfg.raw_dir
        if not cfg.usable or not vault.is_dir():
            raise HTTPException(
                status_code=400,
                detail={"ok": False, "error": "knowledge vault not configured"},
            )
        name = f"{time.strftime('%Y-%m-%d')}-{_slug(title)}.md"
        target = raw / name
        body = [
            "---",
            f"title: {title}",
            "tags: [distilled, session]",
            f"source_session: {session_id}",
            f"date: {time.strftime('%Y-%m-%d')}",
            "---",
            "",
            f"# {title}",
            "",
            "## 问题",
            "",
            first_user or "（无首条用户消息）",
            "",
            "## 关键步骤",
            "",
        ]
        for i, s in enumerate(steps[:20], 1):
            body.append(f"{i}. `{s['name']}` — `{s['args']}`")
        body += ["", "## 结论", "", "（在此补充可复用的结论。）", ""]
        try:
            raw.mkdir(parents=True, exist_ok=True)
            target.write_text("\n".join(body), encoding="utf-8")
        except OSError as e:
            raise HTTPException(
                status_code=500, detail={"ok": False, "error": f"write failed: {e}"}
            ) from e
        _mark(meta, slug, session_id, "kb", str(target))
        return {"ok": True, "kind": "kb", "path": str(target)}

    if kind == "workflow":
        return {
            "ok": True,
            "kind": "workflow",
            "hint": "POST /api/workflows/summarize-from-session",
        }

    raise HTTPException(
        status_code=400, detail={"ok": False, "error": f"unknown kind: {kind}"}
    )


def _mark(meta: dict, slug: str, session_id: str, kind: str, where: str) -> None:
    marks = list(meta.get("distilled_to") or [])
    marks.append({"kind": kind, "path": where, "at": time.time()})
    _session_meta_patch(slug, session_id, {"distilled_to": marks}, touch=False)
