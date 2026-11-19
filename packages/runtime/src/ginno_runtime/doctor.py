"""Config health-check engine ("Doctor", 2026-11-19).

Three config surfaces (skills / MCP / hooks) plus settings and runtime grew
organically, and each has a proven silent-failure mode:

* GUI sidecar launches without the homebrew PATH → every ``npx``/``uvx`` MCP
  server in mcp.json fails to spawn with no surfacing (real incident).
* A full disk (Errno 28) kills subagents mid-turn.
* Hook matchers are whole-string compares, not regex — a matcher written as
  ``bash|write_file`` never fires for anything.
* A SKILL.md with broken frontmatter is silently skipped by the loader.
* Unknown settings.json keys are ignored forever (key drift).

This module answers "is anything misconfigured?" in one read-only sweep.
Design contract (shared with the frontend Doctor page — do not change
unilaterally)::

    GET /api/doctor → {
      "ok": true,                    # always true; failures become entries
      "ran_at": "<ISO 8601>",
      "summary": {"error": N, "warn": M, "total": K},
      "checks": [
        {"surface": "mcp"|"skills"|"hooks"|"settings"|"runtime",
         "id": "mcp.command-missing",
         "level": "ok"|"warn"|"error",
         "items": [{"name": str, "detail": str}]}
      ]
    }

* One entry per registered check, always — the frontend renders the full
  grid; ``level: "ok"`` with empty ``items`` means "ran, nothing found".
  Informational findings (project skill overriding a global one, a hook
  shadowed by the grants policy) ride as ``ok`` entries WITH items so they
  show up without inflating the warn/error counters.
* ``detail`` is a neutral technical description (paths, reasons); i18n copy
  lives in the frontend keyed by ``id``.
* Every check is read-only and stat-fast (< 2s total budget). Commands are
  probed with ``shutil.which`` — never spawned.
* A check that itself raises degrades to that entry at ``level: "error"``
  with the crash reason as detail (never a 500).
* No caching: the doctor page is opened explicitly and rarely; staleness
  after fixing a config would defeat the point of a diagnosis.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shlex
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ginno_runtime import jsonx as json

from . import paths

log = logging.getLogger("ginno.doctor")

SURFACES = ("mcp", "skills", "hooks", "settings", "runtime")

# Commands whose binaries ship outside the system PATH on macOS and are the
# usual reason a GUI-launched sidecar cannot spawn an MCP server.
_NODEISH_COMMANDS = frozenset({"npx", "node", "npm", "pnpm", "bun", "bunx", "uvx", "uv", "deno"})
_VALID_TRANSPORTS = frozenset({"stdio", "sse", "streamable-http"})
_SCRIPT_SUFFIXES = (
    ".py",
    ".sh",
    ".bash",
    ".zsh",
    ".mjs",
    ".cjs",
    ".js",
    ".ts",
    ".json",
)

# Hook matchers are whole-string, case-insensitive compares against the tool
# name (hooks/dispatcher.py _matches). A matcher containing any of these regex
# metacharacters can never equal a real tool name (tool names are identifiers).
_REGEX_META_RE = re.compile(r"[*^$\[\]()|+?{}]")

# Relative file/script references inside a SKILL.md body. Only markdown code
# spans and link targets are considered — prose mentions stay out of scope,
# which keeps the false-positive rate tolerable.
_MD_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_MD_LINK_RE = re.compile(r"\]\(([^)\s]+)\)")
_REF_SUFFIXES = (
    ".py",
    ".sh",
    ".mjs",
    ".cjs",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".md",
    ".csv",
    ".txt",
    ".toml",
    ".sql",
)

# settings.json top-level keys the runtime actually reads. Source of truth is
# a repo-wide grep of ``settings.get("...")`` over paths.settings_path()
# readers (2026-11-19). When you add a settings key, add it here — a missing
# entry turns every install into a false-positive warn.
#   defaults (paths._DEFAULT_SETTINGS):
_KNOWN_SETTINGS_KEYS = frozenset(
    {
        "default_provider",
        "language",
        "providers",
        "permissions",
        "hooks",
        "tool_labels",
        "context",
        "floating",
        "instincts",
        "knowledge",
        "mods",
        "bypass_permissions",
        # scattered runtime readers:
        #   connectors (connectors/registry.py, a2a/node.py)
        #   web (web/config.py) · browser (browser/config.py)
        #   subagent (subagent_scheduler/meta.py) · team (teams/store.py)
        #   background_tasks (tasks/store.py) · devswap (lastwords.py)
        #   classifier (classifier/config.py) · rtk (tools/rtk_bridge.py)
        #   env (tools/env_inject.py) · runtime (api/stream/errors.py)
        #   model_configs / use_system_proxy / default_config (providers.py)
        #   agents.order (agents/registry.py)
        #   notifications — frontend-consumed (NotificationsSettings.tsx),
        #   like "floating": the backend never reads it but the UI does
        "connectors",
        "web",
        "browser",
        "subagent",
        "team",
        "background_tasks",
        "devswap",
        "classifier",
        "rtk",
        "env",
        "runtime",
        "model_configs",
        "use_system_proxy",
        "default_config",
        "agents",
        "notifications",
        # legacy one-shot migration keys (paths._migrate_settings/_language)
        "model",
        "prompt_language",
    }
)

# Seed permissions.deny from paths._DEFAULT_SETTINGS — entries beyond these
# are user-authored and worth flagging when privileged mode ignores them.
_SEED_DENY_RULES = frozenset(
    {
        "Bash(rm -rf *)",
        "bash(rm -rf *)",
        "Bash(sudo *)",
        "bash(sudo *)",
        "Write(~/.ssh/**)",
        "Write(~/.gnupg/**)",
    }
)


# ---------------------------------------------------------------- engine ----


@dataclass
class Finding:
    """One item inside a check entry — ``name`` identifies the config piece,
    ``detail`` says why it matters (neutral, technical)."""

    name: str
    detail: str


@dataclass
class CheckSpec:
    surface: str
    id: str
    level: str  # level used when the check yields findings ("warn"|"error")
    fn: Callable[[], list[Finding]]


@dataclass
class DoctorContext:
    """Config documents read once and shared by all checks (read-only)."""

    home: Path = field(default_factory=paths.home)
    settings: dict = field(default_factory=dict)
    settings_error: str | None = None
    config_error: str | None = None
    mcp_doc: dict | None = None  # None = no mcp.json on disk
    mcp_error: str | None = None  # JSON parse failure, if any
    mcp_path: Path | None = None

    @classmethod
    def load(cls) -> "DoctorContext":
        ctx = cls(home=paths.home())
        sp = paths.settings_path()
        if sp.exists():
            try:
                ctx.settings = json.loads(sp.read_text() or "{}")
                if not isinstance(ctx.settings, dict):
                    ctx.settings = {}
                    ctx.settings_error = "settings.json is not a JSON object"
            except (json.JSONDecodeError, OSError) as e:
                ctx.settings_error = str(e)
        cp = ctx.home / "config.json"
        if cp.exists():
            try:
                doc = json.loads(cp.read_text() or "{}")
                if not isinstance(doc, dict):
                    ctx.config_error = "config.json is not a JSON object"
            except (json.JSONDecodeError, OSError) as e:
                ctx.config_error = str(e)
        ctx.mcp_path = paths.mcp_config_path()
        if ctx.mcp_path.exists():
            try:
                ctx.mcp_doc = json.loads(ctx.mcp_path.read_text() or "{}")
            except (json.JSONDecodeError, OSError) as e:
                ctx.mcp_error = str(e)
        return ctx


def _mcp_servers(ctx: DoctorContext) -> dict[str, dict]:
    """Validated-ish view of the mcp.json servers block ({}) on any error."""
    if ctx.mcp_error or not isinstance(ctx.mcp_doc, dict):
        return {}
    servers = ctx.mcp_doc.get("mcpServers") or ctx.mcp_doc.get("servers")
    if not isinstance(servers, dict):
        return {}
    return {str(k): v for k, v in servers.items() if isinstance(v, dict)}


def _enabled_stdio_servers(ctx: DoctorContext) -> list[tuple[str, dict]]:
    out: list[tuple[str, dict]] = []
    for name, cfg in _mcp_servers(ctx).items():
        transport = str(cfg.get("transport") or cfg.get("type") or "stdio")
        transport = transport.strip().lower().replace("_", "-")
        if transport != "stdio":
            continue
        if cfg.get("enabled", True) is False:
            continue
        out.append((name, cfg))
    return out


# ------------------------------------------------------------------ checks ---
# Each check returns findings; an empty list renders as level "ok".


def _check_mcp_json_parse(ctx: DoctorContext) -> list[Finding]:
    if ctx.mcp_path is None or not ctx.mcp_path.exists():
        return []
    if ctx.mcp_error:
        return [Finding(name=str(ctx.mcp_path), detail=f"invalid JSON: {ctx.mcp_error}")]
    if not isinstance(ctx.mcp_doc, dict):
        return [Finding(name=str(ctx.mcp_path), detail="top level is not a JSON object")]
    servers = ctx.mcp_doc.get("mcpServers") or ctx.mcp_doc.get("servers")
    if servers is not None and not isinstance(servers, dict):
        return [
            Finding(
                name=str(ctx.mcp_path),
                detail="servers block is not an object (expected {name: {...}})",
            )
        ]
    findings: list[Finding] = []
    for name, cfg in _mcp_servers(ctx).items():
        transport = str(cfg.get("transport") or cfg.get("type") or "stdio")
        transport = transport.strip().lower().replace("_", "-")
        if transport not in _VALID_TRANSPORTS:
            findings.append(
                Finding(name=f"server:{name}", detail=f"unknown transport '{transport}'")
            )
        elif transport == "stdio" and not str(cfg.get("command") or "").strip():
            findings.append(Finding(name=f"server:{name}", detail="stdio server has no 'command'"))
        elif transport != "stdio" and not str(cfg.get("url") or "").strip():
            findings.append(
                Finding(name=f"server:{name}", detail=f"{transport} server has no 'url'")
            )
    return findings


def _which_for(cfg: dict) -> str | None:
    """shutil.which under the server's own PATH when it overrides one —
    that override is exactly how the GUI-sidecar PATH gap gets patched, so
    it must also be how we probe."""
    env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
    override = env.get("PATH")
    if override and str(override).strip():
        # Scoped probe: build a mini-PATH just for this lookup.
        old = os.environ.get("PATH")
        try:
            os.environ["PATH"] = str(override)
            return shutil.which(str(cfg.get("command") or ""))
        finally:
            if old is None:
                os.environ.pop("PATH", None)
            else:
                os.environ["PATH"] = old
    return shutil.which(str(cfg.get("command") or ""))


def _check_mcp_command_missing(ctx: DoctorContext) -> list[Finding]:
    findings: list[Finding] = []
    for name, cfg in _enabled_stdio_servers(ctx):
        command = str(cfg.get("command") or "").strip()
        if not command:
            continue  # json-parse already flags it
        p = Path(command)
        if p.is_absolute():
            if not p.exists():
                findings.append(
                    Finding(name=f"server:{name}", detail=f"command path not found: {command}")
                )
            continue
        if _which_for(cfg) is None:
            probe_path = None
            env = cfg.get("env") if isinstance(cfg.get("env"), dict) else {}
            if env.get("PATH"):
                probe_path = str(env["PATH"])
            detail = f"'{command}' not found in PATH"
            if probe_path:
                detail += f" (server PATH override: {probe_path})"
            else:
                detail += f" (sidecar PATH: {os.environ.get('PATH', '')})"
            findings.append(Finding(name=f"server:{name}", detail=detail))
    return findings


def _check_mcp_empty_config(ctx: DoctorContext) -> list[Finding]:
    if ctx.mcp_path is None or not ctx.mcp_path.exists():
        return [Finding(name="mcp.json", detail="no MCP config file — no servers configured")]
    if not _mcp_servers(ctx):
        return [Finding(name=str(ctx.mcp_path), detail="no servers configured")]
    return []


def _skill_files(ctx: DoctorContext) -> list[tuple[Path, str]]:
    """(SKILL.md path, tier) for global + every project's skills dir."""
    out: list[tuple[Path, str]] = []
    global_dir = paths.global_skills_dir()
    if global_dir.exists():
        out += [(p, "global") for p in sorted(global_dir.glob("*/SKILL.md"))]
    projects_root = ctx.home / "projects"
    try:
        slugs = sorted(p.name for p in projects_root.iterdir() if p.is_dir())
    except OSError:
        slugs = []
    for slug in slugs:
        d = paths.project_skills_dir(slug)
        if d.exists():
            out += [(p, f"project:{slug}") for p in sorted(d.glob("*/SKILL.md"))]
    return out


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


def _parse_frontmatter(raw: str) -> tuple[dict | None, str]:
    """(meta, reason) — meta None means the loader would silently skip it."""
    import yaml

    m = _FRONTMATTER_RE.match(raw)
    if not m:
        return None, "no '---' frontmatter block found"
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError as e:
        return None, f"frontmatter is not valid YAML: {e}"
    if not isinstance(meta, dict):
        return None, "frontmatter did not parse to a mapping"
    return meta, ""


def _check_skills_frontmatter(ctx: DoctorContext) -> list[Finding]:
    findings: list[Finding] = []
    for path, _tier in _skill_files(ctx):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as e:
            findings.append(Finding(name=str(path), detail=f"unreadable: {e}"))
            continue
        meta, reason = _parse_frontmatter(raw)
        if meta is None:
            findings.append(Finding(name=str(path), detail=reason))
            continue
        if not str(meta.get("name") or "").strip():
            findings.append(Finding(name=str(path), detail="frontmatter 'name' missing or empty"))
        if not str(meta.get("description") or "").strip():
            findings.append(
                Finding(name=str(path), detail="frontmatter 'description' missing or empty")
            )
    return findings


def _check_skills_name_collision(ctx: DoctorContext) -> list[Finding]:
    global_names: dict[str, Path] = {}
    gdir = paths.global_skills_dir()
    if gdir.exists():
        for p in gdir.glob("*/SKILL.md"):
            global_names[p.parent.name] = p
    findings: list[Finding] = []
    projects_root = ctx.home / "projects"
    try:
        slugs = sorted(p.name for p in projects_root.iterdir() if p.is_dir())
    except OSError:
        return findings
    for slug in slugs:
        pdir = paths.project_skills_dir(slug)
        if not pdir.exists():
            continue
        for p in pdir.glob("*/SKILL.md"):
            if p.parent.name in global_names:
                findings.append(
                    Finding(
                        name=f"{slug}/{p.parent.name}",
                        detail=f"project skill overrides global {global_names[p.parent.name]}",
                    )
                )
    return findings


def _ref_candidates(body: str) -> list[str]:
    """Relative file references worth an existence probe (deduped, ordered).

    Template placeholders (``<file>``, ``$ARGUMENTS``) and URLs are excluded —
    a real smoke run against user skills showed they dominate the false
    positives otherwise."""
    seen: dict[str, None] = {}
    for token in _MD_CODE_SPAN_RE.findall(body) + _MD_LINK_RE.findall(body):
        token = token.strip().strip("'\"")
        if not token or token.startswith(("http://", "https://", "mailto:", "#", "<")):
            continue
        if any(ch in token for ch in "<>{}$*"):
            continue  # template placeholder, not a file
        if Path(token).is_absolute():
            continue
        if any(token.endswith(suf) for suf in _REF_SUFFIXES):
            # Directory-style refs without a suffix (e.g. `scripts/lib`) also
            # count when they carry a slash.
            seen.setdefault(token, None)
        elif "/" in token and not token.endswith(("/", ".com", ".org", ".io")):
            seen.setdefault(token, None)
    return list(seen)


def _check_skills_broken_ref(ctx: DoctorContext) -> list[Finding]:
    findings: list[Finding] = []
    for path, _tier in _skill_files(ctx):
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue  # frontmatter check already flags unreadable files
        m = _FRONTMATTER_RE.match(raw)
        body = m.group(2) if m else raw
        for ref in _ref_candidates(body):
            if not (path.parent / ref).exists():
                rel = os.path.normpath(ref)
                findings.append(
                    Finding(
                        name=f"{path.parent.name}/{rel}",
                        detail=f"referenced in {path} but missing",
                    )
                )
    return findings


def _hook_entries(ctx: DoctorContext) -> list[tuple[str, str, dict]]:
    """(event, index, entry) for the settings.json hooks block."""
    hooks = ctx.settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    out: list[tuple[str, str, dict]] = []
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for i, entry in enumerate(entries):
            if isinstance(entry, dict):
                out.append((str(event), str(i), entry))
    return out


def _known_hook_events() -> frozenset[str]:
    from .hooks.dispatcher import HookEventName

    return frozenset(HookEventName.__args__)  # type: ignore[attr-defined]


def _check_hooks_event_unknown(ctx: DoctorContext) -> list[Finding]:
    hooks = ctx.settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    known = _known_hook_events()
    return [
        Finding(name=f"hooks.{event}", detail=f"unknown event '{event}' — never fires")
        for event in hooks
        if event not in known
    ]


def _check_hooks_matcher_invalid(ctx: DoctorContext) -> list[Finding]:
    findings: list[Finding] = []
    for event, idx, entry in _hook_entries(ctx):
        if event not in _known_hook_events():
            continue  # event-unknown already covers the whole block
        matcher = str(entry.get("matcher") or "").strip()
        # Empty matcher is legal and matches every tool (dispatcher _matches).
        if matcher and _REGEX_META_RE.search(matcher):
            findings.append(
                Finding(
                    name=f"hooks.{event}[{idx}]",
                    detail=(
                        f"matcher '{matcher}' contains regex metacharacters but "
                        "matchers are whole-string tool-name compares "
                        "(case-insensitive) — never matches any tool"
                    ),
                )
            )
    return findings


def _script_token(command: str) -> str | None:
    """The first file-looking token of a hook command, if any."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    for tok in tokens:
        tok = tok.strip("'\"")
        if not tok or "${" in tok or tok.startswith("-"):
            continue
        if "://" in tok or ":" in tok or "*" in tok:
            continue  # URLs / env refs / globs are not file probes
        if tok.startswith(("~/", "./", "/")) or "/" in tok:
            return tok
        if tok.endswith(_SCRIPT_SUFFIXES):
            return tok
    return None


def _check_hooks_script_missing(ctx: DoctorContext) -> list[Finding]:
    findings: list[Finding] = []
    for event, idx, entry in _hook_entries(ctx):
        command = str(entry.get("command") or "").strip()
        if not command:
            findings.append(
                Finding(name=f"hooks.{event}[{idx}]", detail="hook entry has no 'command'")
            )
            continue
        token = _script_token(command)
        if token is None:
            continue  # plain command (curl …); nothing to probe
        p = Path(token).expanduser()
        if not p.is_file():
            findings.append(Finding(name=f"hooks.{event}[{idx}]", detail=f"script not found: {p}"))
        elif token.endswith((".sh", ".bash", ".zsh")) and not os.access(p, os.X_OK):
            findings.append(
                Finding(
                    name=f"hooks.{event}[{idx}]",
                    detail=f"script exists but is not executable: {p}",
                )
            )
    return findings


def _check_hooks_policy_shadowed(ctx: DoctorContext) -> list[Finding]:
    """Hook commands the grants layer would refuse before they ever run.

    Uses the runtime's own classifier (hooks/grants.py) — no hardcoded
    pattern list. When classification is unavailable the check yields
    nothing (probe, don't guess)."""
    try:
        from .hooks.grants import SETTINGS_HOOK_MOD, classic_grant_for, classify_hook_command
    except Exception:  # noqa: BLE001 — probe only
        return []
    if not _hook_entries(ctx):
        return []
    grant = classic_grant_for(ctx.settings, SETTINGS_HOOK_MOD)
    if grant == "allow":
        return []
    findings: list[Finding] = []
    for event, idx, entry in _hook_entries(ctx):
        command = str(entry.get("command") or "")
        danger = sorted(classify_hook_command(command))
        if danger:
            findings.append(
                Finding(
                    name=f"hooks.{event}[{idx}]",
                    detail=(
                        f"classified dangerous ({', '.join(danger)}) and the "
                        f"classic-hooks grant is '{grant}' — this hook never runs"
                    ),
                )
            )
    return findings


def _check_settings_unknown_keys(ctx: DoctorContext) -> list[Finding]:
    if ctx.settings_error:
        return []  # reported by settings.json-shape
    unknown = sorted(set(ctx.settings) - _KNOWN_SETTINGS_KEYS)
    if not unknown:
        return []
    return [
        Finding(
            name=key,
            detail="no runtime reader found for this settings.json key (key drift?)",
        )
        for key in unknown
    ]


def _check_settings_shape(ctx: DoctorContext) -> list[Finding]:
    findings: list[Finding] = []
    if ctx.settings_error:
        findings.append(
            Finding(name=str(paths.settings_path()), detail=f"invalid JSON: {ctx.settings_error}")
        )
    if ctx.config_error:
        findings.append(Finding(name=str(ctx.home / "config.json"), detail=ctx.config_error))
    return findings


def _check_settings_permission_ignored(ctx: DoctorContext) -> list[Finding]:
    """Permissions quietly ignored under privileged mode.

    ``permission.policy.is_bypass_permissions`` (default ON) makes the
    permission node pass every call through without consulting
    permissions.allow/deny/ask — a hand-written deny rule then protects
    nothing. Only user-added deny rules beyond the shipped seeds count."""
    if ctx.settings_error:
        return []
    if not ctx.settings.get("bypass_permissions", True):
        return []
    perms = ctx.settings.get("permissions")
    if not isinstance(perms, dict):
        return []
    deny = [str(x) for x in (perms.get("deny") or [])]
    ask = [str(x) for x in (perms.get("ask") or [])]
    user_deny = [x for x in deny if x not in _SEED_DENY_RULES]
    if not user_deny and not ask:
        return []
    parts = []
    if user_deny:
        parts.append(f"{len(user_deny)} user deny rule(s): {', '.join(user_deny[:3])}")
    if ask:
        parts.append(f"{len(ask)} ask rule(s)")
    return [
        Finding(
            name="permissions",
            detail=(
                "bypass_permissions=true skips permission checks entirely — " + "; ".join(parts)
            ),
        )
    ]


def _check_runtime_homebrew_path(ctx: DoctorContext) -> list[Finding]:
    if sys.platform != "darwin":
        return []
    needs_nodeish = any(
        Path(str(cfg.get("command") or "")).name in _NODEISH_COMMANDS
        for _name, cfg in _enabled_stdio_servers(ctx)
    )
    if not needs_nodeish:
        return []
    path = os.environ.get("PATH") or ""
    if "/opt/homebrew/bin" in path.split(os.pathsep):
        return []
    return [
        Finding(
            name="PATH",
            detail=(
                "/opt/homebrew/bin is not in the sidecar PATH and mcp.json "
                f"uses a node/uv-family command (sidecar PATH: {path}). Fix: "
                "set a PATH override in the server's env block, e.g. "
                '"env": {"PATH": "/opt/homebrew/bin:/usr/bin:/bin"}'
            ),
        )
    ]


def _check_runtime_disk_space(ctx: DoctorContext) -> list[Finding]:
    try:
        usage = shutil.disk_usage(ctx.home if ctx.home.exists() else Path.home())
    except OSError:
        return []
    free_gib = usage.free / (1024**3)
    if free_gib < 5:
        return [
            Finding(
                name=str(ctx.home),
                detail=f"only {free_gib:.1f} GiB free on this volume — subagents and "
                "checkpoints die with Errno 28 (no space left on device)",
            )
        ]
    if free_gib < 15:
        return [
            Finding(
                name=str(ctx.home),
                detail=f"{free_gib:.1f} GiB free on this volume (checkpoint/usage "
                "growth can exhaust it)",
            )
        ]
    return []


def _check_runtime_writable(ctx: DoctorContext) -> list[Finding]:
    findings: list[Finding] = []
    for sub in ("", "projects", "cache", "logs", "mcp"):
        p = ctx.home / sub if sub else ctx.home
        if not p.exists():
            findings.append(Finding(name=str(p), detail="expected directory is missing"))
        elif not os.access(p, os.W_OK | os.X_OK):
            findings.append(Finding(name=str(p), detail="directory is not writable"))
    return findings


def _check_runtime_dev_override(ctx: DoctorContext) -> list[Finding]:
    link = ctx.home / "dev-override" / "ginno-runtime"
    if not link.is_symlink():
        return []
    target = os.readlink(link)
    tpath = Path(target)
    if not tpath.is_absolute():
        tpath = link.parent / tpath
    if not tpath.exists():
        return [
            Finding(
                name=str(link),
                detail=f"dev-override symlink points to missing directory: {tpath}",
            )
        ]
    # A live dev bundle is healthy (the sidebar already badges it).
    return []


def _build_checks(ctx: DoctorContext) -> list[CheckSpec]:
    """The full registered check set, in a stable surface-grouped order."""
    return [
        # mcp
        CheckSpec("mcp", "mcp.json-parse", "error", lambda: _check_mcp_json_parse(ctx)),
        CheckSpec("mcp", "mcp.command-missing", "error", lambda: _check_mcp_command_missing(ctx)),
        CheckSpec("mcp", "mcp.empty-config", "ok", lambda: _check_mcp_empty_config(ctx)),
        # skills
        CheckSpec("skills", "skills.frontmatter", "warn", lambda: _check_skills_frontmatter(ctx)),
        CheckSpec(
            "skills", "skills.name-collision", "ok", lambda: _check_skills_name_collision(ctx)
        ),
        CheckSpec("skills", "skills.broken-ref", "warn", lambda: _check_skills_broken_ref(ctx)),
        # hooks
        CheckSpec("hooks", "hooks.event-unknown", "warn", lambda: _check_hooks_event_unknown(ctx)),
        CheckSpec(
            "hooks", "hooks.matcher-invalid", "warn", lambda: _check_hooks_matcher_invalid(ctx)
        ),
        CheckSpec(
            "hooks", "hooks.script-missing", "warn", lambda: _check_hooks_script_missing(ctx)
        ),
        CheckSpec(
            "hooks", "hooks.policy-shadowed", "ok", lambda: _check_hooks_policy_shadowed(ctx)
        ),
        # settings
        CheckSpec("settings", "settings.json-shape", "error", lambda: _check_settings_shape(ctx)),
        CheckSpec(
            "settings", "settings.unknown-keys", "warn", lambda: _check_settings_unknown_keys(ctx)
        ),
        CheckSpec(
            "settings",
            "settings.permission-ignored",
            "warn",
            lambda: _check_settings_permission_ignored(ctx),
        ),
        # runtime
        CheckSpec(
            "runtime", "runtime.homebrew-path", "error", lambda: _check_runtime_homebrew_path(ctx)
        ),
        CheckSpec("runtime", "runtime.disk-space", "warn", lambda: _check_runtime_disk_space(ctx)),
        CheckSpec("runtime", "runtime.writable", "error", lambda: _check_runtime_writable(ctx)),
        CheckSpec(
            "runtime", "runtime.dev-override", "error", lambda: _check_runtime_dev_override(ctx)
        ),
    ]


def _run_check(spec: CheckSpec) -> dict:
    """Run one check; any crash degrades to that entry at level 'error'."""
    try:
        findings = spec.fn()
        items = [{"name": f.name, "detail": f.detail} for f in findings or []]
        # Findings render at the check's declared level ("warn"/"error");
        # informational checks declare "ok" so their items never inflate the
        # summary counters. No findings always means "ok".
        level = spec.level if items else "ok"
    except Exception as e:  # noqa: BLE001 — a broken check is itself a finding
        log.exception("doctor check crashed: %s", spec.id)
        items = [{"name": spec.id, "detail": f"check crashed: {type(e).__name__}: {e}"}]
        level = "error"
    return {"surface": spec.surface, "id": spec.id, "level": level, "items": items}


def collect_doctor(ctx: DoctorContext | None = None) -> dict:
    """Synchronous sweep — the engine core (tests call this directly)."""
    ctx = ctx or DoctorContext.load()
    checks = [_run_check(spec) for spec in _build_checks(ctx)]
    return {
        "ok": True,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "error": sum(1 for c in checks if c["level"] == "error"),
            "warn": sum(1 for c in checks if c["level"] == "warn"),
            "total": len(checks),
        },
        "checks": checks,
    }


async def run_doctor() -> dict:
    """Concurrent sweep for the API layer (per-surface checks in threads).

    All probes are stat-fast; the threads mostly buy isolation so a slow
    filesystem hiccup on one surface cannot serialize the whole report."""
    ctx = DoctorContext.load()
    specs = _build_checks(ctx)
    results = await asyncio.gather(*(asyncio.to_thread(_run_check, s) for s in specs))
    checks = list(results)
    return {
        "ok": True,
        "ran_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "error": sum(1 for c in checks if c["level"] == "error"),
            "warn": sum(1 for c in checks if c["level"] == "warn"),
            "total": len(checks),
        },
        "checks": checks,
    }
