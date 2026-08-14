"""Agent registry.

Each agent is a JSON file under ~/.ginno/agents/<id>.json with its own
persona, model binding, tool allowlist, and (via memory.py) memory scope.

    {
      "id": "dev",
      "name": "Dev Agent",
      "icon": "terminal",        # lucide icon name
      "color": "blue",           # theme color key
      "system_prompt": "...",
      "provider": "custom",      # provider id from providers.py
      "model": "",               # empty → use provider default
      "tools_allow": ["*"],      # ["*"] = all; else fnmatch patterns over tool names
      "memory_scope": "agent"    # "agent" → ~/.ginno/agents/<id>/memory
    }

`status` (active/idle/running) is computed at runtime, not persisted.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .. import paths
from .memory import ensure_agent_memory


@dataclass
class AgentConfig:
    id: str
    name: str
    icon: str = "terminal"
    color: str = "blue"
    system_prompt: str = ""
    provider: str = "custom"
    model: str = ""
    tools_allow: list[str] = field(default_factory=lambda: ["*"])
    memory_scope: str = "agent"
    status: str = "idle"  # runtime presence; default idle

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# Research Agent persona. The "research discipline" section adapts the rules
# OpenAI Codex embeds in its web.run tool description (when to verify, citation
# placement, quote limits, fact-vs-inference) to ginno's vault-first context.
_RESEARCH_PROMPT = """\
You are Research Agent. You gather and synthesize information from the user's \
notes, documents, and connected tools (MCP), and produce clear, well-sourced \
answers. Your tools are read-only by design: never modify files or external \
state, and never attempt workarounds to do so — if the conclusion requires \
changes, recommend them and leave execution to the Dev or Writer agents.

Research discipline:

1. Verify before you claim.
- For any fact that may have changed over time (versions, prices, laws, \
schedules, people/roles, recommendations), or anything you are more than ~10% \
unsure about, verify with a search or tool call instead of answering from \
memory.
- For high-stakes topics (medical, legal, financial, security), verify by \
default and prefer primary sources (official docs, papers, specs).
- When sources conflict, report the conflict with dates, and prefer the newer, \
more authoritative source.

2. Work the local knowledge base first.
- Start from the user's vault: locate relevant notes/docs with glob_files and \
grep_files, then read_file the promising ones. Reach for external (MCP) tools \
only when the vault cannot answer.
- Reuse what you already found; do not re-read the same files.

3. Cite as you go.
- Attach a source to every non-obvious claim, placed right next to the claim — \
never collected in a pile at the end.
- Local notes/docs: cite the file path (plus heading/section when useful). \
Web sources: use Markdown links [title](url); never bare URLs; never place \
citations inside code fences.
- Do not expose internal tool ids, search refs, or raw tool output in the \
final answer.

4. Quote sparingly.
- Quote at most ~25 words verbatim from any single source; otherwise \
paraphrase. Quoted material should be a small fraction of the answer.

5. Separate fact from inference.
- Clearly mark what is directly supported by a source versus what you inferred \
or assumed. State assumptions explicitly.

6. Structure multi-part research.
- For research with several questions, track the parts with todo_list, answer \
each, then end with a short synthesis: answer first, evidence second, open \
questions and gaps last."""

# The original one-liner seed, kept verbatim so ensure_research_discipline()
# can detect an UNCUSTOMIZED prompt on old installs and upgrade it.
_LEGACY_RESEARCH_PROMPT = (
    "You are Research Agent. You gather and synthesize information, read "
    "docs and notes, and produce clear summaries with sources. Do not "
    "modify files unless asked."
)


# Default seed — matches the design mockup (Dev / Research / Writer).
_SEED: list[AgentConfig] = [
    AgentConfig(
        id="dev",
        name="Dev Agent",
        icon="terminal",
        color="blue",
        system_prompt=(
            "You are Dev Agent, a software-engineering assistant. You help with "
            "code, PRs, debugging, and repo operations. Prefer concrete actions "
            "using your tools. Be concise."
        ),
        provider="custom",
        tools_allow=["*"],
    ),
    AgentConfig(
        id="research",
        name="Research Agent",
        icon="search",
        color="orange",
        system_prompt=_RESEARCH_PROMPT,
        provider="custom",
        tools_allow=["read_file", "glob_files", "grep_files", "mcp_*", "todo_list"],
    ),
    AgentConfig(
        id="writer",
        name="Writer Agent",
        icon="pen-line",
        color="green",
        system_prompt=(
            "You are Writer Agent. You draft and edit documents, notes, and "
            "communications with a clear, polished voice."
        ),
        provider="custom",
        tools_allow=["read_file", "write_file", "edit_file", "glob_files", "mcp_*", "todo_*"],
    ),
    AgentConfig(
        id="workflow-dev",
        name="Workflow Dev Agent",
        icon="workflow",
        color="violet",
        system_prompt=(
            "You are Workflow Dev Agent. You edit ONE workflow's versioned DSL by "
            "conversation. The user's first message gives the workflow_id and the "
            "current DSL. To change it, call workflow_propose_edit(workflow_id, "
            "new_dsl_json, rationale) with the FULL proposed DSL object. Your edit "
            "then PAUSES: the user sees a unified diff and must Apply or Reject — "
            "there is no DAG editor, the diff confirmation is the gate. Only on Apply "
            "is a new immutable version created. DSL node types: step/branch/loop/"
            "human/browser. A loop routes structurally (its body must NOT carry an "
            "explicit out-edge; reference the loop item via {{<as>}}). browser nodes "
            "use action eval|snapshot|handoff|complete (complete is its own node). "
            "Validate your proposal: entry must be a node id, every edge endpoint "
            "must exist, branch needs cases or default, loop needs over+body+max_iters. "
            "Explain each change in rationale. Keep edits minimal and targeted."
        ),
        provider="custom",
        tools_allow=["workflow_propose_edit", "workflow_list"],
    ),
]


def _agent_path(agent_id: str) -> Path:
    return paths.agents_dir() / f"{agent_id}.json"


def _read(agent_id: str) -> AgentConfig | None:
    p = _agent_path(agent_id)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text() or "{}")
    except json.JSONDecodeError:
        return None
    return AgentConfig(**{k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})


def _write(cfg: AgentConfig) -> None:
    paths.agents_dir().mkdir(parents=True, exist_ok=True)
    p = _agent_path(cfg.id)
    p.write_text(json.dumps(cfg.to_dict(), indent=2, ensure_ascii=False))


# Distinct seed memory so each persona's prompt (and thus behaviour) differs
# out of the box — demonstrates independent per-agent memory.
_MEMORY_SEED: dict[str, str] = {
    "dev": "Focus: code, PRs, debugging, repo ops. Prefer concrete tool actions.",
    "research": "Focus: gather & synthesize information, read docs/notes, cite sources. Avoid mutating files.",
    "writer": "Focus: draft & polish documents and messages with a clear voice.",
}


def ensure_seeded() -> None:
    """Create default agent files if the registry is empty."""
    paths.agents_dir().mkdir(parents=True, exist_ok=True)
    if any(paths.agents_dir().glob("*.json")):
        return
    for cfg in _SEED:
        _write(cfg)
        ensure_agent_memory(cfg.id, cfg.name, _MEMORY_SEED.get(cfg.id, ""))


# TODO tool patterns each persona should have (read-only for research).
_TODO_PATTERNS: dict[str, list[str]] = {
    "dev": ["todo_*"],
    "research": ["todo_list"],
    "writer": ["todo_*"],
}


def ensure_todo_tools() -> None:
    """Merge the TODO tool patterns into existing agents (idempotent migration)."""
    for cfg in list_agents():
        needed = _TODO_PATTERNS.get(cfg.id)
        if not needed:
            continue
        allow = list(cfg.tools_allow or ["*"])
        if "*" in allow:
            continue  # already all-inclusive
        added = [p for p in needed if p not in allow]
        if added:
            update_agent(cfg.id, {"tools_allow": allow + added})


# Goal tool patterns (goal-design.md §4.2). dev already has "*" so it is not
# listed; research is the primary goal scenario, writer may pursue doc goals.
_GOAL_PATTERNS: dict[str, list[str]] = {
    "research": ["goal_*"],
    "writer": ["goal_*"],
}


def ensure_goal_tools() -> None:
    """Merge the goal tool patterns into existing agents (idempotent migration)."""
    for cfg in list_agents():
        needed = _GOAL_PATTERNS.get(cfg.id)
        if not needed:
            continue
        allow = list(cfg.tools_allow or ["*"])
        if "*" in allow:
            continue  # already all-inclusive
        added = [p for p in needed if p not in allow]
        if added:
            update_agent(cfg.id, {"tools_allow": allow + added})


_WEB_PATTERNS: dict[str, list[str]] = {
    "research": ["web_search", "web_fetch"],
    "writer": ["web_search", "web_fetch"],
}


def ensure_web_tools() -> None:
    """Merge web search/fetch into research/writer agents (idempotent migration).

    dev carries ``*`` already; workflow-dev deliberately gets no web tools
    (citations-design.md §8)."""
    for cfg in list_agents():
        needed = _WEB_PATTERNS.get(cfg.id)
        if not needed:
            continue
        allow = list(cfg.tools_allow or ["*"])
        if "*" in allow:
            continue  # already all-inclusive
        added = [p for p in needed if p not in allow]
        if added:
            update_agent(cfg.id, {"tools_allow": allow + added})


_BROWSER_PATTERNS: dict[str, list[str]] = {
    "research": ["browser_*"],
    "writer": ["browser_*"],
}


def ensure_browser_tools() -> None:
    """Merge browser_* into research/writer (idempotent). workflow-dev stays off
    — it only authors DSL (docs/browser-embed-design.md §9.5)."""
    for cfg in list_agents():
        needed = _BROWSER_PATTERNS.get(cfg.id)
        if not needed:
            continue
        allow = list(cfg.tools_allow or ["*"])
        if "*" in allow:
            continue
        added = [p for p in needed if p not in allow]
        if added:
            update_agent(cfg.id, {"tools_allow": allow + added})


def ensure_research_discipline() -> None:
    """Upgrade the seeded Research Agent prompt on old installs (idempotent).

    Replaces the prompt ONLY when it is still the verbatim legacy seed — a
    prompt the user customized (Settings → Agent) is never overwritten.
    """
    cfg = _read("research")
    if cfg is not None and cfg.system_prompt == _LEGACY_RESEARCH_PROMPT:
        update_agent("research", {"system_prompt": _RESEARCH_PROMPT})


def list_agents() -> list[AgentConfig]:
    ensure_seeded()
    out: list[AgentConfig] = []
    for p in sorted(paths.agents_dir().glob("*.json")):
        try:
            data = json.loads(p.read_text() or "{}")
        except json.JSONDecodeError:
            continue
        out.append(
            AgentConfig(**{k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})
        )
    return out


def get_agent(agent_id: str) -> AgentConfig | None:
    ensure_seeded()
    return _read(agent_id)


def create_agent(data: dict[str, Any]) -> AgentConfig:
    cfg = AgentConfig(**{k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})
    if not cfg.id:
        raise ValueError("agent id required")
    if _agent_path(cfg.id).exists():
        raise ValueError(f"agent {cfg.id} already exists")
    _write(cfg)
    return cfg


def update_agent(agent_id: str, data: dict[str, Any]) -> AgentConfig:
    existing = _read(agent_id)
    if not existing:
        raise ValueError(f"agent {agent_id} not found")
    merged = existing.to_dict()
    merged.update({k: v for k, v in data.items() if k in AgentConfig.__dataclass_fields__})
    merged["id"] = agent_id  # id immutable
    cfg = AgentConfig(**merged)
    _write(cfg)
    return cfg


def delete_agent(agent_id: str) -> bool:
    p = _agent_path(agent_id)
    if p.exists():
        p.unlink()
        return True
    return False


def fork_agent(src_id: str, new_id: str, name: str | None = None) -> AgentConfig:
    """Copy an agent's persona/tools/model into a fresh id with EMPTY memory.

    Used by workflow runs (design §5.2): the fork reuses the source provider/model
    and tools_allow but starts with clean history + memory. Idempotent — if the
    fork id already exists (e.g. a rerun) the existing fork is returned.
    """
    src = _read(src_id)
    if not src:
        raise ValueError(f"source agent {src_id} not found")
    data = src.to_dict()
    data["id"] = new_id
    data["name"] = name or f"{src.name} (fork)"
    try:
        return create_agent(data)
    except ValueError:
        # already forked (id exists) — return the existing fork unchanged
        existing = _read(new_id)
        if existing:
            return existing
        raise
