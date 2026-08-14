"""Unit tests for SKILL.md loading, frontmatter parsing, and project overrides."""

from __future__ import annotations

import pytest

from ginno_runtime import paths
from ginno_runtime.skills.loader import SkillLoader, load_all_skills

pytestmark = pytest.mark.unit

_SKILL = """---
name: summarize-notes
description: Summarize a set of notes into a brief
trigger: user-invocable
tools: [read_file, write_file]
---
# Skill body
Read the notes and write a one-paragraph summary.
"""


def _write_skill(base, name, body=_SKILL):
    d = base / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def _user_skills() -> list:
    """Skills excluding the builtin tier (shipped with the runtime)."""
    return [s for s in load_all_skills() if not s.builtin]


def test_parse_frontmatter_and_body(isolated_home):
    _write_skill(paths.global_skills_dir(), "summarize-notes")
    skills = _user_skills()
    assert len(skills) == 1
    s = skills[0]
    assert s.name == "summarize-notes"
    assert s.description == "Summarize a set of notes into a brief"
    assert s.trigger == "user-invocable"
    assert s.allowed_tools == ["read_file", "write_file"]
    assert "one-paragraph summary" in s.body


def test_skill_without_frontmatter_is_skipped(isolated_home):
    d = paths.global_skills_dir() / "broken"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("no frontmatter here\n", encoding="utf-8")
    assert _user_skills() == []
    assert all(s.name != "broken" for s in load_all_skills())


def test_project_skill_overrides_global(isolated_home):
    _write_skill(paths.global_skills_dir(), "summarize-notes")
    override = (
        "---\nname: summarize-notes\ndescription: PROJECT OVERRIDE\n---\nproject body\n"
    )
    _write_skill(paths.project_skills_dir("proj-a"), "summarize-notes", override)
    s = SkillLoader(project_slug="proj-a").get("summarize-notes")
    assert s.description == "PROJECT OVERRIDE"
    assert "project body" in s.body


def test_build_index_prompt_lists_skills(isolated_home):
    _write_skill(paths.global_skills_dir(), "summarize-notes")
    prompt = SkillLoader().build_index_prompt()
    assert "summarize-notes" in prompt
    assert "Summarize a set of notes" in prompt


def test_build_index_prompt_lists_only_builtin_when_no_user_skills(isolated_home):
    # No user-installed skills, but the builtin tier always ships with Ginno.
    prompt = SkillLoader().build_index_prompt()
    assert "todo" in prompt  # builtin TODO skill is always present


def test_get_unknown_returns_none(isolated_home):
    assert SkillLoader().get("nope") is None


# ------------------------------ builtin tier ------------------------------ #
def test_builtin_todo_skill_always_present(isolated_home):
    s = SkillLoader().get("todo")
    assert s is not None and s.builtin is True
    assert "todo_list" in (s.allowed_tools or []) or "todo" in s.body


def test_builtin_browse_skill_always_present(isolated_home):
    s = SkillLoader().get("browse")
    assert s is not None and s.builtin is True
    assert "browser_eval" in (s.allowed_tools or [])
    assert "browser_screenshot" in (s.allowed_tools or [])
    assert "handOffTaskSpace" in s.body
    assert "about:blank" in s.body
    assert "takeOverTaskSpace" in s.body
    assert "mcp_playwright" in s.body


def test_global_copy_overrides_builtin(isolated_home):
    _write_skill(
        paths.global_skills_dir(),
        "todo",
        "---\nname: todo\ndescription: CUSTOM\ntrigger: both\n---\ncustom body\n",
    )
    s = SkillLoader().get("todo")
    assert s.builtin is False and s.description == "CUSTOM"
    assert s.body == "custom body"


def test_builtin_skill_dir_resolves(isolated_home):
    d = paths.builtin_skills_dir()
    assert (d / "todo" / "SKILL.md").exists()
    assert (d / "browse" / "SKILL.md").exists()
