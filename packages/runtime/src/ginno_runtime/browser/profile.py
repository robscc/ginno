"""Chrome profile import: copy cookies / local storage into Ginno's isolated dir.

Never attach to a running Chrome user-data-dir (the lock would corrupt both
profiles). The wizard asks the user to quit Chrome first, then copies a
conservative subset into ``~/.ginno/browser/profile``.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from . import spaces as space_store

# Files that carry login state without dragging the whole (locked) profile.
_COOKIE_FILES = ("Cookies", "Cookies-journal", "Network/Cookies", "Network/Cookies-journal")
_LOCAL_STATE = ("Local State",)
_LOCAL_STORAGE = ("Local Storage", "Session Storage", "IndexedDB")
_LOGIN_DATA = ("Login Data", "Login Data-journal", "Login Data For Account")
_OPTIONAL_EXT = ("Extensions", "Extension State", "Secure Preferences", "Preferences")


def default_chrome_user_data() -> Path | None:
    env = os.environ.get("GINNO_CHROME_USER_DATA")
    if env:
        p = Path(env).expanduser()
        return p if p.exists() else None
    candidates = [
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome",
        Path.home() / "Library" / "Application Support" / "Chromium",
        Path.home() / "Library" / "Application Support" / "Google" / "Chrome Canary",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def chrome_is_running() -> bool:
    """Best-effort: SingletonLock or a chrome process holding the default profile."""
    root = default_chrome_user_data()
    if root is None:
        return False
    lock = root / "SingletonLock"
    if lock.exists() or lock.is_symlink():
        return True
    # macOS: `pgrep` is cheaper than psutil and already used elsewhere.
    try:
        import subprocess

        r = subprocess.run(
            ["pgrep", "-x", "Google Chrome"],
            capture_output=True,
            check=False,
        )
        if r.returncode == 0:
            return True
    except Exception:
        pass
    return False


def list_chrome_profiles(root: Path | None = None) -> list[dict[str, Any]]:
    root = root or default_chrome_user_data()
    if root is None or not root.exists():
        return []
    out: list[dict[str, Any]] = []
    for name in ("Default", *[p.name for p in sorted(root.glob("Profile *")) if p.is_dir()]):
        d = root / name
        if not d.is_dir():
            continue
        cookies = _first_existing(d, _COOKIE_FILES)
        out.append(
            {
                "id": name,
                "path": str(d),
                "has_cookies": cookies is not None,
                "label": "Default" if name == "Default" else name,
            }
        )
    return out


def import_status() -> dict[str, Any]:
    dest = space_store.profile_dir()
    marker = dest / ".ginno-imported"
    return {
        "chrome_user_data": str(default_chrome_user_data() or ""),
        "chrome_running": chrome_is_running(),
        "profiles": list_chrome_profiles(),
        "ginno_profile": str(dest),
        "imported": marker.exists(),
        "imported_from": marker.read_text(encoding="utf-8").strip() if marker.exists() else "",
    }


def import_chrome_profile(
    profile_id: str = "Default",
    *,
    include_extensions: bool = False,
    force: bool = False,
) -> dict[str, Any]:
    """Copy login-bearing files into ``~/.ginno/browser/profile``.

    Chrome must not be running (SingletonLock). Existing Ginno profile is kept
    unless ``force`` — we never silently wipe a working login.
    """
    src_root = default_chrome_user_data()
    if src_root is None:
        return {"ok": False, "error": "no Chrome user-data-dir found"}
    src = src_root / (profile_id or "Default")
    if not src.is_dir():
        return {"ok": False, "error": f"profile {profile_id!r} not found under {src_root}"}
    if chrome_is_running() and not force:
        return {
            "ok": False,
            "error": "Chrome is running — quit it first so the profile is not locked",
            "chrome_running": True,
        }
    dest = space_store.profile_dir()
    dest.mkdir(parents=True, exist_ok=True)
    marker = dest / ".ginno-imported"
    if marker.exists() and not force:
        return {
            "ok": False,
            "error": "Ginno profile already imported; pass force=true to overwrite",
            "imported_from": marker.read_text(encoding="utf-8").strip(),
        }

    copied: list[str] = []
    skipped: list[str] = []

    def _copy(rel: str) -> None:
        s = src / rel
        d = dest / rel
        if not s.exists():
            skipped.append(rel)
            return
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            if s.is_dir():
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                # Copy via a temp file so a locked sqlite isn't half-written.
                with tempfile.NamedTemporaryFile(delete=False, dir=str(d.parent)) as tmp:
                    tmp_path = Path(tmp.name)
                try:
                    shutil.copy2(s, tmp_path)
                    os.replace(tmp_path, d)
                except Exception:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
                    raise
            copied.append(rel)
        except Exception:
            skipped.append(rel)

    for rel in (*_LOCAL_STATE, *_COOKIE_FILES, *_LOGIN_DATA, *_LOCAL_STORAGE):
        _copy(rel)
    if include_extensions:
        for rel in _OPTIONAL_EXT:
            _copy(rel)

    # Cheap integrity check: Cookies sqlite should open if we got one.
    cookies = _first_existing(dest, _COOKIE_FILES)
    cookie_ok = False
    if cookies and cookies.is_file() and cookies.name == "Cookies":
        try:
            con = sqlite3.connect(f"file:{cookies}?mode=ro", uri=True)
            con.execute("SELECT count(*) FROM cookies")
            con.close()
            cookie_ok = True
        except Exception:
            cookie_ok = False

    marker.write_text(f"{src}\n", encoding="utf-8")
    return {
        "ok": True,
        "from": str(src),
        "to": str(dest),
        "copied": copied,
        "skipped": skipped,
        "cookies_ok": cookie_ok,
        "include_extensions": include_extensions,
    }


def _first_existing(root: Path, rels: tuple[str, ...]) -> Path | None:
    for rel in rels:
        p = root / rel
        if p.exists():
            return p
    return None
