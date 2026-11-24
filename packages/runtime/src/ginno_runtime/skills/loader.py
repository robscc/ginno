"""SKILL.md loader.

Skill format (Claude-Code-inspired):

    <package>/skills/builtin/<name>/SKILL.md         (built-in, ships with Ginno)
    ~/.ginno/skills/<name>/SKILL.md                  (global, user-installed)
    ~/.ginno/projects/<slug>/skills/<name>/SKILL.md  (project-scoped override)

Precedence on name conflict: builtin < global < project < session (a user copy
overrides a built-in without breaking it; built-ins can't be deleted).
A copy that omits `tools:`/`sticky:` inherits those bindings from the
built-in it shadows, so an old copy can't orphan a lazy tool family.

    ---
    name: summarize-notes
    description: Summarize a set of Obsidian notes into a brief
    trigger: user-invocable       # or model-invocable | both
    tools: [read, write]
    ---

    # Skill body — instructions injected when triggered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .. import paths

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class Skill:
    name: str
    description: str
    trigger: str = "both"  # user-invocable | model-invocable | both
    allowed_tools: list[str] = field(default_factory=list)
    # Declares this skill as the sync adapter for an external TODO platform
    # (todo-provider design); makes it show up in todo provider discovery.
    todo_provider: str = ""
    # Lazy toolset activation (browser-skill design): the skill's `tools:`
    # family binds only through skill activation, and ONE use_skill keeps it
    # bound for the REST OF THE SESSION (the sticky_skills state channel —
    # unlike active_skills, which the WS layer resets every turn).
    sticky: bool = False
    body: str = ""
    path: Path | None = None
    builtin: bool = False  # shipped with Ginno; not deletable via the API

    def system_prompt_snippet(self) -> str:
        """One-line summary for injection into system prompt index."""
        return f"- {self.name}: {self.description}"

    def model_invocable(self) -> bool:
        return self.trigger in ("model-invocable", "both")

    def user_invocable(self) -> bool:
        return self.trigger in ("user-invocable", "both")

    def effective_tools(self) -> list[str]:
        """Tools this skill is allowed to use once activated.

        Frontmatter ``tools:`` wins. Script-backed skills that omit it still
        need a shell to run the companion ``*.py``, so default to ``bash``.
        """
        if self.allowed_tools:
            return list(self.allowed_tools)
        return ["bash"]


def wrap_skill_body(skill: Skill, request: str = "") -> str:
    """Wrap a SKILL.md body the same way slash substitution and ``use_skill`` do.

    ``$ARGUMENTS`` in the body is replaced with the request (empty when none),
    matching Claude-Code-style skill templates. The skill directory is
    appended so script-backed skills know where to run from.
    """
    body = (skill.body or "").strip()
    body = body.replace("$ARGUMENTS", request or "")
    blocks = [
        f'<skill name="{skill.name}">',
        body,
        "</skill>",
    ]
    if skill.path is not None:
        blocks.append(f"\nSkill directory: {skill.path.parent}")
    if request:
        blocks.append(f"\n\nUser request: {request}")
    else:
        blocks.append("\n\n(Follow the skill instructions above.)")
    return "\n".join(blocks)


def _parse_skill_file(p: Path, builtin: bool = False, *, include_body: bool = True) -> Skill | None:
    if include_body:
        raw = p.read_text(encoding="utf-8")
    else:
        # Index discovery reads frontmatter only, never the instruction body.
        with p.open(encoding="utf-8") as stream:
            first = stream.readline()
            if first.strip() != "---":
                return None
            lines = [first]
            for line in stream:
                lines.append(line)
                if line.strip() == "---":
                    break
            raw = "".join(lines)
    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None
    meta = yaml.safe_load(m.group(1)) or {}
    body = m.group(2).strip()
    return Skill(
        name=meta.get("name", p.parent.name),
        description=meta.get("description", ""),
        trigger=meta.get("trigger", "both"),
        allowed_tools=meta.get("tools", []) or [],
        todo_provider=str(meta.get("todo_provider") or ""),
        sticky=bool(meta.get("sticky", False)),
        body=body,
        path=p,
        builtin=builtin,
    )


def _scan_dir(root: Path, builtin: bool, into: dict[str, Skill], *, include_body: bool = True) -> None:
    if not root.exists():
        return
    for p in root.glob("*/SKILL.md"):
        s = _parse_skill_file(p, builtin=builtin, include_body=include_body)
        if not s:
            continue
        prev = into.get(s.name)
        if prev is not None and prev.builtin and not s.builtin:
            # A user copy overrides a built-in for the *instructions* — it must
            # not silently drop the built-in's tool bindings: the lazy families
            # (graph._LAZY_TOOL_PREFIXES) bind only through a `sticky: true`
            # skill's `tools:` declaration, and an old copy predating those
            # frontmatter keys would orphan the whole family (a user-installed
            # Obsidian-style `todo` did exactly that). The binding is inherited
            # as a WHOLE: a copy that omits `tools:` takes the built-in's
            # tools+sticky; one that declares its own `tools:` owns the entire
            # binding (no inherited stickiness for a self-declared toolset).
            if not s.allowed_tools:
                s.allowed_tools = list(prev.allowed_tools)
                if not s.sticky:
                    s.sticky = prev.sticky
        into[s.name] = s


def load_all_skills(project_slug: str | None = None, *, include_body: bool = True) -> list[Skill]:
    """Load builtin + global + project skills. Later tiers win on conflict
    (builtin < global < project), so a user copy overrides a built-in —
    except that undeclared ``tools:``/``sticky:`` bindings are inherited
    from the built-in (see _scan_dir)."""
    skills: dict[str, Skill] = {}

    _scan_dir(paths.builtin_skills_dir(), builtin=True, into=skills, include_body=include_body)
    _scan_dir(paths.global_skills_dir(), builtin=False, into=skills, include_body=include_body)
    if project_slug:
        _scan_dir(paths.project_skills_dir(project_slug), builtin=False, into=skills, include_body=include_body)

    return list(skills.values())


def session_access_roots(context: dict) -> tuple[str, ...]:
    """Existing session grants only; skill_paths never grant filesystem access."""
    roots = [d["path"] for d in context.get("context_dirs", [])
             if d.get("path") and not d.get("missing")]
    if context.get("workspace"):
        roots.append(context["workspace"])
    return tuple(roots)


def validate_skill_paths(values: list[str], access_roots: tuple[str, ...] = ()) -> list[str]:
    """Canonicalize explicit directories using the file tools' permission fence."""
    from ..tools.builtin import _DenyPolicy

    if len(values) > 20:
        raise ValueError("skill_paths accepts at most 20 directories")
    result = []
    for raw in values:
        p = Path(raw).expanduser()
        if not raw.strip() or not p.is_absolute():
            raise ValueError("skill_paths entries must be absolute directories")
        try:
            p = p.resolve(strict=True)
            if not p.is_dir():
                raise ValueError("skill_paths entries must be directories")
            if _DenyPolicy(None, [Path(r) for r in access_roots]).denied(p):
                raise ValueError("skill_paths entry is protected")
        except (OSError, RuntimeError) as exc:
            raise ValueError("skill_paths entry cannot be resolved") from exc
        if str(p) not in result:
            result.append(str(p))
    return result


@dataclass
class SkillLoader:
    project_slug: str | None = None
    extra_dirs: tuple[str, ...] = ()
    access_roots: tuple[str, ...] = ()

    def load(self, *, include_body: bool = True) -> list[Skill]:
        from ..tools.builtin import _DenyPolicy

        skills = {s.name: s for s in load_all_skills(self.project_slug, include_body=include_body)}
        builtins = {}
        _scan_dir(paths.builtin_skills_dir(), builtin=True, into=builtins, include_body=False)
        for raw in self.extra_dirs:
            try:
                root = Path(validate_skill_paths([raw], self.access_roots)[0])
                candidates = [root / "SKILL.md"] if (root / "SKILL.md").exists() else sorted(root.glob("*/SKILL.md"))
                for candidate in candidates:
                    real = candidate.resolve(strict=True)
                    # Never follow a package's link outside the explicitly selected tree.
                    if not real.is_relative_to(root) or _DenyPolicy(None, [Path(r) for r in self.access_roots]).denied(real):
                        continue
                    skill = _parse_skill_file(real, include_body=include_body)
                    if skill is None:
                        continue
                    builtin = builtins.get(skill.name)
                    if builtin is not None and not skill.allowed_tools:
                        skill.allowed_tools = list(builtin.allowed_tools)
                        skill.sticky = skill.sticky or builtin.sticky
                    skills[skill.name] = skill
            except (OSError, RuntimeError, ValueError, yaml.YAMLError):
                # Revalidate on every index/use, including after restore or link changes.
                continue
        return list(skills.values())

    def build_index_prompt(self) -> str:
        skills = self.load(include_body=False)
        if not skills:
            return ""
        lines = [
            "Available skills. Call use_skill(name, request) when a skill matches"
            " the user's intent — do not ask the user to type /<name> yourself."
            " The user can also invoke one by starting a message with /<name>:"
        ]
        lines += [s.system_prompt_snippet() for s in skills]
        return "\n".join(lines)

    def get(self, name: str) -> Skill | None:
        skill = next((s for s in self.load(include_body=False) if s.name == name), None)
        if skill is not None and skill.path is not None:
            try:
                parsed = _parse_skill_file(skill.path, builtin=skill.builtin)
            except FileNotFoundError:
                # A skill can disappear between indexing and reading its body.
                return None
            if parsed is None:
                return None
            skill.body = parsed.body
        return skill
