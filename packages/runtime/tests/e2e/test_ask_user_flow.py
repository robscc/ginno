"""WebSocket E2E: ask_user -> interrupt -> user.question -> user_answer -> resume.

The 2026-09-17 incident this closes: "导入 skill" was ambiguous (Ginno's
~/.ginno/skills vs. the repo's .claude/skills), the model had no way to ask,
guessed Ginno, and burned ~80 model calls before the user corrected it in
prose. Here the model parks the turn on an interrupt() and the WS layer
surfaces it as user.question until a user_answer resumes it.

The card is a TRANSCRIPT block (unlike the permission prompt, which is a
client-side ref), so the two properties that matter most are:
* the event carries the tool_call id, which is what lets a reconnect re-emit
  merge into the block the client already rebuilt from history;
* the question survives in history afterwards.
"""

from __future__ import annotations

import json

import pytest
from conftest import event_names, events_of, script, script_tool_call

from ginno_runtime import server_shared

pytestmark = pytest.mark.e2e


def _ask_call(call_id: str = "call_ask_1") -> dict:
    return script_tool_call(
        "ask_user",
        {
            "question": "「导入 skill」装到哪？",
            "options": ["Ginno 全局", "本仓库 .claude/skills"],
            "header": "选择安装位置",
        },
        call_id,
    )


def _result_of(events: list[dict]) -> dict:
    ends = events_of(events, "tool.end")
    assert ends, "no tool.end for ask_user"
    return json.loads(ends[-1]["content"])


# --------------------------------------------------------------------------- #
# happy path
# --------------------------------------------------------------------------- #
def test_question_parks_then_resumes_with_the_chosen_option(create_session, ws_conv):
    model = [script(tool_calls=[_ask_call()]), script(text="装到仓库的 .claude/skills 了。")]
    sid = create_session(model)

    with ws_conv(sid) as conv:
        conv.invoke("导入 ponytail skill")
        first = conv.recv_until("user.question", "message.end", "error")
        names = event_names(first)
        # the turn parks — no message.end until the user answers
        assert "user.question" in names
        assert "message.end" not in names

        q = events_of(first, "user.question")[0]
        assert q["id"] == "call_ask_1"  # the tool_call id: the client's merge key
        assert q["question"] == "「导入 skill」装到哪？"
        assert q["options"] == ["Ginno 全局", "本仓库 .claude/skills"]
        assert q["header"] == "选择安装位置"
        assert q["allow_free_text"] is True

        conv.respond_answer("本仓库 .claude/skills", option_index=1)
        rest = conv.recv_until("message.end", "error")
        assert not events_of(rest, "error")

    out = _result_of(rest)
    assert out["ok"] is True and out["skipped"] is False
    assert out["source"] == "option" and out["option_index"] == 1
    assert out["answer"] == "本仓库 .claude/skills"


def test_free_text_answer(create_session, ws_conv):
    model = [script(tool_calls=[_ask_call()]), script(text="ok")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")
        conv.respond_answer("/tmp/elsewhere", option_index=None)
        rest = conv.recv_until("message.end", "error")
    out = _result_of(rest)
    assert out["source"] == "free_text" and out["answer"] == "/tmp/elsewhere"


def test_skip_lets_the_model_proceed(create_session, ws_conv):
    model = [script(tool_calls=[_ask_call()]), script(text="好，我按默认装全局，并说明这个假定。")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")
        conv.respond_answer(skip=True)
        rest = conv.recv_until("message.end", "error")
    out = _result_of(rest)
    assert out["skipped"] is True and out["source"] == "skipped"


def test_ask_user_bypasses_the_permission_policy(create_session, ws_conv):
    """ask_user carries its OWN interrupt; a policy "ask" ahead of it would
    make the user answer two prompts for one question. The conftest client
    runs with bypass OFF, so a permission.request here would be a regression."""
    model = [script(tool_calls=[_ask_call()]), script(text="ok")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        first = conv.recv_until("user.question", "message.end", "error")
        assert "permission.request" not in event_names(first)
        conv.respond_answer(skip=True)
        rest = conv.recv_until("message.end", "error")
    assert "permission.request" not in event_names(rest)


# --------------------------------------------------------------------------- #
# the question survives the client
# --------------------------------------------------------------------------- #
def _question_blocks(client, sid: str) -> list[dict]:
    hist = client.get(f"/api/sessions/{sid}/history").json()["messages"]
    return [
        b
        for m in hist
        for b in (m.get("blocks") or [])
        if isinstance(b, dict) and b.get("kind") == "question"
    ]


def test_parked_question_is_already_in_history(client, create_session, ws_conv):
    """THE property that makes the card survive a reload: while the turn is
    parked there is no ToolMessage yet, so replay reconstructs the question
    from the AIMessage's tool_call alone — with no side store. A client that
    reloads fetches history first and sees the card still pending."""
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)

    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")

        parked = _question_blocks(client, sid)
        assert parked, "a parked question must be reconstructible from history"
        assert parked[0]["id"] == "call_ask_1"
        assert parked[0]["status"] == "pending"
        assert parked[0]["options"] == ["Ginno 全局", "本仓库 .claude/skills"]

        conv.respond_answer("Ginno 全局", option_index=0)
        conv.recv_until("message.end", "error")

    done = _question_blocks(client, sid)
    assert done[0]["status"] == "answered"
    assert done[0]["answer"] == "Ginno 全局"
    assert done[0]["optionIndex"] == 0


def test_answering_works_from_a_new_socket(client, create_session, ws_conv):
    """A reloaded client answers over a fresh socket. The resume guards are
    in-memory, so they are still armed from the parking turn."""
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)
    with ws_conv(sid) as conv1:
        conv1.invoke("导入 skill")
        conv1.recv_until("user.question")
        with ws_conv(sid) as conv2:
            conv2.respond_answer("本仓库 .claude/skills", option_index=1)
            rest = conv2.recv_until("message.end", "error")
            assert not events_of(rest, "error")
    out = _result_of(rest)
    assert out["source"] == "option" and out["option_index"] == 1


def test_parked_interrupt_is_readable_from_the_checkpoint(client, create_session, ws_conv):
    """The reconnect re-emit in api/stream.py reads the pending interrupt out of
    the checkpoint (graph.aget_state().tasks[].interrupts). That only works when
    the checkpointer surfaces pending_writes — the `__interrupt__` write IS
    persisted either way, but with the old chat default it was hidden and
    langgraph rebuilt no interrupts, making the re-emit dead code for chat.

    This is the unit-level half of the fix; the WS half is the test below.
    """
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")
        # The suspension checkpoint is flushed only when the turn's stream
        # generator finishes draining (see the _turn_lock comment in
        # api/stream.py), and the event above is emitted DURING that drain — so
        # poll rather than sleep a fixed amount.
        assert _await_pending_kinds(sid) == ["user_question"]


def test_reconnect_reemits_the_parked_question(client, create_session, ws_conv):
    """End to end: a client that connects while the turn is parked is told about
    the pending question — that is what re-arms _PENDING_RESUME after a runtime
    restart (the in-memory guard is gone, the checkpoint is not)."""
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)
    with ws_conv(sid) as conv1:
        conv1.invoke("导入 skill")
        conv1.recv_until("user.question")
        assert _await_pending_kinds(sid) == ["user_question"]

        # A reconnect (or a restarted runtime): the fresh socket's open-handler
        # replays the interrupt from the checkpoint.
        with ws_conv(sid) as conv2:
            again = conv2.recv_until("user.question", "error")
            assert not events_of(again, "error")
            q = events_of(again, "user.question")[0]
            # Same tool_call id → the client merges instead of duplicating.
            assert q["id"] == "call_ask_1"
            assert q["options"] == ["Ginno 全局", "本仓库 .claude/skills"]

            conv2.respond_answer("Ginno 全局", option_index=0)
            rest = conv2.recv_until("message.end", "error")
            assert not events_of(rest, "error")
    assert _result_of(rest)["option_index"] == 0


def _await_pending_kinds(sid: str, timeout: float = 10.0) -> list[str]:
    """Poll the checkpoint for the parked interrupt kinds (bounded)."""
    import time as _t

    deadline = _t.time() + timeout
    kinds: list[str] = []
    while _t.time() < deadline:
        kinds = _pending_interrupt_kinds(sid)
        if kinds:
            return kinds
        _t.sleep(0.1)
    return kinds


def _pending_interrupt_kinds(sid: str) -> list[str]:
    """What the reconnect path finds: the parked interrupt kinds.

    Read through the checkpointer helper the server itself uses. NOT via
    graph.aget_state().tasks[].interrupts — that comes back empty for an
    interrupt raised inside the tools node (see get_pending_interrupt).
    """
    sess = server_shared._SESSIONS[sid]
    cfg = {"configurable": {"thread_id": sid, "project_slug": "default"}}
    pending = sess["graph"].checkpointer.get_pending_interrupt(cfg)
    return [
        (getattr(i, "value", None) or {}).get("kind")
        for i in pending or []
    ]


# --------------------------------------------------------------------------- #
# stale / duplicate answers
# --------------------------------------------------------------------------- #
def test_second_user_answer_is_ignored(client, create_session, ws_conv):
    """Two tabs both see the question; a late second answer must not
    double-resume the graph."""
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)
    with ws_conv(sid) as conv1, ws_conv(sid) as conv2:
        conv1.invoke("导入 skill")
        conv1.recv_until("user.question")
        conv2.recv_until("user.question")

        conv1.respond_answer("Ginno 全局", option_index=0)
        assert conv1.recv_until("message.end", "error")[-1]["event"] == "message.end"

        conv2.respond_answer("本仓库 .claude/skills", option_index=1)
        conv2.send({"type": "ping"})
        evs = conv2.recv_until("pong", "error")
        assert not events_of(evs, "error")
        assert evs[-1]["event"] == "pong"


def test_permission_response_cannot_resume_a_question(client, create_session, ws_conv):
    """The two interrupts owe DIFFERENT resume payload shapes. A stale
    permission_response while a question is parked must be refused — and must
    leave the question answerable (the guard peeks, it does not pop)."""
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")
        conv.respond_permission("allow")
        conv.respond_answer(skip=True)
        rest = conv.recv_until("message.end", "error")
        assert not events_of(rest, "error")
    assert _result_of(rest)["skipped"] is True


def test_stop_while_parked_heals_the_turn(client, create_session, ws_conv):
    model = [script(tool_calls=[_ask_call()]), script(text="unreachable")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")
        conv.send({"type": "stop"})
        evs = conv.recv_until("turn.stopped", "error")
        assert "turn.stopped" in event_names(evs)

    # The dangling tool call is healed to "(interrupted)" → the history shows a
    # skipped question rather than one that waits forever.
    parked = _question_blocks(client, sid)
    assert parked and parked[0]["status"] == "skipped"


def test_resume_guards_are_armed_and_consumed(create_session, ws_conv):
    """_PENDING_RESUME is armed exactly once per parked turn, and the answer
    consumes it (that is what makes a duplicate from a second tab a no-op)."""
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")
        assert sid in server_shared._PENDING_RESUME
        assert server_shared._PENDING_KIND.get(sid) == "user_question"
        conv.respond_answer(skip=True)
        conv.recv_until("message.end", "error")
    assert sid not in server_shared._PENDING_RESUME
    assert sid not in server_shared._PENDING_KIND


def test_interrupt_helper_is_quiet_once_resumed(create_session, ws_conv):
    """Liveness rule: a RESOLVED interrupt must never be replayed. The helper
    walks back to the newest entry with writes — after the resume those are
    ordinary channel writes, so it reports nothing."""
    model = [script(tool_calls=[_ask_call()]), script(text="done")]
    sid = create_session(model)
    with ws_conv(sid) as conv:
        conv.invoke("导入 skill")
        conv.recv_until("user.question")
        assert _await_pending_kinds(sid) == ["user_question"]
        conv.respond_answer(skip=True)
        conv.recv_until("message.end", "error")
    assert _pending_interrupt_kinds(sid) == []
