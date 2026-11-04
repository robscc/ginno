"""Diagnostics endpoint (P4-1): a read-only health report for the app."""

from __future__ import annotations

from fastapi import APIRouter

from .. import diagnostics

router = APIRouter()


@router.get("/api/diagnostics")
async def get_diagnostics() -> dict:
    """Full self-check (data integrity / tasks / runtime / contracts).

    Read-only and on demand — the settings page renders it as a report. The
    lighter startup subset lives in ``/api/health``.
    """
    return diagnostics.run_diagnostics()


@router.post("/api/diagnostics/fix/shell-equals")
async def fix_shell_equals() -> dict:
    """一键修复 zsh EQUALS（用户在诊断面板确认后调用）。

    幂等：~/.zshenv 已含 ``unsetopt EQUALS`` 就不重复追加；写入后当场
    复测非交互 zsh 并返回结果。写的是用户 home，不属于受保护目录。
    """
    from pathlib import Path

    marker = "unsetopt EQUALS"
    zshenv = Path.home() / ".zshenv"
    try:
        existing = zshenv.read_text(encoding="utf-8") if zshenv.exists() else ""
        if marker not in existing:
            block = (
                "\n# Ginno diagnostics fix (2026-11-04): agent shells are "
                "non-interactive zsh and never source ~/.zshrc.\n"
                f"{marker}\n"
            )
            with zshenv.open("a", encoding="utf-8") as fh:
                fh.write(block)
    except OSError as exc:
        return {"ok": False, "error": f"write_zshenv_failed: {exc}"}
    # 复测
    import subprocess

    try:
        probe = subprocess.run(
            ["zsh", "-c", "[[ -o equals ]] && echo on || echo off"],
            capture_output=True, text=True, timeout=5,
        )
        fixed = probe.stdout.strip() == "off"
    except Exception:  # noqa: BLE001
        fixed = False
    return {"ok": fixed, "recheck": "off" if fixed else "on"}


@router.get("/api/running")
def get_running_work() -> dict:
    """Quit-check aggregate for the desktop shell (⌘Q gate): live turns +
    non-detached running background commands. Sync ``def`` on purpose — the
    title lookups hit disk and belong on the threadpool, not the event loop.
    Semantics/deliberate exclusions: running_work module docstring.
    """
    from ..running_work import running_work_report

    return running_work_report()
