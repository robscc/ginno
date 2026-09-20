"""Skill tools: use_skill / list_skills / install_skills / uninstall_skill.

``use_skill`` is how the model auto-invokes a skill (the user can still
slash-invoke via ``/<name>``). The other three manage Ginno-owned storage
(~/.ginno/skills and the project skills dir) so the permission node treats
them like the todo/workflow tools — never prompts (writes to the user's
own files/shell stay gated by the regular policy via bash/write_file).

``install_skills`` can additionally target a REPO's ``.claude/skills`` — the
fix for the 2026-09-17 incident, where "导入 skill" was ambiguous between
Ginno's dirs and the repo the session had been scaffolding, and the model
silently picked Ginno. That target writes into the user's repo, so it is
allowlisted to project roots this session actually discovered or mounted
(``projects.py``), and it is NOT exempt from the permission policy the way the
Ginno-owned targets are (see ``graph.py``).

Like the builtin tools they are built per session so listing knows the
session's project scope; the model never sees a project_slug parameter.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from langchain_core.tools import tool

from .. import paths
from .. import projects as projects_mod
from ..skills.installer import import_skills_from_dir
from ..skills.installer import uninstall_skill as _uninstall
from ..skills.loader import SkillLoader, _parse_skill_file, wrap_skill_body

SKILL_TOOL_NAMES = {"use_skill", "list_skills", "install_skills", "uninstall_skill"}


def build_skill_tools(
    project_slug: str | None = None,
    session_id: str | None = None,
    primary_path: str | None = None,
) -> list:
    slug = project_slug or ""

    def _known_repo_roots() -> list[str]:
        """Project roots this session may install into.

        Auto-discovered repos first (the incident's case: a repo the agent
        only ever touched by absolute path), then an explicitly mounted
        ★primary dir.
        """
        roots: list[str] = []
        if slug and session_id:
            roots = [p["path"] for p in projects_mod.known(slug, session_id)]
        pp = (primary_path or "").strip()
        if pp and pp not in roots:
            roots.insert(0, pp)
        return roots

    def _resolve_repo_dir(raw: str) -> tuple[Path | None, str]:
        """→ (``<repo>/.claude/skills``, error string). Strict allowlist.

        This tool is in the never-prompt class for its Ginno-owned targets;
        letting it write to an arbitrary ``.claude/skills`` anywhere on disk
        would be a capability escalation. A plausible-but-wrong guess is
        exactly how an unlogged write happens, so refuse and name the
        candidates instead.
        """
        val = (raw or "").strip()
        if not val:
            return None, "target=repo 需要 project_dir（仓库根目录的绝对路径）"
        p = Path(val).expanduser()
        if not p.is_absolute():
            return None, f"project_dir 必须是绝对路径：{val}"
        try:
            p = p.resolve()
        except OSError:
            return None, f"project_dir 无法解析：{val}"
        allowed = _known_repo_roots()
        if str(p) not in allowed:
            cand = "；可选：" + "，".join(allowed) if allowed else "（本会话尚未识别出项目目录）"
            return None, (
                f"{p} 不在本会话已知的项目目录内{cand}。"
                "不要臆造路径 —— 若用户想装到别处，先用 ask_user 与他确认。"
            )
        return projects_mod.claude_skills_dir(p), ""

    @tool
    def use_skill(name: str, request: str = "") -> str:
        """Load a skill's instructions and follow them for this request.

        Call this when an available skill matches the user's intent (billing,
        platform ops, research playbooks, …) instead of asking the user to
        type /<name>. ``name`` is the skill id from the skills index;
        ``request`` is the user's concrete ask (passed through as the skill
        argument). Returns the skill body, or an error string.
        """
        skill = SkillLoader(project_slug=slug or None).get(name)
        if not skill:
            return f"[error] unknown skill: {name}"
        if not skill.model_invocable():
            return (
                f"[error] skill {name!r} is user-invocable only "
                f"(the user must type /{name} themselves)."
            )
        if not skill.body:
            return f"[error] skill {name!r} has an empty body."
        return wrap_skill_body(skill, (request or "").strip())

    @tool
    def list_skills() -> str:
        """List installed skills (global + project-scoped) with their location.

        Each line: ``- <name> [<scope>] <description> (<dir>)``. Use this to
        check what is already installed before installing or uninstalling.
        ``[claude:<repo>]`` entries are repo ``.claude/skills`` trees that
        belong to OTHER agents — Ginno can list and install there, but never
        loads them into its own skill index.
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

        # Repo copies first — farthest from Ginno's own storage.
        for root in _known_repo_roots():
            _scan(projects_mod.claude_skills_dir(root), f"claude:{Path(root).name}")
        # Project-scoped next: on a name conflict it overrides the global one.
        if slug:
            _scan(paths.project_skills_dir(slug), "project")
        _scan(paths.global_skills_dir(), "global")
        if not lines:
            return "No skills installed."
        return "\n".join(lines)

    @tool
    def install_skills(
        path: str,
        target: Literal["global", "project", "repo"] = "global",
        project_dir: str = "",
        overwrite: bool = False,
    ) -> str:
        """Install skill(s) from a local directory into one of three places.

        ``target``:
        * ``"global"`` (default) → ``~/.ginno/skills`` — every session sees it.
        * ``"project"`` → ``~/.ginno/projects/<this project>/skills`` — this
          project only; a same-named global skill is overridden by it.
        * ``"repo"`` → ``<project_dir>/.claude/skills`` — for OTHER agents
          (Claude Code etc.) that read that repo. Requires ``project_dir``:
          the absolute path of a repo root this session has seen (listed in
          the conversation's project context). Ginno itself does NOT load
          skills from there.

        If the user did not say which place they want and more than one is
        plausible, call ask_user FIRST instead of guessing.

        ``path`` may be a directory containing one or more ``<skill>/SKILL.md``
        sub-directories (the usual case after cloning a skill collection repo),
        or a single skill directory containing ``SKILL.md`` itself. The whole
        skill directory (scripts, reference files, ...) is copied, so
        script-backed skills keep working. Existing skills are skipped unless
        ``overwrite`` is true. Returns a JSON report {ok, scanned, imported,
        skipped, errors}.
        """
        if target == "project":
            if not slug:
                return json.dumps(
                    {"ok": False, "error": "target=project 需要会话的 project_slug"},
                    ensure_ascii=False,
                )
            dest = paths.project_skills_dir(slug)
        elif target == "repo":
            dest, err = _resolve_repo_dir(project_dir)
            if dest is None:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
        else:
            dest = paths.global_skills_dir()
        report = import_skills_from_dir(path, overwrite=overwrite, dest_root=dest)
        return json.dumps(report, ensure_ascii=False)

    @tool
    def uninstall_skill(
        name: str,
        scope: Literal["auto", "global", "project", "repo"] = "auto",
        project_dir: str = "",
    ) -> str:
        """Uninstall a skill by name.

        ``scope="auto"`` (default) removes the project-scoped copy first, then
        the global one. ``scope="repo"`` removes ``<project_dir>/.claude/skills
        /<name>`` and requires the same allowlisted ``project_dir`` as
        install_skills(target="repo"). Returns a JSON report
        {ok, removed: ["repo"|"project"|"global", ...]} or {ok: false, error}.
        Use list_skills() to discover exact names.
        """
        repo_dir: str | None = None
        if scope == "repo":
            resolved, err = _resolve_repo_dir(project_dir)
            if resolved is None:
                return json.dumps({"ok": False, "error": err}, ensure_ascii=False)
            repo_dir = str(resolved.parent.parent)
        return json.dumps(
            _uninstall(
                name,
                project_slug=slug or None,
                repo_dir=repo_dir,
                scope=scope,
            ),
            ensure_ascii=False,
        )

    return [use_skill, list_skills, install_skills, uninstall_skill]