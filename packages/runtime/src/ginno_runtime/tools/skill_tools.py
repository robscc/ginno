"""Skill management tools: list_skills / install_skills / uninstall_skill.

Before these existed (2026-08), "install this skill" requests had no
first-class path: the agent improvised with bash/glob and — knowing neither
the skills directories nor any install mechanism — invented paths and
crashed. These tools give the model a deterministic install path that the
system prompt's skills section can point at.

They only touch Ginno-managed storage (~/.ginno/skills and the project
skills dir), so the permission node treats them like the todo/workflow
tools — never prompts (writes to the user's own files/shell stay gated by
the regular policy via bash/write_file).

Like the builtin tools they are built per session
(``build_skill_tools(project_slug)``) so listing knows the session's project
scope; the model never sees a project_slug parameter.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from .. import paths
from ..skills.installer import import_skills_from_dir
from ..skills.installer import uninstall_skill as _uninstall
from ..skills.loader import _parse_skill_file

SKILL_TOOL_NAMES = {"list_skills", "install_skills", "uninstall_skill"}


def build_skill_tools(project_slug: str | None = None) -> list:
    slug = project_slug or ""

    @tool
    def list_skills() -> str:
        """List installed skills (global + project-scoped) with their location.

        Each line: ``- <name> [<scope>] <description> (<dir>)``. Use this to
        check what is already installed before installing or uninstalling.
        """
        lines: list[str] = []
        seen: set[str] = set()

        def _scan(root, scope: str) -> None:
            if not root.exists():
                return
            for p in sorted(root.glob("*/SKILL.md")):
                s = _parse_skill_file(p)
                name = s.name if s else p.parent.name
                if name in seen:
                    continue
                seen.add(name)
                desc = (s.description if s else "") or ""
                lines.append(f"- {name} [{scope}] {desc} ({p.parent})".rstrip())

        # Project-scoped first: on a name conflict it overrides the global one.
        if slug:
            _scan(paths.project_skills_dir(slug), "project")
        _scan(paths.global_skills_dir(), "global")
        if not lines:
            return "No skills installed."
        return "\n".join(lines)

    @tool
    def install_skills(path: str, overwrite: bool = False) -> str:
        """Install skill(s) from a local directory into the global skills dir
        (~/.ginno/skills).

        ``path`` may be:
        * a directory containing one or more ``<skill>/SKILL.md``
          sub-directories (each becomes one installed skill — the usual case
          after cloning a skill collection repo), or
        * a single skill directory containing ``SKILL.md`` itself.

        The whole skill directory (scripts, reference files, ...) is copied,
        so script-backed skills keep working. Existing skills are skipped
        unless ``overwrite`` is true. To get a remote skill first, fetch it
        locally (e.g. ``git clone <repo-url>`` via bash), then call this on
        the clone. Returns a JSON report {ok, scanned, imported, skipped,
        errors}.
        """
        report = import_skills_from_dir(path, overwrite=overwrite)
        return json.dumps(report, ensure_ascii=False)

    @tool
    def uninstall_skill(name: str) -> str:
        """Uninstall a skill by name (project-scoped copy first, then global).

        Returns a JSON report {ok, removed: ["project"|"global", ...]} or
        {ok: false, error}. Use list_skills() to discover exact names.
        """
        return json.dumps(_uninstall(name, project_slug=slug or None), ensure_ascii=False)

    return [list_skills, install_skills, uninstall_skill]
