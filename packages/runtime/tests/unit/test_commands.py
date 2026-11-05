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
from ginno_runtime import compaction as compaction_mod
from ginno_runtime import microcompact as microcompact_mod
from ginno_runtime import paths, server_shared
from ginno_runtime import session_links
from ginno_runtime.session_links import stamp_link
from ginno_runtime.session_meta import _session_meta_upsert as _meta_upsert


def _put_session(sid: str, title: str) -> None:
    import time as _t
    _meta_upsert("default", {"id": sid, "title": title, "type": "main",
                             "created": _t.time(), "updated": _t.time()})
from ginno_runtime.api import stream as api_stream
from ginno_runtime.commands.registry import BUILTINS
from ginno_runtime.commands.resolver import (
    parse_mention_tokens,
    parse_slash,
    resolve_mentions,
    resolve_turn,
    substitute_skill,
)

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
        "hey @artifact:report.csv and @agent:Dev plus @session:雷达 and @result"
    )
    assert toks == [
        {"kind": "artifact", "label": "report.csv"},
        {"kind": "agent", "label": "Dev"},
        {"kind": "session", "label": "雷达"},
        {"kind": "result", "label": None},
    ]
    # 旧类别已退役（2026-11-05 @ 范围整理）：token 层不再识别
    assert parse_mention_tokens("run @workflow:triage and @memory") == []


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


def test_resolve_mentions_session_label_fallback(isolated_home):
    # linked 集合内按标题精确命中（at-session-design §5.5）
    s1 = "sess-main-1"; _put_session(s1, "主会话")
    s2 = "sess-radar-2"; _put_session(s2, "雷达任务")
    stamp_link(s1, s2, "watching")
    resolved, _ = resolve_mentions(None, "run @session:雷达任务 please", "default", cur_session_id=s1)
    assert [r["kind"] for r in resolved] == ["session"]
    assert resolved[0]["label"] == "雷达任务"
    assert resolved[0]["id"] == s2
    # 集合外标题不命中（quiet ignore）
    resolved2, _ = resolve_mentions(None, "@session:不存在的", "default", cur_session_id=s1)
    assert resolved2 == []


def test_resolve_mentions_text_fallback_skips_covered_kinds(isolated_home):
    s1 = "sess-main-1"; _put_session(s1, "主会话")
    s2 = "sess-radar-2"; _put_session(s2, "甲")
    s3 = "sess-other-3"; _put_session(s3, "乙")
    stamp_link(s1, s2, "watching")
    stamp_link(s1, s3, "watching")
    resolved, _ = resolve_mentions(
        [{"kind": "session", "id": s2}],
        "@session:乙 and @result:甲",
        "default",
        cur_session_id=s1,
    )
    kinds = [r["kind"] for r in resolved]
    # structured session 已覆盖 → token 兜底只补 result（同 kind 不重复）
    assert kinds == ["session", "result"]


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


def test_resolve_turn_session_delivery_and_result_cite(isolated_home):
    # @session → deliveries（无 mention_ctx）；@result → 目标最后 assistant
    # 正文注入 <mentioned_result>（空目标 → 「尚无回复」占位）
    s1 = "sess-main-1"; _put_session(s1, "主会话")
    s2 = "sess-radar-2"; _put_session(s2, "雷达")
    stamp_link(s1, s2, "watching")
    plan = resolve_turn(
        {
            "message": "hi",
            "mentions": [{"kind": "session", "id": s2}, {"kind": "result", "id": s2}],
        },
        {**SESSION, "id": s1},
    )
    assert plan.deliveries == [{"id": s2, "label": "雷达"}]
    assert [m["kind"] for m in plan.mention_ctx] == ["result"]
    assert "no assistant reply yet" in plan.mention_ctx[0]["summary"]


def test_resolve_turn_result_cites_last_reply(isolated_home):
    s1 = "sess-main-1"; _put_session(s1, "主会话")
    s2 = "sess-radar-2"; _put_session(s2, "雷达")
    stamp_link(s1, s2, "watching")
    # 直接落一个含 assistant 消息的 checkpoint（同步 put 接口）
    from ginno_runtime.checkpointer import FileCheckpointer
    cp = FileCheckpointer("default")
    cp.put(
        {"configurable": {"thread_id": s2}},
        checkpoint={
            "id": "ckpt-1",
            "channel_values": {
                "messages": [
                    {"type": "human", "content": "跑一期"},
                    {"type": "ai", "content": ""},
                    {"type": "ai", "content": [{"type": "text", "text": "第 8 期结论：OK"}]},
                ]
            },
        },
        metadata={},
        new_versions={},
    )
    plan = resolve_turn(
        {"message": "总结下", "mentions": [{"kind": "result", "id": s2}]},
        {**SESSION, "id": s1},
    )
    assert "第 8 期结论：OK" in plan.mention_ctx[0]["summary"]


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


# --------------------------------------------------------------------------- #
# /dev-swap — 参数校验与失败 notice 清理
# --------------------------------------------------------------------------- #
async def test_dev_swap_rejects_git_url_with_hint(isolated_home):
    # git URL 是最高频误用（真机 2026-11-04：用户先发了 GitHub 地址）——
    # 必须命中专门的 clone 提示，而不是泛泛的 usage 文案。
    from ginno_runtime.commands.registry import _dev_swap_async_handler

    for url in ("https://github.com/robscc/ginno", "git@github.com:robscc/ginno.git"):
        reply = await _dev_swap_async_handler("default", {"session_id": "s1"}, url)
        assert "clone" in reply  # en/zh 双语文案都含 "clone"


async def test_dev_swap_failure_notice_strips_ansi(isolated_home, monkeypatch):
    # 失败 notice 携带构建尾部输出；pytest/make/脚本的颜色码若不剥掉，
    # 聊天气泡里就是 [31m[dev-swap][0m 这种乱码（2026-11-04 真机截图所见）。
    import asyncio as _aio

    from ginno_runtime.commands import registry as commands_registry

    async def fake_exec(*args, **kw):
        class FakeStdout:
            def __init__(self):
                self._lines = iter([
                    b"\x1b[31m[dev-swap]\x1b[0m uv \xe4\xb8\x8d\xe5\x9c\xa8 PATH\n",
                ])

            async def readline(self):
                try:
                    return next(self._lines)
                except StopIteration:
                    return b""

            async def wait(self):
                return 1

        proc = SimpleNamespace(returncode=1, stdout=FakeStdout())
        proc.wait = lambda: _aio.sleep(0)  # 调用返回 coroutine，await 一次即完成
        return proc

    events = []

    async def fake_push(session_id, event, data, turn_id=None):
        events.append((event, data))

    monkeypatch.setattr(_aio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(server_shared, "_push_session_event", fake_push)
    await commands_registry._dev_swap_background("s1", "/repo")
    assert [e for e, _ in events] == ["notice"]
    msg = events[0][1]["message"]
    assert "\x1b" not in msg and "[31m" not in msg
    assert "[dev-swap] uv" in msg and "exit 1" in msg


def test_result_mention_resolves_beyond_linked_but_session_does_not(monkeypatch, tmp_path) -> None:
    """@result 引用语义可指向任意历史会话（2026-11-05）；@session 投递语义
    仍 linked-only——linked 集合外的目标被忽略。"""
    from ginno_runtime.commands import resolver as R
    from ginno_runtime import session_meta as SM

    # 造一个不在 linked 集合里的会话 meta（注入全库迭代器，免依赖磁盘布局）
    monkeypatch.setattr(
        R, "_all_session_metas",
        lambda: iter([({"id": "out-sid", "title": "外部调研", "type": "main"}, "default")]),
    )
    monkeypatch.setattr(R, "_linked_member", lambda cur, sid=None, label=None: None)
    monkeypatch.chdir(tmp_path)

    from ginno_runtime.commands import resolver as res

    got, _ = res.resolve_mentions("cur", "结合 @result:外部调研 的结论", [])
    kinds = [(r["kind"], r["id"]) for r in got]
    assert ("result", "out-sid") in kinds, kinds

    got2, _ = res.resolve_mentions("cur", "@session:外部调研 去干活", [])
    assert not [r for r in got2 if r["kind"] == "session"], got2
