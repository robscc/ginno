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


@router.post("/api/diagnostics/fix/web-search-searxng")
async def fix_web_search_searxng(body: dict) -> dict:
    """一键修复 web_search（2026-11-15）：在指定会话里跑系统提供的 SearXNG
    安装 prompt。前端先 ``newSession`` 建会话并激活，再带 ``session_id`` 调本
    端点触发安装 turn，随后跳回聊天视图观看。

    与 shell-equals 的「直接写文件」不同：安装涉及 Docker/网络/代理等需逐步
    判断与重试的操作，交给一个代理会话按系统 prompt 自主完成更稳妥。turn 以
    interactive 方式跑（用户在旁观看，ask_user/权限闸可正常介入），仿 goal
    continuation 的 ``_run_stream`` 接法。幂等性由前端「每次新建会话」保证。"""
    import asyncio
    import uuid

    from .. import server_shared as shared
    from ..api import sessions as sessions_api
    from ..api.stream import turn as stream_turn
    from ..web.searxng_install import SEARXNG_INSTALL_PROMPT

    session_id = str((body or {}).get("session_id") or "").strip()
    if not session_id:
        return {"ok": False, "error": "missing session_id"}
    session = shared._SESSIONS.get(session_id) or sessions_api._ensure_session(session_id)
    if not session:
        return {"ok": False, "error": "unknown session"}
    if session_id in shared._RUNNING_TURNS or session_id in shared._PENDING_RESUME:
        return {"ok": False, "error": "session busy"}

    slug = session["project_slug"]
    agent_id = session.get("agent_id") or sessions_api._first_agent_id()
    turn_id = uuid.uuid4().hex
    prompt = SEARXNG_INSTALL_PROMPT
    config = {
        "configurable": {
            "thread_id": session_id,
            "project_slug": slug,
            "agent_id": agent_id,
            "turn_id": turn_id,
            "user_text": prompt,
            # 系统驱动的修复 turn，usage  telemetry 归入 goal 桶以便与用户
            # 手动聊天区分（usage-stats-design.md §3.6）。
            "usage_source": "goal",
        }
    }

    async def _run_install() -> None:
        # 早置 running 标志：_stream_graph 到流式开始才注册 turn，而前置
        # （compaction 等）可能耗时数秒——此窗口内用户 stop 须能看到活 turn。
        shared._RUNNING_TURNS[session_id] = turn_id
        try:
            await stream_turn._run_stream(
                None, session["graph"], config, prompt, session, agent_id
            )
        except Exception:  # noqa: BLE001 — 后台 turn 失败不得冒泡到事件循环
            import logging

            logging.getLogger(__name__).exception(
                "searxng_install_turn_failed session=%s", session_id
            )
        finally:
            if (
                shared._RUNNING_TURNS.get(session_id) == turn_id
                and session_id not in shared._PENDING_RESUME
            ):
                shared._RUNNING_TURNS.pop(session_id, None)

    asyncio.create_task(_run_install())
    return {"ok": True, "session_id": session_id}


@router.get("/api/running")
def get_running_work() -> dict:
    """Quit-check aggregate for the desktop shell (⌘Q gate): live turns +
    non-detached running background commands. Sync ``def`` on purpose — the
    title lookups hit disk and belong on the threadpool, not the event loop.
    Semantics/deliberate exclusions: running_work module docstring.
    """
    from ..running_work import running_work_report

    return running_work_report()
