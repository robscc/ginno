"""Best-effort startup migration of session files into per-session dirs.

Historically uploads lived in a shared workspace (`<workspace>/uploads/<sid>/`,
often `/tmp/gw/...`) and analysis results sat next to the source file
(`<source>/results/...`). The session-scoped layout moves both under
`projects/<slug>/sessions/<sid>/{uploads,results}/`.

Run once at startup (lifespan, before `yield`), BEFORE any upload/preview/
watcher can race it. Idempotent and non-fatal: an entry already inside its
session dir is skipped; a file that no longer exists (tmp cleared) is marked
stale and left in place; any per-file error is logged and skipped. Returns a
stats dict; never raises.
"""

from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path

from . import artifacts as art_store
from . import paths
from .files import get_registry, norm_path, unique_dest

log = logging.getLogger(__name__)


def _inside_session_dir(norm_file: str, slug: str, session_id: str) -> bool:
    base = norm_path(paths.session_files_dir(slug, session_id))
    try:
        return Path(norm_file).is_relative_to(base)
    except ValueError:
        return False


def _legacy_subdir(src: Path, session_id: str) -> str | None:
    # A registry entry associates a file with a session for preview. It does
    # not imply that Ginno owns the file. Match the layouts of the old writers.
    if (
        src.parent.name == session_id
        and src.parent.parent.name == "uploads"
        and re.fullmatch(r"[0-9a-f]{8}-.+", src.name, flags=re.DOTALL)
    ):
        return "uploads"
    if src.parent.name == "results" and re.fullmatch(
        r".+-result-[0-9a-f]{8}\.csv", src.name, flags=re.DOTALL,
    ):
        return "results"
    return None


def migrate_session_files() -> dict:
    """Move legacy managed uploads and analysis results into their session dirs.

    Returns ``{"scanned", "moved", "missing", "skipped", "errors"}``.
    """
    stats = {"scanned": 0, "moved": 0, "missing": 0, "skipped": 0, "errors": 0}
    projects_root = paths.home() / "projects"
    if not projects_root.is_dir():
        return stats

    for proj in sorted(projects_root.iterdir()):
        if not proj.is_dir():
            continue
        slug = proj.name
        if not (proj / "files.json").exists():
            continue
        try:
            reg = get_registry(slug)
        except Exception:
            stats["errors"] += 1
            continue

        for e in reg.list_all():
            stats["scanned"] += 1
            session_id = e.get("session_id") or ""
            old_path = e.get("path") or ""
            if not session_id or not old_path:
                stats["skipped"] += 1  # unattributable — leave as-is
                continue
            old_norm = norm_path(old_path)
            if _inside_session_dir(old_norm, slug, session_id):
                stats["skipped"] += 1  # already migrated (idempotent)
                continue
            src = Path(old_norm)
            subdir = _legacy_subdir(src, session_id)
            if subdir is None:
                stats["skipped"] += 1  # external reference, not a managed legacy file
                continue
            if not src.is_file():
                # tmp was cleared / file removed: keep the record but flag it so
                # the UI shows the missing-file state instead of a broken link.
                reg.mark_stale(e["id"], True)
                stats["missing"] += 1
                continue

            dest_dir = paths.session_files_dir(slug, session_id) / subdir
            try:
                dest_dir.mkdir(parents=True, exist_ok=True)
                dest = unique_dest(dest_dir / src.name)
                shutil.move(str(src), str(dest))
            except OSError as exc:
                log.warning("migration: failed to move %s: %s", old_norm, exc)
                stats["errors"] += 1
                continue

            new_norm = norm_path(dest)
            reg.relocate(e["id"], dest)
            # Keep the artifact's ref in sync (in-place — add_artifact would dedupe
            # on the old ref and create a duplicate). Prefer the linked artifact_id;
            # fall back to matching by the old normalized ref.
            art_id = e.get("artifact_id")
            if art_id:
                art_store.set_ref(slug, art_id, new_norm)
            else:
                for a in art_store.list_artifacts(slug):
                    if a.get("ref") and norm_path(a["ref"]) == old_norm:
                        art_store.set_ref(slug, a["id"], new_norm)
                        break
            stats["moved"] += 1

    if stats["moved"] or stats["missing"]:
        log.info("session-files migration: %s", stats)
    return stats
