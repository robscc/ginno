"""Unit tests for the command resolver (slash commands + @mentions).

Covers parse edge cases (``/tmp/foo`` passthrough is the headline regression),
skill trigger gating, mention token scanning, and TurnPlan assembly.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, RemoveMessage

from ginno_runtime import agents as agents_reg
from ginno_runtime import artifacts as art_store
from ginno_runtime import paths
from ginno_runtime import workflows as wf_store
from ginno_runtime.commands.registry import BUILTINS
from ginno_runtime.commands.resolver import (
    parse_mention_tokens,
    parse_slash,
    resolve_mentions,
    resolve_turn,
    substitute_skill,
)
from ginno_runtime import server_shared
from ginno_runtime.api import stream as api_stream
from ginno_runtime import compaction as compaction_mod
from ginno_runtime import microcompact as microcompact_mod

pytestmark = pytest.mark.unit


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def seed_skill(home: Path, name: str, trigger: str = "both", body: str = "Do the thing.") -> None:
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill {name}\ntrigger: {trigger}\n---\n\n{body}\n",
        encoding="utf-8",
    )


SESSION = {"project_slug": "default", "workspace": "/tmp/ws"}


# --------------------------------------------------------------------------- #
# parse_slash — membership gating (D2)
# --------------------------------------------------------------------------- #
def test_parse_slash_builtin(isolated_home):
    assert parse_slash("/help me", "default") == ("help", "me")
    assert parse_slash("/help", "default") == ("help", "")
    assert parse_slash("  /help x", "default") == ("help", "x")


def test_parse_slash_filesystem_path_passthrough(isolated_home):
    # THE regression: absolute paths must never be treated as commands.
    assert parse_slash("/tmp/foo is the path", "default") is None
    assert parse_slash("/Users/me/notes.md 请总结", "default") is None


def test_parse_slash_unknown_and_midsentence(isolated_home):
    assert parse_slash("/nonexistent hi", "default") is None
    assert parse_slash("please run /help now", "default") is None
    assert parse_slash("", "default") is None
    assert parse_slash("/", "default") is None


def test_parse_slash_matches_user_invocable_skill(isolated_home):
    seed_skill(isolated_home, "summarize-notes")
    assert parse_slash("/summarize-notes these notes", "default") == (
        "summarize-notes",
        "these notes",
    )
    # model-invocable-only skills are NOT slash-addressable
    seed_skill(isolated_home, "model-only", trigger="model-invocable")
    assert parse_slash("/model-only x", "default") is None


def test_builtin_todo_skill_is_slash_addressable(isolated_home):
    # The builtin tier ships a `todo` skill with no seeding required.
    assert parse_slash("/todo list my tasks", "default") == ("todo", "list my tasks")
    text, name = substitute_skill("/todo 看看今天有什么", "default")
    assert name == "todo"
    assert '<skill name="todo">' in text
    assert "todo_list" in text


# --------------------------------------------------------------------------- #
# substitute_skill — body format + trigger gating (D3)
# --------------------------------------------------------------------------- #
def test_substitute_skill_body_format(isolated_home):
    seed_skill(isolated_home, "summarize-notes", body="Summarize carefully.")
    text, name = substitute_skill("/summarize-notes do it now", "default")
    assert name == "summarize-notes"
    assert '<skill name="summarize-notes">' in text
    assert "Summarize carefully." in text
    assert "User request: do it now" in text


def test_substitute_skill_arguments_placeholder(isolated_home):
    seed_skill(isolated_home, "aliyun-bill", body="用户输入: $ARGUMENTS\n跑脚本。")
    text, name = substitute_skill("/aliyun-bill overview 2026-07", "default")
    assert name == "aliyun-bill"
    assert "用户输入: overview 2026-07" in text
    assert "$ARGUMENTS" not in text


def test_substitute_skill_no_tail(isolated_home):
    seed_skill(isolated_home, "summarize-notes")
    text, name = substitute_skill("/summarize-notes", "default")
    assert name == "summarize-notes"
    assert "(Follow the skill instructions above.)" in text


def test_substitute_skill_model_invocable_passthrough(isolated_home):
    seed_skill(isolated_home, "model-only", trigger="model-invocable")
    text, name = substitute_skill("/model-only do it", "default")
    assert name is None
    assert text == "/model-only do it"


def test_substitute_skill_unknown_passthrough(isolated_home):
    text, name = substitute_skill("/whatever hello", "default")
    assert name is None and text == "/whatever hello"
    text, name = substitute_skill("/tmp/foo bar", "default")
    assert name is None and text == "/tmp/foo bar"


# --------------------------------------------------------------------------- #
# parse_mention_tokens (D12 regex)
# --------------------------------------------------------------------------- #
def test_mention_tokens_basic(isolated_home):
    toks = parse_mention_tokens(
        "hey @artifact:report.csv and @agent:Dev plus @workflow:triage and @memory"
    )
    assert toks == [
        {"kind": "artifact", "label": "report.csv"},
        {"kind": "agent", "label": "Dev"},
        {"kind": "workflow", "label": "triage"},
        {"kind": "memory", "label": None},
    ]


def test_mention_tokens_no_false_positives(isolated_home):
    # emails and path-embedded @ must not match
    assert parse_mention_tokens("mail bob@artifact.io today") == []
    assert parse_mention_tokens("see /x/@artifact:weird") == []
    assert parse_mention_tokens("word@memory") == []
    # unknown kinds never match
    assert parse_mention_tokens("@banana:foo @skill:x") == []


# --------------------------------------------------------------------------- #
# resolve_mentions — structured + fallback
# --------------------------------------------------------------------------- #
def test_resolve_mentions_structured_artifact(isolated_home):
    f = isolated_home / "data.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    art = art_store.add_artifact("default", "table", "data.csv", str(f), "s1")
    resolved, override = resolve_mentions(
        [{"kind": "artifact", "id": art["id"]}], "", "default"
    )
    assert override is None
    assert [r["kind"] for r in resolved] == ["artifact"]
    assert resolved[0]["id"] == art["id"]


def test_resolve_mentions_agent_override(isolated_home):
    agents = {a.name: a.id for a in agents_reg.list_agents()}
    assert "Research Agent" in agents  # seeded
    resolved, override = resolve_mentions(
        [{"kind": "agent", "id": agents["Research Agent"]}], "", "default"
    )
    assert override == agents["Research Agent"]
    # agent mentions are routing-only; they still appear in resolved
    assert any(r["kind"] == "agent" for r in resolved)


def test_resolve_mentions_ambiguous_agent_name_skipped(isolated_home):
    seed = agents_reg.list_agents()[0]
    # a second agent with the same name makes fallback resolution ambiguous
    agents_reg.create_agent(
        {
            "id": "dup-1",
            "name": seed.name,
            "icon": "terminal",
            "color": "#888888",
            "system_prompt": "",
            "provider": "custom",
            "model": "m",
            "tools_allow": ["*"],
            "memory_scope": "none",
            "status": "active",
        }
    )
    resolved, override = resolve_mentions(None, f"@agent:{seed.name} hi", "default")
    assert override is None
    assert resolved == []


def test_resolve_mentions_workflow_name_fallback(isolated_home):
    wf_store.create_def(
        {"name": "triage", "description": "d", "steps": [{"id": "a", "title": "A"}]}
    )
    resolved, _ = resolve_mentions(None, "run @workflow:triage please", "default")
    assert [r["kind"] for r in resolved] == ["workflow"]
    assert resolved[0]["label"] == "triage"


def test_resolve_mentions_text_fallback_skips_covered_kinds(isolated_home):
    wf_store.create_def({"name": "triage", "description": "", "steps": [{"id": "a", "title": "A"}]})
    wf_store.create_def({"name": "other", "description": "", "steps": [{"id": "a", "title": "A"}]})
    wf = wf_store.get_def(wf_store.list_defs()[0]["id"])
    resolved, _ = resolve_mentions(
        [{"kind": "workflow", "id": wf["id"]}],
        "@workflow:other and @memory",
        "default",
    )
    kinds = [r["kind"] for r in resolved]
    assert kinds == ["workflow", "memory"]  # token fallback added memory only


# --------------------------------------------------------------------------- #
# resolve_turn — full pipeline
# --------------------------------------------------------------------------- #
def test_resolve_turn_builtin_short_circuit(isolated_home):
    seed_skill(isolated_home, "summarize-notes")
    plan = resolve_turn({"message": "/help"}, SESSION)
    assert plan.builtin_reply is not None
    assert "/help" in plan.builtin_reply
    assert "/summarize-notes" in plan.builtin_reply  # lists user-invocable skills
    assert plan.skill_name is None


def test_resolve_turn_skill_substitution(isolated_home):
    seed_skill(isolated_home, "summarize-notes", body="Body here.")
    plan = resolve_turn({"message": "/summarize-notes my notes"}, SESSION)
    assert plan.builtin_reply is None
    assert plan.skill_name == "summarize-notes"
    assert "<skill" in plan.text and "User request: my notes" in plan.text
    assert plan.files_extra == [] and plan.mention_ctx == []


def test_resolve_turn_file_artifact_rides_files(isolated_home):
    f = isolated_home / "sales.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    art = art_store.add_artifact("default", "table", "sales.csv", str(f), "s1")
    plan = resolve_turn(
        {"message": "看看这个", "mentions": [{"kind": "artifact", "id": art["id"]}]},
        SESSION,
    )
    assert plan.files_extra == [{"artifact_id": art["id"]}]
    # no duplicate mention_ctx section for file-backed artifacts
    assert all(m["kind"] != "artifact" for m in plan.mention_ctx)


def test_resolve_turn_link_artifact_becomes_context(isolated_home):
    art = art_store.add_artifact("default", "link", "my-link", "", "s1")
    plan = resolve_turn(
        {"message": "看看", "mentions": [{"kind": "artifact", "id": art["id"]}]},
        SESSION,
    )
    assert plan.files_extra == []
    arts = [m for m in plan.mention_ctx if m["kind"] == "artifact"]
    assert len(arts) == 1 and arts[0]["name"] == "my-link"


def test_resolve_turn_workflow_and_memory(isolated_home):
    wf_store.create_def(
        {"name": "triage", "description": "PR triage", "steps": [{"id": "a", "title": "Fetch"}]}
    )
    paths.memory_index_path().write_text("用户喜欢喝茶。", encoding="utf-8")
    wf_id = wf_store.list_defs()[0]["id"]
    plan = resolve_turn(
        {
            "message": "hi",
            "mentions": [{"kind": "workflow", "id": wf_id}, {"kind": "memory"}],
        },
        SESSION,
    )
    kinds = {m["kind"] for m in plan.mention_ctx}
    assert kinds == {"workflow", "memory"}
    wf_item = next(m for m in plan.mention_ctx if m["kind"] == "workflow")
    assert "Fetch" in wf_item["summary"] and "PR triage" in wf_item["summary"]
    mem_item = next(m for m in plan.mention_ctx if m["kind"] == "memory")
    assert "用户喜欢喝茶" in mem_item["summary"]


def test_resolve_turn_memory_empty_skipped(isolated_home):
    # default boilerplate MEMORY.md (created by ensure_layout) → nothing injected
    paths.ensure_layout()
    plan = resolve_turn({"message": "hi", "mentions": [{"kind": "memory"}]}, SESSION)
    assert plan.mention_ctx == []


def test_resolve_turn_agent_override(isolated_home):
    msg = {"message": "hi", "mentions": [{"kind": "agent", "id": "research"}]}
    plan = resolve_turn(msg, SESSION)
    assert plan.agent_override == "research"
    # routing-only: no context section for agents
    assert all(m["kind"] != "agent" for m in plan.mention_ctx)


def test_help_lists_builtins_and_excludes_model_only(isolated_home):
    seed_skill(isolated_home, "good-skill", trigger="user-invocable")
    seed_skill(isolated_home, "hidden-skill", trigger="model-invocable")
    out = BUILTINS["help"].handler("default")
    assert "/good-skill" in out
    assert "/hidden-skill" not in out


# --------------------------------------------------------------------------- #
# /compact async handler — zombie checkpoint vs live turn (2026-10-12 fix)
# --------------------------------------------------------------------------- #
class _CompactGraph:
    """Fake graph whose `next` is settable, to stage either a clean state, a
    live turn, or a half-committed zombie checkpoint (restart killed a
    mid-superstep turn and the in-memory liveness registry died with it)."""

    def __init__(self, messages, next_=()):
        self._messages = messages
        self._next = next_

    async def aget_state(self, config):
        return SimpleNamespace(next=self._next, values={"messages": self._messages})

    async def aupdate_state(self, config, values, as_node=None):
        self._next = ()
        return None


async def _run_compact(monkeypatch, graph, *, running=(), compact_exc=None):
    """Drive BUILTINS['compact'].async_handler with everything server-side
    stubbed at the modules the handler lazily imports from. Returns
    (reply, calls) so tests assert on side effects, not on locale wording."""
    calls = {"heal": 0, "micro": 0, "compact": 0}

    async def _heal(graph, config):
        calls["heal"] += 1

    async def _micro(session, config):
        calls["micro"] += 1
        return None

    async def _compact(session, config, force=False, on_summary_start=None):
        calls["compact"] += 1
        assert force is True  # manual /compact must always force
        if compact_exc is not None:
            raise compact_exc
        return {"compacted_messages": 2, "summary_chars": 30, "kept_messages": 1}

    monkeypatch.setattr(api_stream, "_heal_interrupted_turn", _heal)
    monkeypatch.setattr(microcompact_mod, "maybe_microcompact_history", _micro)
    monkeypatch.setattr(compaction_mod, "maybe_compact_history", _compact)
    # The liveness registries are imported by name inside the handler, so
    # replacing the module attributes is enough to stage each scenario.
    monkeypatch.setattr(server_shared, "_RUNNING_TURNS", dict.fromkeys(running))
    monkeypatch.setattr(server_shared, "_PENDING_RESUME", set())
    monkeypatch.setattr(server_shared, "_TURN_TASKS", {})

    session = {"session_id": "s1", "project_slug": "default", "graph": graph}
    reply = await BUILTINS["compact"].async_handler("default", session, "")
    return reply, calls


async def test_compact_async_zombie_heals_then_compacts(isolated_home, monkeypatch):
    # `next` non-empty but nothing live in this process → zombie checkpoint:
    # heal it, then compact as usual instead of refusing forever.
    graph = _CompactGraph([HumanMessage(content=f"m{i}") for i in range(3)], next_=("agent",))
    reply, calls = await _run_compact(monkeypatch, graph)
    assert calls["heal"] == 1 and calls["compact"] == 1
    assert "✅" in reply  # success reply in either locale


async def test_compact_async_live_turn_rejected(isolated_home, monkeypatch):
    # `next` non-empty AND the turn is live in this process → refuse; heal
    # must never run against a turn that may still write checkpoints.
    graph = _CompactGraph([HumanMessage(content="m")], next_=("agent",))
    reply, calls = await _run_compact(monkeypatch, graph, running=("s1",))
    assert calls["heal"] == 0 and calls["compact"] == 0
    assert "/compact" in reply  # rejection wording names the command


async def test_compact_async_failure_returns_error_not_crash(isolated_home, monkeypatch):
    # THE regression (2026-10-12): a summarizer failure (429 from the
    # provider) reached the error logger and UnboundLocalError'd instead of
    # reporting — a `from ..server_shared import _log` inside the handler's
    # except block had made `_log` local to the whole function, so every
    # later `_log.exception(...)` crashed when that import never ran.
    graph = _CompactGraph([HumanMessage(content="m")])
    reply, calls = await _run_compact(monkeypatch, graph, compact_exc=RuntimeError("boom"))
    assert calls["compact"] == 1
    assert "RuntimeError" in reply and "boom" in reply  # real cause surfaced


# --------------------------------------------------------------------------- #
# /clear async handler — zombie checkpoint vs live turn (2026-10-15 fix,
# same two meanings of non-empty `next` as /compact above)
# --------------------------------------------------------------------------- #
class _ClearGraph(_CompactGraph):
    """Same staging surface as _CompactGraph; aupdate_state records the wipe."""

    def __init__(self, messages, next_=()):
        super().__init__(messages, next_=next_)
        self.wipes: list[dict] = []

    async def aupdate_state(self, config, values, as_node=None):
        self.wipes.append(values)
        self._next = ()
        return None


async def _run_clear(monkeypatch, graph, *, running=(), pending=()):
    """Drive BUILTINS['clear'].async_handler with the server-side pieces the
    handler lazily imports stubbed at their modules; returns (reply, calls)."""
    calls = {"heal": 0, "event": 0}

    async def _heal(graph_, config):
        calls["heal"] += 1

    async def _push(sid, event, payload, turn_id=None):
        calls["event"] += 1

    monkeypatch.setattr(api_stream, "_heal_interrupted_turn", _heal)
    monkeypatch.setattr(server_shared, "_push_session_event", _push)
    monkeypatch.setattr(server_shared, "_RUNNING_TURNS", dict.fromkeys(running))
    monkeypatch.setattr(server_shared, "_PENDING_RESUME", set(pending))
    monkeypatch.setattr(server_shared, "_TURN_TASKS", {})

    session = {"session_id": "s1", "project_slug": "default", "graph": graph}
    reply = await BUILTINS["clear"].async_handler("default", session, "")
    return reply, calls


async def test_clear_async_zombie_heals_then_clears(isolated_home, monkeypatch):
    # `next` non-empty but nothing live in this process (restart killed the
    # parked turn; the UI has no confirmation card to answer) → heal, then
    # wipe as usual instead of refusing forever.
    msgs = [HumanMessage(content=f"m{i}", id=f"m{i}") for i in range(3)]
    graph = _ClearGraph(msgs, next_=("agent",))
    reply, calls = await _run_clear(monkeypatch, graph)
    assert calls["heal"] == 1
    assert len(graph.wipes) == 1 and len(graph.wipes[0]["messages"]) == 3
    assert all(isinstance(v, RemoveMessage) for v in graph.wipes[0]["messages"])
    assert calls["event"] == 1  # history.cleared broadcast fired
    assert "🧹" in reply  # success reply in either locale


async def test_clear_async_live_parked_rejected(isolated_home, monkeypatch):
    # `next` non-empty AND the turn is parked at a live interrupt in this
    # process (a card the user can still answer) → refuse; the wipe must not
    # run and heal must never touch a turn that may still resume.
    graph = _ClearGraph([HumanMessage(content="m")], next_=("agent",))
    reply, calls = await _run_clear(monkeypatch, graph, pending=("s1",))
    assert calls["heal"] == 0 and calls["event"] == 0
    assert graph.wipes == []
    assert "/clear" in reply  # rejection wording names the command
