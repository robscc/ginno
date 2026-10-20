"""团队记忆分层（agent-team-design.md §5.4，P2 尾巴 2026-10-14 实施）。

三层注入边界：

| 层 | 位置 | 注入范围 |
|---|---|---|
| agent 记忆 | agents 目录 MEMORY.md | 该 agent 的所有会话（既有） |
| **实例记忆** | ``teams/<id>/members/<iid>/notes.md`` | 仅该成员实例的 turn |
| **团队共享记忆** | ``teams/<id>/TEAM.md`` | 团队全部成员 + 主会话的 turn |

成员**不共享对话上下文**（隔离价值），只共享 TEAM.md + 共享 workspace。
TEAM.md 由用户在团队设置里维护（治理模型同 GINNO.md：用户改，或 Lead
提议、用户确认——本期只做用户直编）。

注入走 ``build_turn_context`` 的 per-turn 挥发层（``wrap_context_section``
"team_context" / "member_notes"，与 ``injected_wiki`` 同范式）而非稳定
系统提示——TEAM.md 改动不失效缓存前缀。
"""

from __future__ import annotations

import os

from .. import paths
from ..lang import t

# 注入上限：记忆是约定不是知识库，超限截断（全文留在文件里，模型可用
# 文件工具读）。TEAM.md 全员每轮都注入，取紧一点。
TEAM_MD_MAX_CHARS = 8000
MEMBER_NOTES_MAX_CHARS = 4000


def _read_md(p) -> str:
    try:
        return (p.read_text(encoding="utf-8") or "").strip()
    except OSError:
        return ""


def _write_md(p, text: str) -> bool:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(text or "", encoding="utf-8")
        os.replace(tmp, p)
        return True
    except OSError:
        return False


def team_md(team_id: str) -> str:
    """TEAM.md 全文（无文件返回空串——空 TEAM.md 不注入）。"""
    return _read_md(paths.team_md_path(team_id))


def set_team_md(team_id: str, text: str) -> bool:
    return _write_md(paths.team_md_path(team_id), (text or "").strip())


def member_notes(team_id: str, instance_id: str) -> str:
    return _read_md(paths.team_member_notes_dir(team_id, instance_id) / "notes.md")


def set_member_notes(team_id: str, instance_id: str, text: str) -> bool:
    return _write_md(
        paths.team_member_notes_dir(team_id, instance_id) / "notes.md", (text or "").strip()
    )


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit].rstrip() + t("\n…(truncated; full text in file)",
                                     "\n…（已截断，全文在文件中）"), True


def turn_context(session_id: str) -> str:
    """该 session 的团队记忆注入段（TEAM.md [+ 实例 notes]）；非团队会话
    返回空串。绝不抛异常——注入失败不带走 turn。"""
    try:
        from . import store

        found = store.team_of_session(session_id)
        if found is None:
            return ""
        team, member = found
        team_id = str(team.get("id") or "")
        parts: list[str] = []

        md = team_md(team_id)
        if md:
            clipped, _ = _clip(md, TEAM_MD_MAX_CHARS)
            parts.append(t(
                "Team shared memory (TEAM.md — conventions set by the user;"
                " treat as instructions for this team's work):",
                "团队共享记忆（TEAM.md——用户定下的约定，视为本团队工作准则）：",
            ) + "\n" + clipped)

        if member is not None:
            notes = member_notes(team_id, str(member.get("instance_id") or ""))
            if notes:
                clipped, _ = _clip(notes, MEMBER_NOTES_MAX_CHARS)
                parts.append(t(
                    "Your instance memory (notes for you specifically in this"
                    " team):",
                    "你的实例记忆（仅针对你在本团队的备注）：",
                ) + "\n" + clipped)

        return "\n\n".join(parts)
    except Exception:  # noqa: BLE001 — 展示性注入绝不带崩 turn
        from ..server_shared import _log

        _log.exception("team_memory_context_failed session=%s", session_id)
        return ""
