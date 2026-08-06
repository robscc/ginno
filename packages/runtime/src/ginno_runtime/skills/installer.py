"""Skill install/uninstall core — shared by the REST API and agent tools.

Extracted from the old ``POST /api/skills/import-dir`` handler (2026-08) so
the agent-side ``install_skills`` tool and the UI endpoint share ONE
implementation. Semantics are unchanged:

* Each sub-directory containing a ``SKILL.md`` (case-insensitive) is one
  skill; the whole sub-directory (scripts, reference docs, ...) is copied so
  script-backed skills keep working. A path that is itself a single skill
  directory imports just that one.
* Existing skills are skipped unless ``overwrite`` is true.
* The loader expects ``SKILL.md`` (case-sensitive glob), so a lowercase
  ``skill.md`` is force-renamed on case-insensitive filesystems.
"""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path

from .. import paths
from .loader import _parse_skill_file


def find_skill_md(d: Path) -> Path | None:
    """The SKILL.md (any case) directly inside ``d``, if any."""
    try:
        for f in d.iterdir():
            if f.is_file() and f.name.lower() == "skill.md":
                return f
    except OSError:
        return None
    return None


def sanitize_name(n: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]", "-", (n or "").strip()).strip("-") or "skill"


def import_skills_from_dir(
    raw: str,
    overwrite: bool = False,
    dest_root: Path | None = None,
) -> dict:
    """Import skill(s) from a local directory into ``dest_root`` (default:
    the global skills dir). Returns the report shape the REST endpoint has
    always returned: ``{ok, scanned, imported, skipped, errors}`` (or
    ``{ok: False, error}`` for a bad path)."""
    if not raw:
        return {"ok": False, "error": "path required"}
    src = Path(raw).expanduser().resolve()
    if not src.is_dir():
        return {"ok": False, "error": f"not a directory: {raw}"}

    candidates = [src] if find_skill_md(src) else sorted(
        c for c in src.iterdir()
        if c.is_dir() and not c.name.startswith(".") and find_skill_md(c)
    )

    imported: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []
    dest_root = dest_root or paths.global_skills_dir()
    dest_root.mkdir(parents=True, exist_ok=True)

    for c in candidates:
        smd = find_skill_md(c)
        if not smd:
            continue
        parsed = _parse_skill_file(smd)
        name = sanitize_name(parsed.name if parsed and parsed.name else c.name)
        target = dest_root / name
        if target.exists() and not overwrite:
            skipped.append({"name": name, "reason": "exists"})
            continue
        try:
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(c, target, ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))
            # Ginno's loader expects SKILL.md (its glob is case-sensitive), but the
            # source may use lowercase skill.md. On case-insensitive filesystems
            # (macOS APFS) a direct rename is a no-op, so go via a temp name to
            # force the case change; detect the real on-disk name via iterdir.
            actual = next(
                (f for f in target.iterdir() if f.is_file() and f.name.lower() == "skill.md"),
                None,
            )
            if actual is not None and actual.name != "SKILL.md":
                tmp = target / f".skill_md_rename_{uuid.uuid4().hex}"
                actual.rename(tmp)
                tmp.rename(target / "SKILL.md")
            imported.append({
                "name": name,
                "description": (parsed.description if parsed else "") or "",
                "from": str(c),
            })
        except Exception as e:  # noqa: BLE001
            errors.append({"name": c.name, "error": f"{type(e).__name__}: {e}"})

    return {
        "ok": True,
        "scanned": len(candidates),
        "imported": imported,
        "skipped": skipped,
        "errors": errors,
    }


def uninstall_skill(name: str, project_slug: str | None = None) -> dict:
    """Remove an installed skill by name. The project-scoped copy goes first
    (it is the effective one), then the global copy. ``{ok, removed: [scope]}``.

    Built-in skills (shipped with the runtime) are never removable — if the
    only copy on disk is a built-in, report it as not found."""
    proj = paths.project_skills_dir(project_slug) / name if project_slug else None
    glob = paths.global_skills_dir() / name
    removed: list[str] = []
    if proj is not None and proj.is_dir():
        shutil.rmtree(proj)
        removed.append("project")
    if glob.is_dir():
        shutil.rmtree(glob)
        removed.append("global")
    if not removed:
        from .loader import SkillLoader

        s = SkillLoader(project_slug=project_slug).get(name)
        if s and s.builtin:
            return {"ok": False, "error": f"builtin skill cannot be uninstalled: {name}"}
        return {"ok": False, "error": f"skill not found: {name}"}
    return {"ok": True, "removed": removed}
