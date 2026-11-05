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
- session: delivery plan (WS-layer short-circuit, at-session-design §5.1).
- result: target session's last assistant reply as ``<mentioned_result>``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import agents as agents_reg
from .. import artifacts as art_store
from ..lang import t
from ..skills.loader import SkillLoader, wrap_skill_body
from .registry import BUILTINS

_log = logging.getLogger("ginno.commands")

MENTION_KINDS = ("artifact", "agent", "session", "result")

# Lookbehind keeps emails (`a@artifact.io`) and paths (`/x/@y`) from matching.
# Labels are single tokens — whitespace-containing labels only work through the
# structured `mentions` list (documented).
_MENTION_TOKEN_RE = re.compile(
    r"(?<![\w/.])@(artifact|agent|session|result)(?::([^\s@:]+))?",
    re.IGNORECASE,
)

# `/name` only counts when it is the very first token of the message — this is
# what makes "/tmp/foo is the path" safe (membership check in parse_slash).
_SLASH_RE = re.compile(r"^\s*/([A-Za-z0-9_-]+)(?:\s+([\s\S]*))?$")


@dataclass
class TurnPlan:
    text: str  # final HumanMessage text (skill-substituted when applicable)
    builtin_reply: str | None = None  # set → skip the graph entirely
    # Async builtin (e.g. /compact): the WS handler must await
    # BUILTINS[builtin_async].async_handler(slug, session, builtin_args) and
    # deliver the reply as a notice. Mutually exclusive with builtin_reply.
    builtin_async: str | None = None
    builtin_args: str = ""
    mention_ctx: list[dict] = field(default_factory=list)  # {kind,id,name,summary}
    # @session 投递计划（at-session-design §5.1）：mentions 全为 session 且有
    # 正文时 WS 层短路投递（不开本会话 turn）。[{id,label}]
    deliveries: list[dict] = field(default_factory=list)
    agent_override: str | None = None  # first resolved @agent mention
    files_extra: list[dict] = field(default_factory=list)  # [{"artifact_id": id}]
    skill_name: str | None = None
    # Mod-registered slash command (design §5.3 command.run): ``(name, tail)``.
    # The WS layer raises the command.run event instead of starting a turn.
    # Never set for a name that shadows a builtin or a skill (P1 不拦截内置).
    mod_command: tuple[str, str] | None = None


def _user_invocable_skill_names(project_slug: str | None) -> set[str]:
    return {
        s.name
        for s in SkillLoader(project_slug=project_slug).load()
        if s.user_invocable()
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
    if not skill.user_invocable():
        return text, None
    if skill.name == "browser":
        # Same gate as use_skill / the skills index: with browser disabled the
        # browser_* tools don't exist — substituting the skill body would hand
        # the model instructions for tools it cannot call.
        from ..world_state import _browser_tools_enabled

        if not _browser_tools_enabled():
            return text, None
    tail = (m.group(2) or "").strip()
    return wrap_skill_body(skill, tail), skill.name


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



def _linked_member(cur_session_id: str | None, sid: str | None = None, label: str | None = None) -> dict | None:
    """@session/@result 的目标解析：在当前会话的 linked members 内按 id 或
    标题精确命中（at-session-design §5.5——集合 ≤32，标题在集合内唯一）。
    排除自身；渠道/subagent 类型在菜单层已挡，这里兜底再排一次。"""
    if not cur_session_id:
        return None
    from ..session_links import linked_report

    rep = linked_report(cur_session_id)
    members = [m for m in (rep.get("members") or []) if m.get("id") != cur_session_id]
    for m in members:
        if (m.get("type") or "").startswith("channel") or m.get("type") == "subagent":
            continue
        if sid is not None and m.get("id") == sid:
            return m
        if label is not None and (m.get("title") or "") == label:
            return m
    if sid or label:
        _log.info("mention: session %r not in linked set — ignored", sid or label)
    return None


def _all_session_metas():
    """全库会话 meta 迭代器（@result 标题兜底用）。独立成函数便于测试注入。"""
    from .. import paths as _paths
    from ..session_meta import _session_meta_list

    for slug_dir in _paths.home().glob("projects/*/sessions/_index.json"):
        slug = slug_dir.parent.parent.name
        for meta in _session_meta_list(slug):
            yield meta, slug


def _result_target(cur_session_id: str | None, sid: str | None = None, label: str | None = None) -> dict | None:
    """@result 目标解析（2026-11-05 反馈修正）：引用语义的 X 常是任意历史
    会话（昨天那个调研），未必与本会话 linked。先走 linked（§5.5 原路径，
    精确 id/标题命中），未命中再按 id/标题在全库 meta 兜底；排除自身 /
    channel / subagent。@session（投递语义）**不走**本兜底——投递范围矩阵
    不变。"""
    m = _linked_member(cur_session_id, sid=sid, label=label)
    if m:
        return m
    if not (sid or label):
        return None
    from .. import paths as _paths
    from ..session_meta import _find_meta, _session_meta_list

    hit = _find_meta(sid) if sid else None
    if hit is None and label:
        for meta, slug in _all_session_metas():
            if (meta.get("title") or "") == label:
                hit = (meta, slug)
                break
    if not hit:
        _log.info("mention: @result target %r not found — ignored", sid or label)
        return None
    meta, slug = hit
    if meta.get("id") == cur_session_id:
        return None
    if (meta.get("type") or "").startswith("channel") or meta.get("type") == "subagent":
        return None
    return {"id": meta["id"], "title": meta.get("title") or meta["id"], "type": meta.get("type")}


def _last_assistant_text_sync(session_id: str, slug: str) -> str:
    """目标会话最后一条 assistant 正文（@result 取数，§5.2：倒序跳过
    tool-only/空文本；同步 checkpoint 读——resolve_turn 是 sync 路径）。
    checkpoint 消息可能是 dict 或 LangChain 对象（get/aget 序列化差异），
    双形态兼容。"""
    try:
        from ..checkpointer import FileCheckpointer

        tup = FileCheckpointer(slug).get_tuple({"configurable": {"thread_id": session_id}})
        messages = (
            (tup.checkpoint.get("channel_values") or {}).get("messages")
            if tup and tup.checkpoint else []
        )
        for m in reversed(messages or []):
            is_dict = isinstance(m, dict)
            mtype = m.get("type") if is_dict else getattr(m, "type", None)
            if mtype != "ai":
                continue
            content = m.get("content") if is_dict else getattr(m, "content", "")
            if isinstance(content, str):
                if content.strip():
                    return content.strip()
                continue
            texts = [
                (b.get("text") if isinstance(b, dict) else getattr(b, "text", "")) or ""
                for b in content
                if isinstance(b, dict) or hasattr(b, "text")
            ]
            joined = "\n".join(t for t in texts if t).strip()
            if joined:
                return joined
        return ""
    except Exception:
        return ""


def _artifact_by_name(slug: str, name: str) -> dict | None:
    hits = [a for a in art_store.list_artifacts(slug) if a.get("name") == name]
    if len(hits) == 1:
        return hits[0]
    if len(hits) > 1:
        _log.warning("mention: artifact name %r is ambiguous (%d hits) — ignored", name, len(hits))
    return None


def resolve_mentions(
    structured: list[dict] | None, text: str, slug: str, cur_session_id: str | None = None
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
        if not mid and kind not in ("session", "result"):
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
        elif kind in ("session", "result"):
            # @result 引用任意会话的结论（2026-11-05）：linked 未命中时全库
            # 兜底；@session 投递语义保持 linked-only。
            m = (
                _result_target(cur_session_id, sid=mid)
                if kind == "result"
                else _linked_member(cur_session_id, sid=mid)
            )
            if m:
                add(kind, m["id"], m.get("title") or m["id"], m)
            else:
                _log.warning("mention: session id %s not in linked set", mid)
        else:
            _log.warning("mention: unknown kind %r ignored", kind)

    # Text fallback: only for kinds the structured list did not cover.
    covered = {r["kind"] for r in resolved}
    for tok in parse_mention_tokens(text):
        kind, label = tok["kind"], tok.get("label")
        if kind in covered:
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
        elif kind in ("session", "result"):
            m = (
                _result_target(cur_session_id, label=label)
                if kind == "result"
                else _linked_member(cur_session_id, label=label)
            )
            if m:
                add(kind, m["id"], m.get("title") or m["id"], m)
                covered.add(kind)

    agent_override = next((r["id"] for r in resolved if r["kind"] == "agent"), None)
    return resolved, agent_override




def resolve_turn(msg: dict, session: dict) -> TurnPlan:
    """Full pipeline: builtin → mentions → skill substitution → TurnPlan."""
    text = msg.get("message", "") or ""
    slug = session.get("project_slug") or "default"

    # 1) Built-in commands short-circuit (no agent resolution, no persistence).
    cmd = parse_slash(text, slug)
    if cmd and cmd[0] in BUILTINS:
        name, tail = cmd
        _log.info("builtin_cmd name=%s slug=%s", name, slug)
        builtin = BUILTINS[name]
        if builtin.async_handler is not None:
            # Executed by the (async) WS handler, not here — resolve_turn is
            # sync and the command does real server work (LLM call, state
            # rewrite). Still short-circuits the graph turn.
            return TurnPlan(text=text, builtin_async=name, builtin_args=tail)
        return TurnPlan(
            text=text, builtin_reply=builtin.handler(slug, session, tail)
        )

    # 1.5) Mod-registered slash command (design §5.3): only when neither the
    # builtin registry nor a user-invocable skill matched (both keep priority).
    # Kept out of parse_slash on purpose — that helper's None must keep meaning
    # "pass through as a plain message" for every other caller.
    if cmd is None:
        from . import mod_commands

        m = _SLASH_RE.match(text or "")
        if m and mod_commands.lookup(m.group(1)):
            name = m.group(1)
            _log.info("mod_cmd name=%s slug=%s", name, slug)
            return TurnPlan(text=text, mod_command=(name, (m.group(2) or "").strip()))

    # 2) Mentions (structured authoritative + text fallback).
    # 内存条目键是 session_id（s_entry），meta 视图是 id——两形态都取。
    resolved, agent_override = resolve_mentions(
        msg.get("mentions"), text, slug,
        cur_session_id=session.get("session_id") or session.get("id"),
    )

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
            summary = f"{t('Kind:', '类型：')} {art.get('kind') or 'artifact'}"
            summary += (
                f"\n{t('Ref:', '引用：')} {ref}"
                if ref
                else f"\n{t('Ref: (none — non-file artifact)', '引用：（无文件 — 非文件类产物）')}"
            )
            plan.mention_ctx.append(
                {"kind": "artifact", "id": r["id"], "name": r["label"], "summary": summary}
            )
            continue
        if kind == "session":
            # 投递计划：不在本会话组 mention_ctx——WS 层短路投递（§5.1）。
            plan.deliveries.append({"id": r["id"], "label": r["label"]})
            continue
        if kind == "result":
            # 引用目标会话最后结论（§5.2）：>4000 字截断 + 尾注；快照语义。
            target = r["_obj"] or {}
            raw = _last_assistant_text_sync(r["id"], slug)
            if raw:
                CUT = 4000
                body = raw if len(raw) <= CUT else raw[:CUT] + "\n…（已截断，全文在原会话）"
                plan.mention_ctx.append(
                    {
                        "kind": "result",
                        "id": r["id"],
                        "name": r["label"],
                        "summary": body,
                    }
                )
            else:
                plan.mention_ctx.append(
                    {
                        "kind": "result",
                        "id": r["id"],
                        "name": r["label"],
                        "summary": t(
                            "(that session has no assistant reply yet)",
                            "（该会话尚无助手回复）",
                        ),
                    }
                )
            continue
    return plan
