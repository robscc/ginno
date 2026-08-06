"""Resolve one raw invoke message into a ``TurnPlan``.

Pipeline (see docs/commands-and-mentions-design.md):

1. Built-in command? → short-circuit reply, no LLM turn.
2. @mentions: the structured ``mentions`` list (from the UI) is authoritative;
   ``@kind:label`` text tokens are a best-effort fallback for raw API clients
   (resolved by name, only when unambiguous).
3. ``/<skill-name> [prompt]`` → SKILL.md body substitution (trigger-gated).

Semantics per kind:
- artifact: file-backed artifacts ride ``attached_files`` (``files_extra``);
  non-file artifacts (empty/missing ``ref``) become a text context section.
- agent: routing override only — no persona context injection.
- workflow: name/description/steps injected as ``<mentioned_workflow>``.
- memory: MEMORY.md content injected as ``<mentioned_memory>`` (skip if empty).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import agents as agents_reg
from .. import artifacts as art_store
from .. import workflows as wf_store
from ..skills.loader import SkillLoader
from ..workflows import dsl as wf_dsl
from .registry import BUILTINS

_log = logging.getLogger("ginno.commands")

MENTION_KINDS = ("artifact", "agent", "workflow", "memory")

# Lookbehind keeps emails (`a@artifact.io`) and paths (`/x/@y`) from matching.
# Labels are single tokens — whitespace-containing labels only work through the
# structured `mentions` list (documented).
_MENTION_TOKEN_RE = re.compile(
    r"(?<![\w/.])@(artifact|agent|workflow|memory)(?::([^\s@:]+))?",
    re.IGNORECASE,
)

# `/name` only counts when it is the very first token of the message — this is
# what makes "/tmp/foo is the path" safe (membership check in parse_slash).
_SLASH_RE = re.compile(r"^\s*/([A-Za-z0-9_-]+)(?:\s+([\s\S]*))?$")


@dataclass
class TurnPlan:
    text: str  # final HumanMessage text (skill-substituted when applicable)
    builtin_reply: str | None = None  # set → skip the graph entirely
    mention_ctx: list[dict] = field(default_factory=list)  # {kind,id,name,summary}
    agent_override: str | None = None  # first resolved @agent mention
    files_extra: list[dict] = field(default_factory=list)  # [{"artifact_id": id}]
    skill_name: str | None = None


def _user_invocable_skill_names(project_slug: str | None) -> set[str]:
    return {
        s.name
        for s in SkillLoader(project_slug=project_slug).load()
        if s.trigger in ("user-invocable", "both")
    }


def parse_slash(text: str, project_slug: str | None) -> tuple[str, str] | None:
    """Return ``(name, tail)`` if the text starts with a KNOWN command/skill.

    Membership-gated: an unknown ``/word`` (e.g. a filesystem path) returns
    ``None`` and the message passes through untouched.
    """
    m = _SLASH_RE.match(text or "")
    if not m:
        return None
    name = m.group(1)
    if name in BUILTINS or name in _user_invocable_skill_names(project_slug):
        return name, (m.group(2) or "").strip()
    return None


def substitute_skill(text: str, project_slug: str | None) -> tuple[str, str | None]:
    """Replace a leading ``/<skill-name>`` with the SKILL.md body.

    Returns ``(new_text, skill_name)`` — unchanged text and ``None`` when the
    first token is not a user-invocable skill. Honors ``skill.trigger``:
    model-invocable-only skills are NOT slash-callable.
    """
    m = _SLASH_RE.match(text or "")
    if not m:
        return text, None
    skill = SkillLoader(project_slug=project_slug).get(m.group(1))
    if not skill or not skill.body:
        return text, None
    if skill.trigger not in ("user-invocable", "both"):
        return text, None
    tail = (m.group(2) or "").strip()
    blocks = [
        f'<skill name="{skill.name}">',
        skill.body.strip(),
        "</skill>",
    ]
    if tail:
        blocks.append(f"\n\nUser request: {tail}")
    else:
        blocks.append("\n\n(Follow the skill instructions above.)")
    return "\n".join(blocks), skill.name


def parse_mention_tokens(text: str) -> list[dict]:
    """Best-effort ``@kind[:label]`` scan for raw-API clients."""
    out: list[dict] = []
    for m in _MENTION_TOKEN_RE.finditer(text or ""):
        out.append({"kind": m.group(1).lower(), "label": m.group(2)})
    return out


def _agent_by_unique_name(name: str):
    hits = [a for a in agents_reg.list_agents() if a.name == name]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _log.warning("mention: agent name %r is ambiguous (%d hits) — ignored", name, len(hits))
    return None


def _workflow_by_unique_name(name: str) -> dict | None:
    hits = [w for w in wf_store.list_defs() if w.get("name") == name]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _log.warning("mention: workflow name %r is ambiguous (%d hits) — ignored", name, len(hits))
    return None


def _artifact_by_name(slug: str, name: str) -> dict | None:
    hits = [a for a in art_store.list_artifacts(slug) if a.get("name") == name]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _log.warning("mention: artifact name %r is ambiguous (%d hits) — ignored", name, len(hits))
    return None


def resolve_mentions(
    structured: list[dict] | None, text: str, slug: str
) -> tuple[list[dict], str | None]:
    """Resolve mentions to ``[{kind, id, label, _obj}]`` + first agent override.

    Structured entries win; text tokens only fill kinds not already covered.
    """
    resolved: list[dict] = []

    def add(kind: str, obj_id: str, label: str, obj: Any) -> None:
        resolved.append({"kind": kind, "id": obj_id, "label": label, "_obj": obj})

    for m in structured or []:
        if not isinstance(m, dict):
            continue
        kind = m.get("kind")
        mid = m.get("id")
        if kind == "memory":
            add("memory", "global", "MEMORY.md", None)
            continue
        if not mid:
            continue
        if kind == "artifact":
            art = art_store.get_artifact(slug, mid)
            if art:
                add("artifact", art["id"], art.get("name") or art["id"], art)
            else:
                _log.warning("mention: artifact id %s not found", mid)
        elif kind == "agent":
            agent = agents_reg.get_agent(mid)
            if agent:
                add("agent", agent.id, agent.name, agent)
            else:
                _log.warning("mention: agent id %s not found", mid)
        elif kind == "workflow":
            wf = wf_store.get_def(mid)
            if wf:
                add("workflow", wf.get("id") or mid, wf.get("name") or mid, wf)
            else:
                _log.warning("mention: workflow id %s not found", mid)
        else:
            _log.warning("mention: unknown kind %r ignored", kind)

    # Text fallback: only for kinds the structured list did not cover.
    covered = {r["kind"] for r in resolved}
    for tok in parse_mention_tokens(text):
        kind, label = tok["kind"], tok.get("label")
        if kind in covered:
            continue
        if kind == "memory":
            add("memory", "global", "MEMORY.md", None)
            covered.add("memory")
            continue
        if not label:
            continue
        if kind == "artifact":
            art = _artifact_by_name(slug, label)
            if art:
                add("artifact", art["id"], art.get("name") or art["id"], art)
                covered.add("artifact")
        elif kind == "agent":
            agent = _agent_by_unique_name(label)
            if agent:
                add("agent", agent.id, agent.name, agent)
                covered.add("agent")
        elif kind == "workflow":
            wf = _workflow_by_unique_name(label)
            if wf:
                add("workflow", wf.get("id") or "", wf.get("name") or "", wf)
                covered.add("workflow")

    agent_override = next((r["id"] for r in resolved if r["kind"] == "agent"), None)
    return resolved, agent_override


def _workflow_summary(wf: dict) -> str:
    lines = []
    if wf.get("description"):
        lines.append(f"描述: {wf['description']}")
    steps = wf.get("steps") or []
    if not steps and wf.get("dsl"):
        steps = wf_dsl.steps_from_dsl(wf.get("dsl") or {})
    if steps:
        lines.append("步骤:")
        for s in steps:
            title = s.get("title") or s.get("id") or ""
            lines.append(f"- {title}")
    return "\n".join(lines)


def resolve_turn(msg: dict, session: dict) -> TurnPlan:
    """Full pipeline: builtin → mentions → skill substitution → TurnPlan."""
    text = msg.get("message", "") or ""
    slug = session.get("project_slug") or "default"

    # 1) Built-in commands short-circuit (no agent resolution, no persistence).
    cmd = parse_slash(text, slug)
    if cmd and cmd[0] in BUILTINS:
        name, tail = cmd
        _log.info("builtin_cmd name=%s slug=%s", name, slug)
        return TurnPlan(
            text=text, builtin_reply=BUILTINS[name].handler(slug, session, tail)
        )

    # 2) Mentions (structured authoritative + text fallback).
    resolved, agent_override = resolve_mentions(msg.get("mentions"), text, slug)

    # 3) Skill substitution on the raw text (tokens in the tail survive).
    new_text, skill_name = substitute_skill(text, slug)

    # 4) Assemble the plan.
    plan = TurnPlan(text=new_text, agent_override=agent_override, skill_name=skill_name)
    for r in resolved:
        kind = r["kind"]
        if kind == "agent":
            continue  # routing-only — no persona context injection
        if kind == "artifact":
            art = r["_obj"] or {}
            ref = (art.get("ref") or "").strip()
            if ref and Path(ref).expanduser().is_file():
                # File-backed → rides attached_files (schema injection etc.).
                # No duplicate mentioned_* section for it.
                plan.files_extra.append({"artifact_id": art["id"]})
                continue
            summary = f"类型: {art.get('kind') or 'artifact'}"
            summary += f"\n引用: {ref}" if ref else "\n引用: （无文件 — 非文件类产物）"
            plan.mention_ctx.append(
                {"kind": "artifact", "id": r["id"], "name": r["label"], "summary": summary}
            )
            continue
        if kind == "workflow":
            plan.mention_ctx.append(
                {
                    "kind": "workflow",
                    "id": r["id"],
                    "name": r["label"],
                    "summary": _workflow_summary(r["_obj"] or {}),
                }
            )
            continue
        if kind == "memory":
            # Lazy import: knowledge.injection drags in the knowledge subpackage;
            # graph.py keeps it out of the startup import graph the same way.
            from ..knowledge.injection import read_global_memory

            content = read_global_memory()
            if content:
                plan.mention_ctx.append(
                    {
                        "kind": "memory",
                        "id": "global",
                        "name": "MEMORY.md",
                        "summary": content,
                    }
                )
            else:
                _log.info("mention: global memory empty — nothing to inject")
    return plan
