"""Doctor engine unit tests (2026-11-19).

Every check gets a "broken config is caught" fixture and a "healthy config
produces no finding" counterpart — the engine must never cry wolf on a good
install. All fixtures live under the isolated GINNO_HOME (tmp), never the
user's real ~/.ginno.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from ginno_runtime import doctor
from ginno_runtime.doctor import DoctorContext

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------- helpers ----


def home() -> Path:
    return Path(os.environ["GINNO_HOME"])


def write_mcp(doc: dict | str) -> None:
    p = home() / "mcp" / "mcp.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))


def write_settings(doc: dict | str) -> None:
    p = home() / "settings.json"
    p.write_text(doc if isinstance(doc, str) else json.dumps(doc))


def write_skill(rel: str, frontmatter: str = "", body: str = "do the thing\n") -> Path:
    p = home() / rel / "SKILL.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\n{frontmatter}---\n\n{body}")
    return p


def ctx() -> DoctorContext:
    return DoctorContext.load()


def finding_names(check, context: DoctorContext | None = None) -> list[str]:
    return [f.name for f in check(context or ctx())]


# -------------------------------------------------------------------- mcp ----


def test_mcp_json_parse_catches_broken_json():
    write_mcp("{not valid json")
    names = finding_names(doctor._check_mcp_json_parse)
    assert names == [str(home() / "mcp" / "mcp.json")]


def test_mcp_json_parse_catches_stdio_without_command():
    write_mcp({"mcpServers": {"broken": {"transport": "stdio", "args": ["x"]}}})
    findings = doctor._check_mcp_json_parse(ctx())
    assert [f.name for f in findings] == ["server:broken"]
    assert "no 'command'" in findings[0].detail


def test_mcp_json_parse_catches_unknown_transport():
    write_mcp({"mcpServers": {"weird": {"transport": "websocket", "command": "x"}}})
    findings = doctor._check_mcp_json_parse(ctx())
    assert "unknown transport" in findings[0].detail


def test_mcp_json_parse_clean_config_no_findings():
    write_mcp({"mcpServers": {"ok": {"transport": "stdio", "command": "python3"}}})
    assert doctor._check_mcp_json_parse(ctx()) == []


def test_mcp_command_missing_catches_missing_binary():
    write_mcp({"mcpServers": {"s": {"transport": "stdio", "command": "definitely-not-a-cmd-42"}}})
    findings = doctor._check_mcp_command_missing(ctx())
    assert [f.name for f in findings] == ["server:s"]
    assert "not found in PATH" in findings[0].detail


def test_mcp_command_missing_honors_env_path_override(tmp_path):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "definitely-not-a-cmd-42").write_text("#!/bin/sh\n")
    os.chmod(bin_dir / "definitely-not-a-cmd-42", 0o755)
    write_mcp(
        {
            "mcpServers": {
                "s": {
                    "transport": "stdio",
                    "command": "definitely-not-a-cmd-42",
                    "env": {"PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin"},
                }
            }
        }
    )
    assert doctor._check_mcp_command_missing(ctx()) == []


def test_mcp_command_missing_skips_disabled_servers():
    write_mcp(
        {
            "mcpServers": {
                "off": {
                    "transport": "stdio",
                    "command": "definitely-not-a-cmd-42",
                    "enabled": False,
                }
            }
        }
    )
    assert doctor._check_mcp_command_missing(ctx()) == []


def test_mcp_command_missing_absolute_path_missing():
    write_mcp({"mcpServers": {"s": {"command": "/no/such/binary anywhere"}}})
    # command with a space is a config error anyway; use a clean path form
    write_mcp({"mcpServers": {"s": {"command": "/no/such/binary"}}})
    findings = doctor._check_mcp_command_missing(ctx())
    assert [f.name for f in findings] == ["server:s"]


def test_mcp_command_missing_real_command_no_finding():
    write_mcp({"mcpServers": {"s": {"command": sys.executable}}})
    assert doctor._check_mcp_command_missing(ctx()) == []


def test_mcp_empty_config_reports_when_no_servers():
    write_mcp({"mcpServers": {}})
    findings = doctor._check_mcp_empty_config(ctx())
    assert findings and "no servers configured" in findings[0].detail


def test_mcp_empty_config_silent_when_servers_exist():
    write_mcp({"mcpServers": {"s": {"command": "python3"}}})
    assert doctor._check_mcp_empty_config(ctx()) == []


# ----------------------------------------------------------------- skills ----


def test_skills_frontmatter_catches_missing_block():
    p = home() / "skills" / "broken" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("no frontmatter at all, just instructions\n")
    findings = doctor._check_skills_frontmatter(ctx())
    assert [f.name for f in findings] == [str(p)]
    assert "frontmatter" in findings[0].detail


def test_skills_frontmatter_catches_bad_yaml():
    p = home() / "skills" / "badyaml" / "SKILL.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: [unclosed\n---\nbody\n")
    findings = doctor._check_skills_frontmatter(ctx())
    assert [f.name for f in findings] == [str(p)]


def test_skills_frontmatter_catches_missing_name_and_description():
    write_skill("skills/anonymous", frontmatter="trigger: both\n")
    findings = doctor._check_skills_frontmatter(ctx())
    details = " | ".join(f.detail for f in findings if f.name.endswith("anonymous/SKILL.md"))
    assert "'name'" in details and "'description'" in details


def test_skills_frontmatter_clean_skill_no_findings():
    write_skill("skills/good", frontmatter="name: good\ndescription: works\n")
    assert doctor._check_skills_frontmatter(ctx()) == []


def test_skills_name_collision_project_overrides_global():
    write_skill("skills/dup", frontmatter="name: dup\ndescription: global\n")
    write_skill("projects/demo/skills/dup", frontmatter="name: dup\ndescription: local\n")
    findings = doctor._check_skills_name_collision(ctx())
    assert [f.name for f in findings] == ["demo/dup"]
    assert "overrides global" in findings[0].detail


def test_skills_name_collision_distinct_names_no_findings():
    write_skill("skills/alpha", frontmatter="name: alpha\ndescription: a\n")
    write_skill("projects/demo/skills/beta", frontmatter="name: beta\ndescription: b\n")
    assert doctor._check_skills_name_collision(ctx()) == []


def test_skills_broken_ref_catches_missing_relative_file():
    write_skill(
        "skills/refy",
        frontmatter="name: refy\ndescription: r\n",
        body="run `./scripts/run.py` then check [docs](notes/extra.md)\n",
    )
    findings = doctor._check_skills_broken_ref(ctx())
    refs = {f.name for f in findings}
    assert "refy/scripts/run.py" in refs
    assert "refy/notes/extra.md" in refs


def test_skills_broken_ref_existing_files_no_findings():
    write_skill(
        "skills/refy",
        frontmatter="name: refy\ndescription: r\n",
        body="run `./scripts/run.py`\n",
    )
    scripts = home() / "skills" / "refy" / "scripts" / "run.py"
    scripts.parent.mkdir(parents=True, exist_ok=True)
    scripts.write_text("print('ok')\n")
    assert doctor._check_skills_broken_ref(ctx()) == []


def test_skills_broken_ref_ignores_template_placeholders():
    write_skill(
        "skills/templ",
        frontmatter="name: templ\ndescription: t\n",
        body="fill in `<file>` and `scripts/<file>` and $ARGUMENTS paths\n",
    )
    assert doctor._check_skills_broken_ref(ctx()) == []


def test_skills_broken_ref_ignores_urls_and_plain_prose():
    write_skill(
        "skills/chatty",
        frontmatter="name: chatty\ndescription: c\n",
        body="see https://example.com/x.py and the foo.py convention\n",
    )
    assert doctor._check_skills_broken_ref(ctx()) == []


# ------------------------------------------------------------------ hooks ----


def test_hooks_unknown_event_never_fires():
    write_settings({"hooks": {"OnToolCall": [{"command": "true"}]}})
    findings = doctor._check_hooks_event_unknown(ctx())
    assert [f.name for f in findings] == ["hooks.OnToolCall"]
    assert "unknown event" in findings[0].detail


def test_hooks_matcher_regex_meta_never_matches():
    write_settings({"hooks": {"PreToolUse": [{"matcher": "bash|write_file", "command": "true"}]}})
    findings = doctor._check_hooks_matcher_invalid(ctx())
    assert [f.name for f in findings] == ["hooks.PreToolUse[0]"]
    assert "whole-string" in findings[0].detail


def test_hooks_matcher_plain_and_empty_are_valid():
    write_settings(
        {
            "hooks": {
                "PreToolUse": [{"matcher": "bash", "command": "true"}],
                "Stop": [{"command": "true"}],
            }
        }
    )
    assert doctor._check_hooks_matcher_invalid(ctx()) == []


def test_hooks_script_missing_catches_absent_script():
    write_settings({"hooks": {"SessionStart": [{"command": "python3 ~/.ginno/hooks/ghost.py"}]}})
    findings = doctor._check_hooks_script_missing(ctx())
    assert [f.name for f in findings] == ["hooks.SessionStart[0]"]
    assert "script not found" in findings[0].detail


def test_hooks_script_missing_existing_script_no_finding(tmp_path):
    script = tmp_path / "hook.sh"
    script.write_text("#!/bin/sh\ntrue\n")
    os.chmod(script, 0o755)
    write_settings({"hooks": {"Stop": [{"command": f"sh {script}"}]}})
    assert doctor._check_hooks_script_missing(ctx()) == []


def test_hooks_script_missing_plain_command_no_finding():
    write_settings({"hooks": {"Stop": [{"command": "curl -s https://example.com"}]}})
    assert doctor._check_hooks_script_missing(ctx()) == []


def test_hooks_policy_shadowed_dangerous_command_under_default_grant():
    write_settings({"hooks": {"PreToolUse": [{"matcher": "bash", "command": "brew install mpv"}]}})
    findings = doctor._check_hooks_policy_shadowed(ctx())
    assert [f.name for f in findings] == ["hooks.PreToolUse[0]"]
    assert "never runs" in findings[0].detail


def test_hooks_policy_shadowed_allow_grant_no_finding():
    write_settings(
        {
            "hooks": {"PreToolUse": [{"matcher": "bash", "command": "brew install mpv"}]},
            "mods": {"items": {"settings": {"grants": {"classic": "allow"}}}},
        }
    )
    assert doctor._check_hooks_policy_shadowed(ctx()) == []


def test_hooks_policy_shadowed_benign_command_no_finding():
    write_settings({"hooks": {"Stop": [{"command": "echo done"}]}})
    assert doctor._check_hooks_policy_shadowed(ctx()) == []


# --------------------------------------------------------------- settings ----


def test_settings_shape_catches_broken_json():
    write_settings("{broken")
    (home() / "config.json").write_text("{also broken")
    findings = doctor._check_settings_shape(ctx())
    assert len(findings) == 2
    joined = " | ".join(f.detail for f in findings)
    assert "invalid JSON" in joined
    assert any(f.name.endswith("config.json") for f in findings)


def test_settings_unknown_keys_catches_drift():
    write_settings({"providers": {}, "totally_bogus_key": 1})
    findings = doctor._check_settings_unknown_keys(ctx())
    assert [f.name for f in findings] == ["totally_bogus_key"]
    assert "key drift" in findings[0].detail


def test_settings_unknown_keys_clean_settings_no_findings():
    # Every key the runtime genuinely reads must stay whitelisted — this is
    # the no-false-positive guard for the whitelist itself.
    write_settings(
        {
            "default_provider": "custom",
            "language": "zh-CN",
            "providers": {},
            "permissions": {},
            "hooks": {},
            "tool_labels": {},
            "context": {},
            "floating": {},
            "instincts": {},
            "knowledge": {},
            "mods": {},
            "bypass_permissions": True,
            "connectors": {},
            "web": {},
            "browser": {},
            "subagent": {},
            "team": {},
            "background_tasks": {},
            "devswap": {},
            "classifier": {},
            "rtk": {},
            "env": {},
            "runtime": {},
            "model_configs": [],
            "use_system_proxy": True,
            "default_config": "prov_main",
            "agents": {"order": []},
            "notifications": {},
        }
    )
    assert doctor._check_settings_unknown_keys(ctx()) == []


def test_settings_permission_ignored_flags_user_deny_under_bypass():
    write_settings(
        {
            "bypass_permissions": True,
            "permissions": {"deny": ["Bash(dangerous-tool *)"]},
        }
    )
    findings = doctor._check_settings_permission_ignored(ctx())
    assert [f.name for f in findings] == ["permissions"]
    assert "bypass_permissions=true" in findings[0].detail


def test_settings_permission_ignored_silent_for_seed_rules_only():
    write_settings(
        {
            "bypass_permissions": True,
            "permissions": {"deny": ["Bash(rm -rf *)", "bash(rm -rf *)", "bash(sudo *)"]},
        }
    )
    assert doctor._check_settings_permission_ignored(ctx()) == []


def test_settings_permission_ignored_silent_when_bypass_off():
    write_settings(
        {
            "bypass_permissions": False,
            "permissions": {"deny": ["Bash(dangerous-tool *)"]},
        }
    )
    assert doctor._check_settings_permission_ignored(ctx()) == []


# ---------------------------------------------------------------- runtime ----


def test_runtime_homebrew_path_missing_with_nodeish_mcp(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    write_mcp({"mcpServers": {"s": {"transport": "stdio", "command": "npx"}}})
    findings = doctor._check_runtime_homebrew_path(ctx())
    assert len(findings) == 1
    assert "/opt/homebrew/bin" in findings[0].detail
    assert "env" in findings[0].detail  # carries the fix suggestion


def test_runtime_homebrew_path_present_no_finding(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "/opt/homebrew/bin:/usr/bin:/bin")
    write_mcp({"mcpServers": {"s": {"transport": "stdio", "command": "npx"}}})
    assert doctor._check_runtime_homebrew_path(ctx()) == []


def test_runtime_homebrew_path_irrelevant_without_nodeish_mcp(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    write_mcp({"mcpServers": {"s": {"transport": "stdio", "command": "python3"}}})
    assert doctor._check_runtime_homebrew_path(ctx()) == []


def test_runtime_disk_space_error_below_5gib(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda p: type("U", (), {"free": 2 * 1024**3, "total": 100 * 1024**3, "used": 1})(),
    )
    findings = doctor._check_runtime_disk_space(ctx())
    assert len(findings) == 1
    assert "Errno 28" in findings[0].detail


def test_runtime_disk_space_warn_below_15gib(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda p: type("U", (), {"free": 10 * 1024**3, "total": 100 * 1024**3, "used": 1})(),
    )
    findings = doctor._check_runtime_disk_space(ctx())
    assert len(findings) == 1
    assert "Errno 28" not in findings[0].detail


def test_runtime_disk_space_healthy_no_finding(monkeypatch):
    monkeypatch.setattr(
        doctor.shutil,
        "disk_usage",
        lambda p: type("U", (), {"free": 200 * 1024**3, "total": 256 * 1024**3, "used": 1})(),
    )
    assert doctor._check_runtime_disk_space(ctx()) == []


def test_runtime_writable_catches_missing_dir():
    import shutil as _shutil

    cache = home() / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    _shutil.rmtree(cache)
    findings = doctor._check_runtime_writable(ctx())
    assert any("missing" in f.detail for f in findings)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_runtime_writable_catches_readonly_dir():
    p = home() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    os.chmod(p, 0o555)
    try:
        findings = doctor._check_runtime_writable(ctx())
        assert any("not writable" in f.detail for f in findings)
    finally:
        os.chmod(p, 0o755)  # restore so tmp cleanup works


def test_runtime_dev_override_dangling_symlink():
    d = home() / "dev-override"
    d.mkdir(parents=True, exist_ok=True)
    link = d / "ginno-runtime"
    link.symlink_to(d / "ginno-runtime.vdead", target_is_directory=True)
    findings = doctor._check_runtime_dev_override(ctx())
    assert len(findings) == 1
    assert "missing directory" in findings[0].detail


def test_runtime_dev_override_live_symlink_is_healthy():
    d = home() / "dev-override"
    target = d / "ginno-runtime.v123"
    target.mkdir(parents=True)
    (d / "ginno-runtime").symlink_to(target, target_is_directory=True)
    assert doctor._check_runtime_dev_override(ctx()) == []


def test_runtime_dev_override_absent_no_finding():
    assert doctor._check_runtime_dev_override(ctx()) == []


# ----------------------------------------------------------------- engine ----


def test_run_check_crash_degrades_to_error_entry():
    from ginno_runtime.doctor import CheckSpec, Finding, _run_check

    def boom() -> list[Finding]:
        raise RuntimeError("kaboom")

    spec = CheckSpec("mcp", "mcp.json-parse", "error", boom)
    entry = _run_check(spec)
    assert entry["level"] == "error"
    assert "kaboom" in entry["items"][0]["detail"]


def test_collect_doctor_contract_shape():
    report = doctor.collect_doctor(ctx())
    assert report["ok"] is True
    assert set(report) == {"ok", "ran_at", "summary", "checks"}
    from datetime import datetime

    datetime.fromisoformat(report["ran_at"])  # valid ISO
    surfaces = {c["surface"] for c in report["checks"]}
    assert surfaces <= set(doctor.SURFACES)
    assert surfaces == set(doctor.SURFACES)  # all five surfaces present
    for c in report["checks"]:
        assert c["level"] in ("ok", "warn", "error")
        assert isinstance(c["items"], list)
        for item in c["items"]:
            assert set(item) == {"name", "detail"}
    s = report["summary"]
    assert s["total"] == len(report["checks"])
    assert s["error"] == sum(1 for c in report["checks"] if c["level"] == "error")
    assert s["warn"] == sum(1 for c in report["checks"] if c["level"] == "warn")


def test_collect_doctor_healthy_install_all_ok():
    # isolated_home is seeded by the lifespan-less unit path: lay out a
    # minimal healthy tree ourselves.
    write_settings({"providers": {}, "hooks": {}})
    write_mcp({"mcpServers": {"s": {"transport": "stdio", "command": sys.executable}}})
    report = doctor.collect_doctor(ctx())
    bad = [(c["id"], c["items"]) for c in report["checks"] if c["level"] in ("warn", "error")]
    # disk-space/writable depend on the real machine: filter those out here.
    bad = [b for b in bad if not b[0].startswith("runtime.")]
    assert bad == []
