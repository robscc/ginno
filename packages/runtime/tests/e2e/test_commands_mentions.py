"""WebSocket E2E: slash commands + @mentions through the real graph.

Covers: /skill substitution reaches the model as a HumanMessage; filesystem
paths (`/tmp/...`) pass through untouched; /help short-circuits with a notice
event and NO model call; @artifact rides attached_files without duplicating
panel rows; @workflow / @memory inject <mentioned_*> system sections; @agent
overrides routing; raw @kind:label tokens resolve without a structured list.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import event_names, events_of
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, AIMessageChunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import PrivateAttr

from ginno_runtime import artifacts as art_store
from ginno_runtime import paths
from ginno_runtime import workflows as wf_store
from ginno_runtime.files import reset_registries
from ginno_runtime.world_state import TURN_CONTEXT_PREFIX

pytestmark = pytest.mark.e2e


def turn_context_of(model) -> str:
    """The last per-turn context message (plan B1) the model received."""
    for h in reversed(model._humans):
        if isinstance(h, str) and h.startswith(TURN_CONTEXT_PREFIX):
            return h
    return ""


@pytest.fixture(autouse=True)
def _fresh_files(isolated_home):
    # The file registry keeps module-level globals; isolated_home does not
    # reset them (same guard as test_files_ws.py).
    reset_registries()
    yield
    reset_registries()


class CapturingModel(BaseChatModel):
    """Fixed reply; records every system prompt and the last human message."""

    reply: str = "ok"
    _captured: list = PrivateAttr(default_factory=list)  # system prompts
    _humans: list = PrivateAttr(default_factory=list)  # human message contents

    @property
    def _llm_type(self) -> str:
        return "capturing"

    def bind_tools(self, tools, **kwargs):
        return self

    def _capture(self, messages) -> None:
        for m in messages:
            t = getattr(m, "type", "")
            if t == "system":
                c = m.content
                self._captured.append(c if isinstance(c, str) else "")
            elif t == "human":
                c = m.content
                self._humans.append(c if isinstance(c, str) else str(c))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.reply))])

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        return self._generate(messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._capture(messages)
        yield ChatGenerationChunk(message=AIMessageChunk(content=self.reply, id="cap"))


def seed_skill(home: Path, name: str, trigger: str = "both", body: str = "Skill body.") -> None:
    d = home / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\ntrigger: {trigger}\n---\n\n{body}\n",
        encoding="utf-8",
    )


# --------------------------------------------------------------------------- #
# slash skills
# --------------------------------------------------------------------------- #
def test_slash_skill_substitution_reaches_model(create_session, ws_conv, isolated_home):
    seed_skill(isolated_home, "summarize-notes", body="Summarize the given notes.")
    model = CapturingModel(reply="done")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/summarize-notes please condense my notes")
        events = conv.recv_until("message.end", "error")
    assert event_names(events)[-1] == "message.end"
    assert model._humans, "model never received a human message"
    human = model._humans[-1]
    assert '<skill name="summarize-notes">' in human
    assert "Summarize the given notes." in human
    assert "User request: please condense my notes" in human


def test_filesystem_path_passthrough(create_session, ws_conv, isolated_home):
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/tmp/foo is where the logs live")
        conv.recv_until("message.end", "error")
    assert model._humans[-1] == "/tmp/foo is where the logs live"


def test_model_invocable_skill_not_slash_callable(create_session, ws_conv, isolated_home):
    seed_skill(isolated_home, "hidden", trigger="model-invocable")
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/hidden do it")
        conv.recv_until("message.end", "error")
    # trigger gating: the text passes through unmodified
    assert model._humans[-1] == "/hidden do it"


def test_help_short_circuits_without_model(create_session, ws_conv, isolated_home):
    seed_skill(isolated_home, "summarize-notes")
    model = CapturingModel(reply="should never run")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("/help")
        events = conv.recv_until("message.end", "error")
    assert event_names(events) == ["notice", "message.end"]
    assert not model._captured and not model._humans  # zero model calls
    notice = events_of(events, "notice")[0]
    assert "/help" in notice["message"]
    assert "/summarize-notes" in notice["message"]


# --------------------------------------------------------------------------- #
# @mentions
# --------------------------------------------------------------------------- #
def test_artifact_mention_attaches_file_without_dup_row(
    create_session, ws_conv, isolated_home, client
):
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    f = isolated_home / "sales.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    art = art_store.add_artifact("default", "table", "sales.csv", str(f), sid)

    with ws_conv(sid) as conv:
        conv.invoke("帮我看看这个数据", mentions=[{"kind": "artifact", "id": art["id"]}])
        conv.recv_until("message.end", "error")
    # plan B1: attached files ride the turn-context message, not the system
    turn_ctx = turn_context_of(model)
    assert "<attached_files>" in turn_ctx
    assert "sales.csv" in turn_ctx
    # no duplicate artifact row was created by the resolution path
    rows = [a for a in art_store.list_artifacts("default") if a.get("name") == "sales.csv"]
    assert len(rows) == 1


def test_workflow_mention_injects_context(create_session, ws_conv):
    wf_store.create_def(
        {
            "name": "triage",
            "description": "PR triage flow",
            "steps": [{"id": "a", "title": "Fetch PRs"}],
        }
    )
    wf_id = wf_store.list_defs()[0]["id"]
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("跑一下", mentions=[{"kind": "workflow", "id": wf_id}])
        conv.recv_until("message.end", "error")
    turn_ctx = turn_context_of(model)
    assert "<mentioned_workflow>" in turn_ctx
    assert "PR triage flow" in turn_ctx
    assert "Fetch PRs" in turn_ctx


def test_memory_mention_injects_memory(create_session, ws_conv, isolated_home):
    paths.memory_index_path().write_text("用户偏好：先给结论再给细节。", encoding="utf-8")
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi", mentions=[{"kind": "memory"}])
        conv.recv_until("message.end", "error")
    turn_ctx = turn_context_of(model)
    assert "<mentioned_memory>" in turn_ctx
    assert "先给结论再给细节" in turn_ctx


def test_memory_mention_empty_skipped(create_session, ws_conv):
    # isolated_home has no MEMORY.md content (or only boilerplate)
    paths.ensure_layout()
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi", mentions=[{"kind": "memory"}])
        conv.recv_until("message.end", "error")
    assert "<mentioned_memory>" not in model._captured[-1]


def test_agent_mention_overrides_routing(create_session, ws_conv, client):
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hi", mentions=[{"kind": "agent", "id": "research"}])
        events = conv.recv_until("message.end", "error")
    turn = events_of(events, "turn.start")[0]
    assert turn["agent_id"] == "research"
    # the session agent was persisted server-side
    meta = client.get(f"/api/sessions/{sid}").json()
    assert meta["agent_id"] == "research"


def test_mention_token_fallback_without_structured_list(create_session, ws_conv):
    wf_store.create_def(
        {"name": "deploy", "description": "d", "steps": [{"id": "a", "title": "Ship it"}]}
    )
    model = CapturingModel(reply="ok")
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        # raw API style: token in text, no structured mentions key
        conv.send({"type": "invoke", "message": "@workflow:deploy run it"})
        conv.recv_until("message.end", "error")
    turn_ctx = turn_context_of(model)
    assert "<mentioned_workflow>" in turn_ctx
    assert "Ship it" in turn_ctx
