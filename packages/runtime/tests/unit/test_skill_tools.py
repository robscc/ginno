"""Unit tests for the skill installer core + agent-side skill tools.

The installer is shared by POST /api/skills/import-dir and the
install_skills tool, so both surfaces get coverage here (the REST shape is
additionally pinned by tests/api/test_skills_import.py).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ginno_runtime import paths
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
    assert SKILL_TOOL_NAMES == {"list_skills", "install_skills", "uninstall_skill"}


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
