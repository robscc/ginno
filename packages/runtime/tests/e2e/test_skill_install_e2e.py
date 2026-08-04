"""E2E: skill installation context + tools over the real WS flow.

Regression suite for the 2026-08-04 incident (turn 70758149): the user
asked "install all skills from this repo" and the agent — knowing neither
its workspace nor the skills directories, and having no install tool —
probed ``pwd``/``$HOME``, globbed from the sidecar cwd ``/`` and crashed
the whole turn with ``OSError: [Errno 22]``.

Cases (each maps to one missing piece of that failure):

1. system prompt carries workspace + skills dirs + install guidance  (context)
2. bash/file tools run in the session workspace, not the sidecar cwd (F1)
3. install_skills installs a ponytail-shaped tree; next turn announces it
   via WorldState and the skills are usable/listed                    (tools)
4. a bad tool call degrades to an error ToolMessage — the turn survives
   instead of dying with a 500                                        (containment)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import events_of, script, script_tool_call
from langchain_core.messages import SystemMessage
from pydantic import PrivateAttr

from ginno_runtime import paths
from ginno_runtime.testing.fake_model import ScriptedChatModel

pytestmark = pytest.mark.e2e


@pytest.fixture
def bypass_on(client, isolated_home):
    """Privileged mode for tool-driving tests: the conftest client defaults
    to approval mode, where bash/write_file interrupt for permission (covered
    separately by test_permission_flow). Here we want the tool path itself."""
    sp = isolated_home / "settings.json"
    s = json.loads(sp.read_text())
    s["bypass_permissions"] = True
    sp.write_text(json.dumps(s))


class CapturingModel(ScriptedChatModel):
    """Scripted model that records every message list it is invoked with, so
    tests can assert on the exact system prompt the runtime sent. The WS
    server streams via ``_astream`` (not ``_generate``), so both entry points
    record."""

    _seen: list = PrivateAttr(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self._seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        self._seen.append(list(messages))
        async for chunk in super()._astream(messages, stop, run_manager, **kwargs):
            yield chunk

    def system_texts(self) -> list[str]:
        out = []
        for call in self._seen:
            for m in call:
                if isinstance(m, SystemMessage):
                    c = m.content
                    out.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
        return out


def _skill_dir(root: Path, name: str, desc: str, extra: dict[str, str] | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: >\n  {desc}\n---\n\n# {name}\n\nbody\n",
        encoding="utf-8",
    )
    for fn, content in (extra or {}).items():
        (d / fn).write_text(content, encoding="utf-8")
    return d


@pytest.fixture
def ponytail_repo(tmp_path):
    """Shape of DietrichGebert/ponytail: <repo>/skills/<name>/SKILL.md."""
    skills = tmp_path / "ponytail" / "skills"
    _skill_dir(skills, "ponytail", "The lazy senior dev. One line. It works.")
    _skill_dir(skills, "ponytail-audit", "Whole-repo audit for over-engineering.")
    (tmp_path / "ponytail" / "README.md").write_text("# Ponytail\n")
    return tmp_path / "ponytail"


# --------------------------------------------------------------------------- #
# 1. context: the model is told where it is and where skills go
# --------------------------------------------------------------------------- #
def test_system_prompt_carries_workspace_and_skills_context(create_session, ws_conv):
    model = CapturingModel(scripts=[script(text="ok")])
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("你好")
        conv.recv_until("message.end", "error")

    sys_texts = model.system_texts()
    assert sys_texts, "model was never invoked"
    prompt = sys_texts[0]

    workspace = str(paths.session_files_dir("default", sid))
    # the session workspace (was frozen out until this incident)
    assert f"<workspace>{workspace}" in prompt
    # skills install locations
    assert str(paths.global_skills_dir()) in prompt
    assert str(paths.project_skills_dir("default")) in prompt
    # install guidance names the real tool
    assert "install_skills" in prompt
    # zero skills installed yet — the section must still be present
    assert "尚未安装" in prompt


def test_research_agent_gets_dirs_but_not_install_guidance(create_session, ws_conv):
    model = CapturingModel(scripts=[script(text="ok")])
    sid = create_session(model, agent_id="research")  # narrow tools_allow
    with ws_conv(sid) as conv:
        conv.invoke("hi")
        conv.recv_until("message.end", "error")
    prompt = model.system_texts()[0]
    assert str(paths.global_skills_dir()) in prompt
    assert "install_skills" not in prompt  # it cannot call the tool


# --------------------------------------------------------------------------- #
# 2. F1: tools run in the session workspace, never the sidecar cwd
# --------------------------------------------------------------------------- #
def test_bash_pwd_is_session_workspace(create_session, ws_conv, bypass_on):
    model = CapturingModel(
        scripts=[
            script(tool_calls=[script_tool_call("bash", {"command": "pwd"})]),
            script(text="done"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    workspace = str(paths.session_files_dir("default", sid))
    with ws_conv(sid) as conv:
        conv.invoke("pwd?")
        events = conv.recv_until("message.end", "error")

    assert not events_of(events, "error"), "turn must not fail"
    tool_ends = events_of(events, "tool.end")
    assert tool_ends and workspace in tool_ends[0]["content"]


def test_write_file_relative_lands_in_workspace(create_session, ws_conv, client, bypass_on):
    model = CapturingModel(
        scripts=[
            script(
                tool_calls=[script_tool_call("write_file", {"path": "note.md", "content": "hi"})]
            ),
            script(text="done"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("write it")
        conv.recv_until("message.end", "error")
    assert (paths.session_files_dir("default", sid) / "note.md").exists()


# --------------------------------------------------------------------------- #
# 3. the install flow itself — ponytail-shaped repo → global skills
# --------------------------------------------------------------------------- #
def test_install_skills_flow_end_to_end(create_session, ws_conv, client, ponytail_repo):
    model = CapturingModel(
        scripts=[
            script(tool_calls=[
                script_tool_call("install_skills", {"path": str(ponytail_repo / "skills")})
            ]),
            script(text="已安装完成"),
            script(text="第二轮"),  # next turn: WorldState should announce the change
        ]
    )
    sid = create_session(model, agent_id="dev")

    # turn 1 — install
    with ws_conv(sid) as conv:
        conv.invoke("安装这个仓库里的所有 skill")
        events = conv.recv_until("message.end", "error")
    assert not events_of(events, "error"), "install turn must not fail"
    tool_ends = events_of(events, "tool.end")
    report = json.loads(tool_ends[0]["content"])
    assert report["ok"] is True
    assert {x["name"] for x in report["imported"]} == {"ponytail", "ponytail-audit"}
    # turn end announces the skills mutation → the UI reloads its slash menu
    # (2026-08-05: installed skills were invisible in the / menu until reload)
    assert events_of(events, "skills.changed")

    # files actually landed in the global skills dir
    g = paths.global_skills_dir()
    assert (g / "ponytail" / "SKILL.md").exists()
    assert (g / "ponytail-audit" / "SKILL.md").exists()
    listed = {s["name"] for s in client.get("/api/skills").json()}
    assert {"ponytail", "ponytail-audit"} <= listed

    # turn 2 — the WorldState diff announces the new skills (C1/C2)
    with ws_conv(sid) as conv:
        conv.invoke("还在吗")
        events2 = conv.recv_until("message.end", "error")
    ups = events_of(events2, "context.updated")
    assert len(ups) == 1
    assert {c["section"] for c in ups[0]["changes"]} == {"skills"}
    summary = ups[0]["changes"][0]["summary"]
    assert "ponytail" in summary

    # and the system prompt of turn 2 now lists the installed skills
    prompt2 = model.system_texts()[-1]
    assert "ponytail" in prompt2 and "install_skills" in prompt2


def test_rest_import_notifies_open_sessions(create_session, ws_conv, client, ponytail_repo):
    """Settings-page flow: POST /api/skills/import-dir must push
    skills.changed to every connected session so the / menu updates live."""
    sid = create_session([script(text="hi")], agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("hello")
        conv.recv_until("message.end", "error")
        r = client.post(
            "/api/skills/import-dir",
            json={"path": str(ponytail_repo / "skills")},
        ).json()
        assert r["ok"] is True and len(r["imported"]) == 2
        evs = conv.recv_until("skills.changed", "error")
        assert not events_of(evs, "error")
        assert evs[-1]["event"] == "skills.changed"
    listed = {s["name"] for s in client.get("/api/skills").json()}
    assert {"ponytail", "ponytail-audit"} <= listed


def test_install_skills_bad_path_keeps_turn_alive(create_session, ws_conv):
    model = CapturingModel(
        scripts=[
            script(tool_calls=[
                script_tool_call("install_skills", {"path": "/no/such/repo/skills"})
            ]),
            script(text="路径不对，我换个方式"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("安装 /no/such/repo 里的 skill")
        events = conv.recv_until("message.end", "error")
    assert not events_of(events, "error")
    report = json.loads(events_of(events, "tool.end")[0]["content"])
    assert report["ok"] is False and report["error"]
    assert events[-1]["event"] == "message.end"


# --------------------------------------------------------------------------- #
# 4. containment: hallucinated tool call ≠ dead turn (incident shape)
# --------------------------------------------------------------------------- #
def test_hallucinated_tool_call_does_not_kill_turn(create_session, ws_conv, client, bypass_on):
    model = CapturingModel(
        scripts=[
            # the incident model invented paths; here we simulate the sibling
            # failure — invoking a tool that doesn't exist must not 500 the turn
            script(tool_calls=[script_tool_call("ghost_tool", {"x": 1})]),
            script(text="我恢复过来了"),
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("do it")
        events = conv.recv_until("message.end", "error")

    assert not events_of(events, "error"), "turn must survive a bad tool call"
    assert events[-1]["event"] == "message.end"
    # the error surfaced as a tool result the model could read, not a crash
    tool_ends = events_of(events, "tool.end")
    assert tool_ends and tool_ends[0]["content"]
    history = client.get(f"/api/sessions/{sid}/history").json()
    assert history["ok"] is True
    assert any("ghost_tool" in str(m.get("blocks")) for m in history["messages"])
