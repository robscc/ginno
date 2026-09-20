"""Unit tests for the skill installer core + agent-side skill tools.

The installer is shared by POST /api/skills/import-dir and the
install_skills tool, so both surfaces get coverage here (the REST shape is
additionally pinned by tests/api/test_skills_import.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginno_runtime import paths, projects
from ginno_runtime.skills.installer import import_skills_from_dir, uninstall_skill
from ginno_runtime.tools.skill_tools import SKILL_TOOL_NAMES, build_skill_tools

pytestmark = pytest.mark.unit


def _skill_dir(root: Path, name: str, desc: str = "d", md_name: str = "SKILL.md") -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / md_name).write_text(
        f"---\nname: {name}\ndescription: {desc}\ntrigger: both\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return d


@pytest.fixture
def src(tmp_path):
    """A ponytail-shaped source tree: <root>/skills/<name>/SKILL.md."""
    root = tmp_path / "repo" / "skills"
    _skill_dir(root, "ponytail", "The lazy senior dev")
    _skill_dir(root, "ponytail-audit", "Repo-wide audit")
    (tmp_path / "repo" / "README.md").write_text("not a skill")
    return root


# --------------------------------------------------------------------------- #
# installer core
# --------------------------------------------------------------------------- #
def test_import_multiple_skills(src):
    r = import_skills_from_dir(str(src))
    assert r["ok"] is True and r["scanned"] == 2
    assert {x["name"] for x in r["imported"]} == {"ponytail", "ponytail-audit"}
    assert (paths.global_skills_dir() / "ponytail" / "SKILL.md").exists()


def test_import_single_skill_dir(src):
    one = src / "ponytail"
    r = import_skills_from_dir(str(one))
    assert {x["name"] for x in r["imported"]} == {"ponytail"}


def test_import_bad_path():
    assert import_skills_from_dir("")["ok"] is False
    assert import_skills_from_dir("/no/such/dir/xyz")["ok"] is False


def test_import_skip_then_overwrite(src):
    import_skills_from_dir(str(src))
    r2 = import_skills_from_dir(str(src))
    assert {x["name"] for x in r2["skipped"]} == {"ponytail", "ponytail-audit"}
    r3 = import_skills_from_dir(str(src), overwrite=True)
    assert {x["name"] for x in r3["imported"]} == {"ponytail", "ponytail-audit"}


def test_uninstall_skill(src):
    import_skills_from_dir(str(src))
    r = uninstall_skill("ponytail")
    assert r["ok"] is True and "global" in r["removed"]
    assert not (paths.global_skills_dir() / "ponytail").exists()
    # gone → error report, no exception
    assert uninstall_skill("ponytail")["ok"] is False


def test_uninstall_project_scoped_first(src, isolated_home):
    import_skills_from_dir(str(src))  # global copy
    proj = paths.project_skills_dir("default")
    _skill_dir(proj, "ponytail", "project override")
    r = uninstall_skill("ponytail", project_slug="default")
    assert r["ok"] is True and set(r["removed"]) == {"project", "global"}


# --------------------------------------------------------------------------- #
# agent-facing tools
# --------------------------------------------------------------------------- #
def test_skill_tool_names():
    assert SKILL_TOOL_NAMES == {
        "use_skill",
        "list_skills",
        "install_skills",
        "uninstall_skill",
    }


def test_use_skill_wraps_body_and_substitutes_arguments(src):
    import_skills_from_dir(str(src))
    # rewrite ponytail body to include $ARGUMENTS so we pin substitution
    p = paths.global_skills_dir() / "ponytail" / "SKILL.md"
    p.write_text(
        "---\nname: ponytail\ndescription: d\ntrigger: both\n---\n\n"
        "Run for $ARGUMENTS\n",
        encoding="utf-8",
    )
    tools = {t.name: t for t in build_skill_tools("default")}
    out = tools["use_skill"].invoke({"name": "ponytail", "request": "latest bill"})
    assert '<skill name="ponytail">' in out
    assert "Run for latest bill" in out
    assert "User request: latest bill" in out
    assert "Skill directory:" in out


def test_use_skill_unknown_and_user_only(isolated_home):
    d = isolated_home / "skills" / "user-only"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        "---\nname: user-only\ndescription: d\ntrigger: user-invocable\n---\n\nBody.\n",
        encoding="utf-8",
    )
    tools = {t.name: t for t in build_skill_tools("default")}
    assert tools["use_skill"].invoke({"name": "nope"}).startswith("[error] unknown")
    err = tools["use_skill"].invoke({"name": "user-only", "request": "x"})
    assert err.startswith("[error]") and "user-invocable only" in err


def test_tool_schemas_hide_project_slug():
    for t in build_skill_tools("default"):
        assert "project_slug" not in t.args, t.name


def test_install_list_uninstall_roundtrip(src):
    tools = {t.name: t for t in build_skill_tools("default")}

    report = json.loads(tools["install_skills"].invoke({"path": str(src)}))
    assert report["ok"] is True and len(report["imported"]) == 2

    listing = tools["list_skills"].invoke({})
    assert "ponytail [global]" in listing and "ponytail-audit [global]" in listing

    out = json.loads(tools["uninstall_skill"].invoke({"name": "ponytail"}))
    assert out["ok"] is True
    assert "ponytail " not in tools["list_skills"].invoke({})


def test_list_skills_project_scope_overrides(src, isolated_home):
    import_skills_from_dir(str(src))  # global ponytail
    _skill_dir(paths.project_skills_dir("default"), "ponytail", "project override")
    tools = {t.name: t for t in build_skill_tools("default")}
    listing = tools["list_skills"].invoke({})
    assert "ponytail [project]" in listing
    assert "ponytail [global]" not in listing  # project copy wins


def test_list_skills_empty():
    tools = {t.name: t for t in build_skill_tools("default")}
    assert tools["list_skills"].invoke({}) == "No skills installed."


def test_install_skills_reports_bad_path_as_json():
    tools = {t.name: t for t in build_skill_tools("default")}
    out = json.loads(tools["install_skills"].invoke({"path": "/no/such/dir"}))
    assert out["ok"] is False and "error" in out


# --------------------------------------------------------------------------- #
# install targets (global / project / repo) — the 2026-09-17 fix
# --------------------------------------------------------------------------- #
@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A Claude-style repo OUTSIDE Ginno's home, registered for the session."""
    home = tmp_path / "ginno-home"
    home.mkdir()
    monkeypatch.setenv("GINNO_HOME", str(home))
    r = tmp_path / "work" / "claude-agent-team"
    (r / ".git").mkdir(parents=True)
    r = r.resolve()
    projects.record("default", "sess-1", r)
    return r


def _tools(session_id="sess-1", primary_path=None):
    return {
        t.name: t
        for t in build_skill_tools("default", session_id, primary_path)
    }


def test_install_project_target(src, repo):
    out = json.loads(
        _tools()["install_skills"].invoke({"path": str(src), "target": "project"})
    )
    assert out["ok"] is True and len(out["imported"]) == 2
    assert (paths.project_skills_dir("default") / "ponytail" / "SKILL.md").exists()
    # …and NOT in the global dir — that is the whole point of the parameter.
    assert not (paths.global_skills_dir() / "ponytail").exists()


def test_install_repo_target_creates_the_claude_dir(src, repo):
    out = json.loads(
        _tools()["install_skills"].invoke(
            {"path": str(src), "target": "repo", "project_dir": str(repo)}
        )
    )
    assert out["ok"] is True and len(out["imported"]) == 2
    assert (repo / ".claude" / "skills" / "ponytail" / "SKILL.md").exists()
    assert not (paths.global_skills_dir() / "ponytail").exists()


def test_install_repo_target_refuses_an_unregistered_dir(src, repo, tmp_path):
    """A plausible-but-wrong guess is how an unlogged write happens."""
    stranger = tmp_path / "work" / "some-other-repo"
    stranger.mkdir()
    out = json.loads(
        _tools()["install_skills"].invoke(
            {"path": str(src), "target": "repo", "project_dir": str(stranger)}
        )
    )
    assert out["ok"] is False
    assert str(repo) in out["error"]  # names the candidates
    assert not (stranger / ".claude").exists()


def test_install_repo_target_needs_an_absolute_dir(src, repo):
    out = json.loads(
        _tools()["install_skills"].invoke(
            {"path": str(src), "target": "repo", "project_dir": "./relative"}
        )
    )
    assert out["ok"] is False and "绝对路径" in out["error"]


def test_install_repo_target_without_project_dir(src, repo):
    out = json.loads(
        _tools()["install_skills"].invoke({"path": str(src), "target": "repo"})
    )
    assert out["ok"] is False and "project_dir" in out["error"]


def test_install_repo_target_accepts_a_primary_mount(src, tmp_path, monkeypatch):
    """An explicitly mounted ★primary dir is a legitimate target too."""
    home = tmp_path / "ginno-home"
    home.mkdir()
    monkeypatch.setenv("GINNO_HOME", str(home))
    mounted = tmp_path / "mounted"
    mounted.mkdir()
    mounted = mounted.resolve()
    tools = _tools(session_id="", primary_path=str(mounted))
    out = json.loads(
        tools["install_skills"].invoke(
            {"path": str(src), "target": "repo", "project_dir": str(mounted)}
        )
    )
    assert out["ok"] is True
    assert (mounted / ".claude" / "skills" / "ponytail" / "SKILL.md").exists()


def test_install_defaults_to_global_for_backward_compat(src, repo):
    out = json.loads(_tools()["install_skills"].invoke({"path": str(src)}))
    assert out["ok"] is True
    assert (paths.global_skills_dir() / "ponytail" / "SKILL.md").exists()


def test_list_skills_labels_the_repo_scope(src, repo):
    _tools()["install_skills"].invoke(
        {"path": str(src), "target": "repo", "project_dir": str(repo)}
    )
    listing = _tools()["list_skills"].invoke({})
    assert "[claude:claude-agent-team]" in listing
    # Repo skills belong to another agent — Ginno must not offer them for use.
    assert "[global]" not in listing


def test_repo_skills_are_not_loadable(src, repo):
    """Write-only by design: installing into a repo must not put that skill
    into Ginno's own index (any repo the agent touches could otherwise inject
    instructions into every prompt)."""
    _tools()["install_skills"].invoke(
        {"path": str(src), "target": "repo", "project_dir": str(repo)}
    )
    from ginno_runtime.skills.loader import SkillLoader

    assert SkillLoader(project_slug="default").get("ponytail") is None
    assert _tools()["use_skill"].invoke({"name": "ponytail"}).startswith("[error]")


def test_uninstall_repo_scope(src, repo):
    tools = _tools()
    tools["install_skills"].invoke(
        {"path": str(src), "target": "repo", "project_dir": str(repo)}
    )
    out = json.loads(
        tools["uninstall_skill"].invoke(
            {"name": "ponytail", "scope": "repo", "project_dir": str(repo)}
        )
    )
    assert out["ok"] is True and out["removed"] == ["repo"]
    assert not (repo / ".claude" / "skills" / "ponytail").exists()


def test_uninstall_repo_scope_is_allowlisted(src, repo, tmp_path):
    stranger = tmp_path / "work" / "other"
    stranger.mkdir()
    out = json.loads(
        _tools()["uninstall_skill"].invoke(
            {"name": "ponytail", "scope": "repo", "project_dir": str(stranger)}
        )
    )
    assert out["ok"] is False and "error" in out


def test_uninstall_global_scope_leaves_the_project_copy(src, isolated_home):
    import_skills_from_dir(str(src))  # global
    _skill_dir(paths.project_skills_dir("default"), "ponytail", "project override")
    out = json.loads(
        _tools()["uninstall_skill"].invoke({"name": "ponytail", "scope": "global"})
    )
    assert out["removed"] == ["global"]
    assert (paths.project_skills_dir("default") / "ponytail").exists()
