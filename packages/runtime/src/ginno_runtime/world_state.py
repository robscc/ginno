"""WorldState — sectioned, snapshot-diffed model-visible state (plan C1/C2).

Ported from the Codex harness discipline, adapted to Ginno's LangGraph shape:

* The model-visible world (date, permission mode, active agent, skills,
  memory, MCP toolset) is split into named **sections**, each producing a
  small structured **snapshot**.
* Every turn the server rebuilds the current snapshot and diffs it against
  the session baseline (``sessions/<id>.world.json``). Changed sections
  produce ONE merged ``[world state update]`` message placed before the
  turn's user message, plus a ``context.updated`` WS event for the UI chip.
* The stable system prompt is rendered from the same sections every model
  call (see ``graph.build_stable_system``), so between changes it stays
  byte-identical — the precondition for prefix caching (plan B2).

Ginno-specific simplification vs Codex: the baseline never has to be
replayed from a rollout, because the system prompt is rebuilt from live state
on every call; the persisted snapshot only serves change detection across
runtime restarts.

Sections implemented: environment (A1), permissions (A3), agent (A4),
skills (A6), memory (change awareness; budget deferred), mcp (A7).
"""

from __future__ import annotations

import hashlib
import json
import platform
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from . import paths
from .agents.memory import read_agent_memory
from .permission.policy import PermissionPolicy, is_bypass_permissions
from .skills.loader import SkillLoader

# Message markers — the UI history endpoint maps these to "context" blocks and
# the model sees them as contextual user messages (Codex-style bundles).
UPDATE_MSG_PREFIX = "[world state update]"
REINJECT_MSG_PREFIX = "[world state re-injection]"
TURN_CONTEXT_PREFIX = "[turn context]"
SUMMARY_MSG_PREFIX = "[conversation summary]"
from .goals.templates import GOAL_CONTEXT_PREFIX  # noqa: E402

ALL_CONTEXT_PREFIXES = (
    UPDATE_MSG_PREFIX,
    REINJECT_MSG_PREFIX,
    TURN_CONTEXT_PREFIX,
    SUMMARY_MSG_PREFIX,
    GOAL_CONTEXT_PREFIX,
)

WEEKDAYS_CN = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# A6: skills index budget (chars) unless overridden in settings.context.
DEFAULT_SKILLS_INDEX_MAX_CHARS = 1500

_CONTEXT_DEFAULTS = {
    "world_state": True,
    "cache_control": True,
    "tool_output_max_chars": 20000,
    "microcompact_enabled": True,
    "microcompact_min_chars": 500,
    "compaction_enabled": True,
    "compact_threshold_tokens": 500000,
    "compact_keep_turns": 3,
    "checkpoint_mode": "delta",
    "skills_index_max_chars": DEFAULT_SKILLS_INDEX_MAX_CHARS,
}


def context_settings() -> dict:
    """Merged settings.context block (defaults ← user settings)."""
    try:
        p = paths.settings_path()
        raw = json.loads(p.read_text() or "{}") if p.exists() else {}
    except Exception:
        raw = {}
    out = dict(_CONTEXT_DEFAULTS)
    user = raw.get("context") or {}
    if isinstance(user, dict):
        out.update({k: v for k, v in user.items() if k in _CONTEXT_DEFAULTS})
    return out


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", errors="replace")).hexdigest()[:12]


_OS_DESC: str | None = None


def _platform_desc() -> str:
    global _OS_DESC
    if _OS_DESC is None:
        _OS_DESC = f"{platform.system()} {platform.release()} ({platform.machine()})"
    return _OS_DESC


@dataclass
class SessionCtx:
    """Everything sections may read to build their snapshots.

    ``agent`` optionally carries the already-resolved agent object (the graph
    path); when absent, sections resolve it from the registry by ``agent_id``
    (the server sync path, where the registry is the source of truth).
    """

    session_id: str
    project_slug: str
    agent_id: str | None = None
    mcp_tool_names: list[str] = field(default_factory=list)
    all_tool_names: list[str] = field(default_factory=list)
    agent: Any = None
    # Session workspace (the session files dir). Constant within a session, so
    # it belongs in the STABLE system layer (prefix-cache safe). Was frozen
    # with F1/F2 until the 2026-08 skill-install incident proved the model
    # cannot improvise file operations without knowing where it is.
    workspace: str = ""


# --------------------------------------------------------------------------- #
# Sections
# --------------------------------------------------------------------------- #
class EnvironmentSection:
    """A1 — date/weekday/timezone/OS/ginno_home/workspace/project. Deliberately
    no clock time: hour/minute churn would break prefix stability; the model
    can `date`. Workspace was a frozen item (plan §9) until the 2026-08
    skill-install incident: without it the model probed `pwd`/`$HOME` and
    eventually globbed from the sidecar cwd ``/`` until the turn crashed. It
    is constant within a session, so prefix stability is preserved."""

    id = "environment"

    def snapshot(self, ctx: SessionCtx) -> dict | None:
        now = datetime.now().astimezone()
        return {
            "date": now.strftime("%Y-%m-%d"),
            "weekday": WEEKDAYS_CN[now.weekday()],
            "tz": f"{now.tzname()} (UTC{now.strftime('%z')})",
            "os": _platform_desc(),
            "ginno_home": str(paths.home()),
            "workspace": ctx.workspace or "",
            "project_slug": ctx.project_slug or "default",
        }

    def render(self, snap: dict) -> str:
        lines = [
            "<environment>",
            f"<date>{snap['date']} ({snap['weekday']})</date>",
            f"<timezone>{snap['tz']}</timezone>",
            f"<os>{snap['os']}</os>",
            f"<ginno_home>{snap['ginno_home']} — 记忆、skills、settings 所在目录</ginno_home>",
        ]
        if snap.get("workspace"):
            lines.append(
                f"<workspace>{snap['workspace']} — 本会话工作目录：bash 的 cwd、"
                "文件工具相对路径的默认位置；产物文件也写在这里</workspace>"
            )
        lines.append(f"<project>{snap['project_slug']}</project>")
        lines.append("</environment>")
        return "\n".join(lines)

    def update_text(self, old: dict, new: dict) -> str | None:
        if old.get("date") != new.get("date"):
            return f"日期已更新为 {new['date']}（{new['weekday']}）。"
        return None  # remaining fields are static within a session


class PermissionsSection:
    """A3 — the model should know whether tools run privileged or gated."""

    id = "permissions"

    def snapshot(self, ctx: SessionCtx) -> dict | None:
        policy = PermissionPolicy.from_settings()
        return {
            "bypass": bool(is_bypass_permissions()),
            "allow": len(policy.allow),
            "deny": len(policy.deny),
            "ask": len(policy.ask),
        }

    def render(self, snap: dict) -> str:
        if snap["bypass"]:
            body = "特权模式：所有工具调用直接执行，无需用户确认。"
        else:
            body = (
                "审批模式：工具调用按 deny→ask→allow 规则匹配"
                f"（当前 deny {snap['deny']} 条、ask {snap['ask']} 条、allow {snap['allow']} 条），"
                "被拒时你会收到 [blocked:...] 消息，请改用其他方式或直接向用户说明。"
            )
        return f"<permissions>\n{body}\n</permissions>"

    def update_text(self, old: dict, new: dict) -> str | None:
        if old.get("bypass") != new.get("bypass"):
            if new["bypass"]:
                return "已切换为特权模式：工具调用不再需要用户确认。"
            return "已切换为审批模式：部分工具调用需要用户确认，被拒时你会收到 [blocked:...] 消息。"
        if old != new:
            return "权限策略规则已更新。"
        return None


def _agent_by_id(agent_id: str | None):
    from . import agents as agents_reg

    if agent_id:
        a = agents_reg.get_agent(agent_id)
        if a:
            return a
    lst = agents_reg.list_agents()
    return lst[0] if lst else None


def _agent_allowed_count(agent, all_tool_names: list[str]) -> int:
    """Mirror of graph.tool_allowed for snapshot purposes (keep in sync)."""
    import fnmatch

    from .tools.artifact_tools import ARTIFACT_TOOL_NAMES
    from .tools.render_tools import RENDER_TOOL_NAMES
    from .tools.workflow_tools import WORKFLOW_TOOL_NAMES

    if not agent:
        return len(all_tool_names)
    allow = agent.tools_allow or ["*"]
    n = 0
    for name in all_tool_names:
        if name in RENDER_TOOL_NAMES or name in WORKFLOW_TOOL_NAMES or name in ARTIFACT_TOOL_NAMES:
            n += 1
            continue
        if "*" in allow or any(fnmatch.fnmatch(name, p) for p in allow):
            n += 1
    return n


class AgentSection:
    """A4 — active persona + prompt fingerprint. Covers both mid-session agent
    switches and edits to the agent's prompt in Settings → Agent 管理 (which
    replaced the GINNO.md plan item)."""

    id = "agent"

    def snapshot(self, ctx: SessionCtx) -> dict | None:
        agent = ctx.agent or _agent_by_id(ctx.agent_id)
        if not agent:
            return None
        return {
            "agent_id": agent.id,
            "name": agent.name,
            "prompt_hash": _sha1(agent.system_prompt or ""),
            "tool_count": _agent_allowed_count(agent, ctx.all_tool_names),
        }

    def render(self, snap: dict) -> str:
        return (
            f"You are operating in this turn as **{snap['name']}** "
            f"({snap['tool_count']} tools available in this role)."
        )

    def update_text(self, old: dict, new: dict) -> str | None:
        lines: list[str] = []
        if old.get("agent_id") != new.get("agent_id"):
            line = f"已切换为 **{new['name']}**（{new['tool_count']} 个可用工具）"
            if old.get("name"):
                line += f"，此前是 {old['name']}"
            lines.append(line + "。")
        elif old.get("prompt_hash") != new.get("prompt_hash"):
            lines.append(f"**{new['name']}** 的角色设定（prompt）已更新，从本轮起生效。")
        elif old.get("tool_count") != new.get("tool_count"):
            lines.append(
                f"你在当前角色下的可用工具数量变化：{old.get('tool_count')} → {new['tool_count']}。"
            )
        return " ".join(lines) if lines else None


class SkillsSection:
    """A6 — one-line skill index with a char budget (descriptions dropped
    first, then whole entries, with a tail note so the model knows), PLUS the
    skills directories and how to install/uninstall (2026-08 incident: the
    model was asked to install a skill but knew neither where skills live nor
    that install_skills exists). The snapshot therefore exists even with ZERO
    skills installed — the install context is most needed exactly then."""

    id = "skills"

    def snapshot(self, ctx: SessionCtx) -> dict | None:
        from .graph import tool_allowed  # local import: avoid cycle

        skills = SkillLoader(project_slug=ctx.project_slug).load()
        names = sorted(s.name for s in skills)
        agent = ctx.agent or _agent_by_id(ctx.agent_id)
        return {
            "names": names,
            "index": self._budget_index(skills),
            "global_dir": str(paths.global_skills_dir()),
            "project_dir": str(paths.project_skills_dir(ctx.project_slug or "default")),
            "can_manage": bool(tool_allowed(agent, "install_skills")),
        }

    def _budget_index(self, skills) -> str:
        if not skills:
            return "(尚未安装任何 skill。)"
        budget = int(
            context_settings().get("skills_index_max_chars", DEFAULT_SKILLS_INDEX_MAX_CHARS)
        )
        header = (
            "Available skills (the user can invoke one by starting a message with"
            " /<name>; listed here for your awareness):"
        )
        lines = [s.system_prompt_snippet() for s in skills]
        kept: list[str] = []
        used = len(header)
        dropped = 0
        for line in lines:
            if used + len(line) + 1 > budget and kept:
                dropped += 1
                continue
            kept.append(line)
            used += len(line) + 1
        out = "\n".join([header] + kept)
        if dropped:
            out += f"\n(另有 {dropped} 个 skill 因预算未列出，可用 / 前缀直接调用。)"
        return out

    def render(self, snap: dict) -> str:
        lines = [
            snap.get("index", ""),
            "<skills_management>",
            f"全局 skills 目录: {snap['global_dir']}/<name>/SKILL.md",
            f"项目 skills 目录: {snap['project_dir']}/<name>/SKILL.md（同名时覆盖全局）",
            "SKILL.md 需包含 YAML frontmatter（至少 name、description）。",
        ]
        if snap.get("can_manage"):
            lines.append(
                "安装 skill：先把源码取到本地（如用 bash git clone 仓库到工作目录），"
                "再调用 install_skills(path) —— path 是含一个或多个 <skill>/SKILL.md "
                "子目录的目录，或单个 skill 目录；list_skills() 查看已安装，"
                "uninstall_skill(name) 卸载。不要手写或猜测 skills 目录之外的安装位置。"
            )
        lines.append("</skills_management>")
        return "\n".join(p for p in lines if p)

    def update_text(self, old: dict, new: dict) -> str | None:
        old_names = set(old.get("names") or [])
        new_names = set(new.get("names") or [])
        if old_names == new_names:
            return None
        added = sorted(new_names - old_names)
        removed = sorted(old_names - new_names)
        parts = [f"数量 {len(old_names)} → {len(new_names)}"]
        if added:
            parts.append("新增 " + ", ".join(added))
        if removed:
            parts.append("移除 " + ", ".join(removed))
        return "Skills 已更新：" + "；".join(parts) + "。"


class MemorySection:
    """Memory change awareness (A5 budget is deferred by product decision —
    content is still injected in full, as before)."""

    id = "memory"

    def snapshot(self, ctx: SessionCtx) -> dict | None:
        from .graph import _read_global_memory

        global_mem = _read_global_memory()
        agent_mem = read_agent_memory(ctx.agent_id) if ctx.agent_id else ""
        if not global_mem and not agent_mem:
            return None
        return {
            "global_hash": _sha1(global_mem),
            "agent_hash": _sha1(agent_mem),
            "global": global_mem,
            "agent": agent_mem,
        }

    def render(self, snap: dict) -> str:
        from .knowledge.injection import wrap_context_section

        parts: list[str] = []
        if snap.get("agent"):
            parts.append("Your persistent memory (private to this agent):\n" + snap["agent"])
        if snap.get("global"):
            parts.append(wrap_context_section("injected_memory", snap["global"]))
        return "\n".join(p for p in parts if p)

    def update_text(self, old: dict, new: dict) -> str | None:
        lines = []
        if old.get("global_hash") != new.get("global_hash"):
            lines.append("全局长期记忆（MEMORY.md）已更新。")
        if old.get("agent_hash") != new.get("agent_hash"):
            lines.append("你所扮演角色的私有记忆已更新。")
        return " ".join(lines) if lines else None


class McpSection:
    """A7 — MCP toolset awareness. Nothing to render (MCP tools reach the
    model via bind_tools + the tools line); only change notifications."""

    id = "mcp"

    def snapshot(self, ctx: SessionCtx) -> dict | None:
        if not ctx.mcp_tool_names:
            return None
        names = sorted(ctx.mcp_tool_names)
        return {"count": len(names), "hash": _sha1("\n".join(names))}

    def render(self, snap: dict) -> str:
        return ""

    def update_text(self, old: dict, new: dict) -> str | None:
        if old == new:
            return None
        old_count = (old or {}).get("count", 0)
        return f"MCP 工具已更新：{old_count} → {new.get('count', 0)} 个。"


class GoalSection:
    """Goal awareness (goal-design.md §4.3.2): the active long-running goal is
    part of the STABLE system layer so EVERY turn (user-interactive or
    autonomous continuation) knows it — not only continuation turns, which
    carry the full continuation prompt. Changes (objective edit, pause/resume,
    completion) announce via the normal context.updated diff path."""

    id = "goal"

    def snapshot(self, ctx: SessionCtx) -> dict | None:
        from .goals import store as goal_store

        goal = goal_store.get_goal(ctx.project_slug, ctx.session_id)
        if not goal:
            return None
        return {
            "goal_id": goal.get("goal_id"),
            "status": goal.get("status"),
            "objective": goal.get("objective"),
            "turns_used": goal.get("turns_used", 0),
        }

    def render(self, snap: dict) -> str:
        status = snap.get("status")
        lines = [
            "<goal>",
            f"<status>{status}</status>",
            f"<objective>{snap.get('objective')}</objective>",
        ]
        if status == "active":
            lines.append(
                "<guidance>本会话有一个自主推进中的长程目标。普通用户消息是该目标"
                "过程中的临时输入：优先结合目标来理解和处理；目标真正达成且无遗留"
                "工作时调用 goal_update(status=\"complete\")，同一阻塞连续 3 个 goal "
                "轮无法推进时调用 goal_update(status=\"blocked\")。暂停/恢复/清除由"
                "用户控制，你不要自行暂停。</guidance>"
            )
        elif status == "paused":
            lines.append(
                "<guidance>目标当前被用户暂停，不会自主续跑；像普通会话一样处理用户"
                "消息，除非用户恢复目标，不要主动推进目标或调用 goal_update。</guidance>"
            )
        else:
            lines.append(
                "<guidance>目标处于终止态（blocked/usage_limited/complete），等待用户"
                "处置；不要主动推进，除非用户恢复或设定新目标。</guidance>"
            )
        lines.append("</goal>")
        return "\n".join(lines)

    def update_text(self, old: dict, new: dict) -> str | None:
        if not old and new:
            return f"长程目标已设定：{new.get('objective')}"
        if old and not new:
            return "长程目标已清除。"
        lines = []
        if old.get("objective") != new.get("objective"):
            lines.append(f"长程目标已更新为：{new.get('objective')}")
        if old.get("status") != new.get("status"):
            label = {
                "active": "目标已恢复自主推进。",
                "paused": "目标已暂停。",
                "blocked": "目标标记为受阻，等待你的指示。",
                "usage_limited": "目标因用量受限停止。",
                "complete": "目标已达成。",
            }.get(new.get("status"), "")
            lines.append(label)
        return " ".join(l for l in lines if l) or None


SECTIONS: list[Any] = [
    AgentSection(),
    GoalSection(),
    EnvironmentSection(),
    PermissionsSection(),
    SkillsSection(),
    MemorySection(),
    McpSection(),
]


# --------------------------------------------------------------------------- #
# WorldState
# --------------------------------------------------------------------------- #
class WorldState:
    """Current sections built from one SessionCtx."""

    def __init__(self, ctx: SessionCtx) -> None:
        self.ctx = ctx
        self.snaps: dict[str, dict] = {}
        for s in SECTIONS:
            try:
                snap = s.snapshot(ctx)
            except Exception:
                snap = None
            if snap is not None:
                self.snaps[s.id] = snap

    def snapshot(self) -> dict[str, dict]:
        return {k: v for k, v in self.snaps.items()}

    def render_system(self) -> str:
        """All section renders in fixed order (only what exists)."""
        parts: list[str] = []
        for s in SECTIONS:
            snap = self.snaps.get(s.id)
            if snap is None:
                continue
            text = s.render(snap)
            if text:
                parts.append(text)
        return "\n".join(parts)


def diff_snapshots(old: dict[str, dict], new: dict[str, dict]) -> list[tuple[str, dict, dict]]:
    """[(section_id, old_snap, new_snap)] for added/removed/changed sections."""
    changes: list[tuple[str, dict, dict]] = []
    for sid, new_snap in new.items():
        old_snap = old.get(sid)
        if old_snap != new_snap:
            changes.append((sid, old_snap or {}, new_snap))
    for sid in old:
        if sid not in new:
            changes.append((sid, old[sid], {}))
    return changes


def render_update(changes: list[tuple[str, dict, dict]]) -> tuple[str, list[dict]]:
    """Merge section diffs into ONE update message + chip change entries."""
    by_id = {s.id: s for s in SECTIONS}
    lines: list[str] = []
    chip: list[dict] = []
    for sid, old_snap, new_snap in changes:
        section = by_id.get(sid)
        if not section:
            continue
        text = section.update_text(old_snap, new_snap)
        if text:
            lines.append(f"- {text}")
            chip.append({"section": sid, "summary": text})
    if not lines:
        return "", []
    # The prefix is a machine marker: _messages_to_ui uses it to map this
    # message to a centered context row and strips it before display.
    return UPDATE_MSG_PREFIX + "\n" + "\n".join(lines), chip


def render_reinjection(ctx: SessionCtx) -> str:
    """E4 — full world facts re-asserted after history compaction."""
    ws = WorldState(ctx)
    body = ws.render_system()
    return f"{REINJECT_MSG_PREFIX}\n（历史刚被压缩，以下是当前世界状态的完整重申）\n{body}"


# --------------------------------------------------------------------------- #
# Baseline persistence + per-turn sync (C1/C2)
# --------------------------------------------------------------------------- #
def world_state_path(project_slug: str, session_id: str) -> Any:
    return paths.project_sessions_dir(project_slug) / f"{session_id}.world.json"


def load_baseline(project_slug: str, session_id: str) -> dict | None:
    p = world_state_path(project_slug, session_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text() or "{}")
        snap = data.get("snapshot")
        return snap if isinstance(snap, dict) else None
    except Exception:
        return None


def save_baseline(project_slug: str, session_id: str, snapshot: dict) -> None:
    p = world_state_path(project_slug, session_id)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"snapshot": snapshot}, ensure_ascii=False))
    except Exception:
        pass  # baseline loss only costs a spurious one-time update message


def sync_world_state(ctx: SessionCtx) -> tuple[str | None, list[dict]]:
    """Diff current world against the session baseline.

    Returns ``(update_message_text | None, chip_changes)``. First sync (no
    baseline) only records the baseline — the initial system prompt already
    carries the world, so nothing needs announcing.
    """
    ws = WorldState(ctx)
    snap = ws.snapshot()
    baseline = load_baseline(ctx.project_slug, ctx.session_id)
    if baseline is None:
        save_baseline(ctx.project_slug, ctx.session_id, snap)
        return None, []
    changes = diff_snapshots(baseline, snap)
    save_baseline(ctx.project_slug, ctx.session_id, snap)
    if not changes:
        return None, []
    text, chip = render_update(changes)
    return (text or None), chip
