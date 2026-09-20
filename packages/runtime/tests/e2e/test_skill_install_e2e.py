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
from langchain_core.messages import HumanMessage, SystemMessage
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
    # install guidance names the real tool and ALL THREE targets, and tells the
    # model to ask rather than assume when the user did not say which
    # (2026-09-17: "导入 skill" silently went to Ginno's global dir)
    assert "install_skills" in prompt
    assert 'target="global"' in prompt
    assert 'target="project"' in prompt
    assert 'target="repo"' in prompt
    assert "ask_user" in prompt
    # the builtin tier always ships the `todo` skill, so the index is never
    # empty; the section must be present and list it
    assert "- todo:" in prompt


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


# --------------------------------------------------------------------------- #
# 6. install targets: the repo's .claude/skills (2026-09-17 incident)
# --------------------------------------------------------------------------- #
def test_install_into_a_discovered_repo(
    create_session, ws_conv, bypass_on, ponytail_repo, repo_outside_home
):
    """End to end: the agent works in a repo by ABSOLUTE path (never mounted),
    then installs a skill into that repo's .claude/skills. The repo becomes
    knowable because the tool layer observed it (projects.py), and
    install_skills(target="repo") is allowlisted to it."""
    home = paths.home()
    repo = repo_outside_home / "claude-agent-team"
    (repo / ".git").mkdir(parents=True)
    # Claude-style markers, like the incident's repo: the same-turn [project]
    # announcement is reserved for these (a plain .git repo is recorded
    # silently — see graph._project_observer).
    (repo / "CLAUDE.md").write_text("# 项目约定\\n")
    repo = repo.resolve()

    model = CapturingModel(
        scripts=[
            # 1) touch the repo by absolute path → discovery records it
            script(tool_calls=[script_tool_call("bash", {"command": f"ls {repo}"})]),
            # 2) install into it
            script(
                tool_calls=[
                    script_tool_call(
                        "install_skills",
                        {"path": str(ponytail_repo / "skills"), "target": "repo",
                         "project_dir": str(repo)},
                    )
                ]
            ),
            script(text="装到仓库的 .claude/skills 了。"),
            script(text="不客气。"),  # a second user turn, for the context check
        ]
    )
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke(f"把 ponytail skill 装到 {repo} 里")
        evs = conv.recv_until("message.end", "error")
        assert not events_of(evs, "error")

        # same-turn signal: the tool result itself announces the discovery
        notes = [e["content"] for e in events_of(evs, "tool.end")]
        assert any("[project]" in c and str(repo) in c for c in notes), (
            "the repo must be announced in the same turn it was touched"
        )

        conv.invoke("谢谢")
        conv.recv_until("message.end", "error")

    # landed in the repo, NOT in Ginno's own storage
    assert (repo / ".claude" / "skills" / "ponytail" / "SKILL.md").exists()
    assert (repo / ".claude" / "skills" / "ponytail-audit" / "SKILL.md").exists()
    assert not (home / "skills" / "ponytail").exists()

    # from the next turn on, the discovered repo rides [turn context] — never
    # the stable system prompt (prefix-cache), and never the skills index
    turn_texts = [
        m.content if isinstance(m.content, str) else json.dumps(m.content, ensure_ascii=False)
        for call in model._seen
        for m in call
        if isinstance(m, HumanMessage)
    ]
    assert any("<projects>" in t and str(repo) in t for t in turn_texts), (
        "the discovered repo must appear in [turn context]"
    )
    for prompt in model.system_texts():
        # prefix-cache: the discovered path is volatile and must stay out of
        # the stable layer
        assert str(repo) not in prompt
        # write-only: a repo skill never enters Ginno's own index
        assert "ponytail" not in prompt


@pytest.fixture
def repo_outside_home(tmp_path):
    """A work dir for fake user repos, OUTSIDE GINNO_HOME.

    projects.py refuses to treat ~/.ginno as a project, so the repo-under-test
    cannot live under the isolated home. Moving GINNO_HOME instead would lose
    the settings the client fixture seeded into it: a bare home falls back to
    the PRODUCTION default (bypass_permissions=True, empty policy), so nothing
    ever prompts and both the permission boundary and read_file's allow-listing
    become invisible. A sibling of the isolated home keeps the seeded settings
    intact.
    """
    work = tmp_path.parent / f"work-{tmp_path.name}"
    work.mkdir(parents=True, exist_ok=True)
    return work


def test_repo_install_goes_through_the_permission_policy(
    create_session, ws_conv, repo_outside_home
):
    """Security boundary: skill tools are exempt from the permission policy
    because they only manage Ginno's own storage — but target="repo" writes
    into the USER'S repo, so it must prompt like any other write. Approval mode
    is where the difference is observable; production runs bypass=on, where
    nothing prompts either way (same as write_file today).

    Discovery is driven by read_file (in the default allow list) so the ONLY
    prompt in this turn is the install itself — bash would be "ask" too and
    would mask what is under test.
    """
    repo = repo_outside_home / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("# rules\n")
    repo = repo.resolve()
    src = repo_outside_home / "src" / "skills"
    _skill_dir(src, "pkg", "a skill")

    model = [
        script(tool_calls=[script_tool_call("read_file", {"path": str(repo / "CLAUDE.md")})]),
        script(
            tool_calls=[
                script_tool_call(
                    "install_skills",
                    {"path": str(src), "target": "repo", "project_dir": str(repo)},
                )
            ]
        ),
        script(text="ok"),
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("装进仓库")
        evs = conv.recv_until("permission.request", "message.end", "error")
        perm = events_of(evs, "permission.request")
        assert perm, "a write into the user's repo must be gated in approval mode"
        assert perm[0]["tool"] == "install_skills"

        conv.respond_permission("allow")
        rest = conv.recv_until("message.end", "error")
        assert not events_of(rest, "error")
        assert not events_of(rest, "permission.request")
    assert (repo / ".claude" / "skills" / "pkg" / "SKILL.md").exists()


def test_global_install_is_not_gated(create_session, ws_conv, repo_outside_home):
    """The contrast: Ginno's own storage is exempt, so no prompt — even in
    approval mode, where every other write would be gated."""
    src = repo_outside_home / "src" / "skills"
    _skill_dir(src, "pkg", "a skill")

    model = [
        script(tool_calls=[script_tool_call("install_skills", {"path": str(src)})]),
        script(text="ok"),
    ]
    sid = create_session(model, agent_id="dev")
    with ws_conv(sid) as conv:
        conv.invoke("装到全局")
        evs = conv.recv_until("message.end", "error")
        assert not events_of(evs, "permission.request")
        assert not events_of(evs, "error")
    assert (paths.global_skills_dir() / "pkg" / "SKILL.md").exists()
