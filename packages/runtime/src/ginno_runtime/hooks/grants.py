"""Classic hook command classifier + grants gate (claude-code-mods-design.md
§8 安全模型 / implementation-notes「e2e 发现的安全问题」P1 follow-up).

Classic hooks (settings.json hooks + plugin hooks.json) are arbitrary shell —
unlike JS mods, whose ``$`` calls cross the broker IPC boundary where grants
are enforced (crates/mod-broker). Until now the classic side had NO gate: the
claude-music plugin's SessionStart hook auto-ran ``brew install mpv`` and
rewrote ``~/.claude/settings.json``.

Two pieces live here:

- :func:`classify_hook_command` — static classification of a hook command
  into danger categories (install-type package-manager writes; settings-file
  writes). Everything else (curl, echo, grep, …) is allowed by default. The
  command line alone is not enough — plugin hooks are usually
  ``${CLAUDE_PLUGIN_ROOT}/hooks/x.sh`` one-liners and the dangerous content
  lives in the referenced script, so readable script files get scanned too.
- :func:`classic_grant_for` — the per-mod tri-state grant
  (``settings mods.items.<name>.grants.classic`` = allow | deny | ask,
  default **ask**). The dispatcher refuses dangerous commands under
  deny/ask; under ask it also warns + toasts so the user can flip the
  grant in the Mods settings page.

Deliberately coarse (static, conservative): a determined obfuscator gets
past it. It exists to stop the *observed* failure mode — hooks quietly
installing packages / rewriting host settings — not to be a sandbox.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger("ginno.hooks")

# Danger categories (returned as a set; empty = allowed).
DANGER_INSTALL = "install"            # package-manager install/add-family writes
DANGER_SETTINGS_WRITE = "settings-write"  # writes a host settings.json

VALID_GRANTS = ("allow", "deny", "ask")
DEFAULT_GRANT = "ask"

# Settings hooks (no plugin name) get their own grants key so users can
# allowlist their own hooks without weakening the plugin default.
SETTINGS_HOOK_MOD = "settings"

# Scan budget for referenced script files: hooks must stay cheap.
_MAX_SCRIPT_FILES = 8
_MAX_SCRIPT_BYTES = 256_000

# ---- danger: package-manager install family ---------------------------------
# <pm> <install-verb> — brew install mpv, apt-get install -y x, npm i -g? no
# (bare `i` is too noisy), pnpm add, uv sync, nix-env -iA, pacman -S …
_PM_INSTALL_RE = re.compile(
    r"(?<![\w.@/-])"
    r"(?P<pm>apt-get|aptitude|apt|brew|dnf|yum|pacman|apk|zypper|conda|nix-env|"
    r"npm|pnpm|yarn|bun|pip3|pip|uv|pipx|cargo|gem|snap|flatpak|winget|scoop|choco)"
    r"(?:\s+-{1,2}[\w-]+)*"
    r"\s+(?:install|addGroup|add|sync|-iA|-i\b|-S\b)"
)

# ---- danger: host settings.json writes ---------------------------------------
# The path itself (reads included) plus one write indicator: redirect / tee /
# sed -i / python open(…, 'w'). Reads without a write indicator stay allowed.
_SETTINGS_PATH_RE = re.compile(r"\.(?:claude|ginno)/settings\.json")
# stderr/null redirects must not count as write indicators (grep f 2>/dev/null).
_NULL_REDIRECT_RE = re.compile(r"[0-9&]*>>?\s*/dev/null")
_REDIRECT_RE = re.compile(r">>|(?<![-)>=!])>(?!=)")
_TEE_RE = re.compile(r"(?<![\w-])tee\b")
_SED_INPLACE_RE = re.compile(r"(?<![\w-])sed\b[^>|;&]*\s-[a-zA-Z]*i")
_PY_OPEN_WRITE_RE = re.compile(r"open\([^()]*['\"]w[a+]?['\"]\s*\)")

# Path-like tokens in a command that may point at a scannable script.
_PATH_TOKEN_RE = re.compile(r"[^\s;&|<>\"']*/[^\s;&|<>\"']*")


def classify_text(text: str) -> set[str]:
    """Static classification of one shell command/script text."""
    dangers: set[str] = set()
    if _PM_INSTALL_RE.search(text):
        dangers.add(DANGER_INSTALL)
    if _SETTINGS_PATH_RE.search(text):
        # Strip null redirects before looking for a real write indicator.
        writable = _NULL_REDIRECT_RE.sub(" ", text)
        if (
            _REDIRECT_RE.search(writable)
            or _TEE_RE.search(writable)
            or _SED_INPLACE_RE.search(writable)
            or _PY_OPEN_WRITE_RE.search(writable)
        ):
            dangers.add(DANGER_SETTINGS_WRITE)
    return dangers


def _scannable_scripts(cmd: str) -> list[str]:
    """Existing file paths the command references (best effort — quoted,
    expanded and env-substituted tokens; anything that stats as a file)."""
    out: list[str] = []
    for raw in _PATH_TOKEN_RE.findall(cmd)[: _MAX_SCRIPT_FILES * 2]:
        token = raw.strip("'\"")
        if "${CLAUDE_PLUGIN_ROOT}" in token:
            continue  # unsubstituted template — nothing readable behind it
        for cand in (token, Path(token).expanduser()):
            try:
                p = Path(str(cand))
                if p.is_file() and p.stat().st_size <= _MAX_SCRIPT_BYTES:
                    out.append(str(p))
                    break
            except OSError:
                continue
        if len(out) >= _MAX_SCRIPT_FILES:
            break
    return out


def classify_hook_command(cmd: str) -> set[str]:
    """Classify a classic hook command: the inline text plus (for script
    one-liner commands like ``…/hooks/session-start.sh``) the referenced
    script files — one level deep, that is where plugin hooks keep their
    logic (the claude-music case)."""
    cmd = cmd or ""
    dangers = classify_text(cmd)
    if not cmd.strip():
        return dangers
    for path in _scannable_scripts(cmd):
        try:
            body = Path(path).read_text(errors="replace")[:_MAX_SCRIPT_BYTES]
        except OSError:
            continue
        dangers |= classify_text(body)
    return dangers


# ---- grants tri-state ------------------------------------------------------------


def classic_grant_for(settings: dict | None, mod: str) -> str:
    """``settings mods.items.<mod>.grants.classic`` as allow|deny|ask.

    Prefers a live settings read so a grant flipped in the Mods settings page
    takes effect on the next hook run without a dispatcher rebuild; falls back
    to the dispatcher's startup copy when the read fails. Unknown/missing →
    ``ask`` (dangerous commands are denied by default)."""
    live: dict = {}
    try:
        from ..mods.bridge_utils import load_mods_settings

        items = load_mods_settings().get("items") or {}
        if isinstance(items.get(mod), dict):
            live = items[mod]
    except Exception:  # noqa: BLE001 — grants must never break dispatch
        live = {}
    stored: dict = (((settings or {}).get("mods") or {}).get("items") or {}).get(mod) or {}
    for item in (live, stored):
        grant = (item.get("grants") or {}).get("classic")
        if grant in VALID_GRANTS:
            return str(grant)
    return DEFAULT_GRANT
