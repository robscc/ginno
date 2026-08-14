"""Embedded-browser tools (docs/browser-embed-design.md §5).

Product-internal: never raise a permission prompt (same class as todo/goal).
``browser_eval`` lifts ``handOff()`` to a graph ``interrupt`` so Goal/HITL
pause instead of spinning. Failures degrade to ``[error] …`` strings.
"""

from __future__ import annotations

import json
from typing import Optional

from langchain_core.tools import tool
from langgraph.types import interrupt

from ..browser import get_supervisor
from ..browser.ownership import AGENT_LOCKED
from ..browser.supervisor import BrowserLocked

BROWSER_TOOL_NAMES = {
    "browser_eval",
    "browser_snapshot",
    "browser_handoff",
    "browser_screenshot",
}


def _dumps(obj) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except TypeError:
        return str(obj)


def _interrupt_handoff(payload: dict) -> None:
    interrupt(
        {
            "kind": "browser_handoff",
            "space": payload.get("space"),
            "url": payload.get("url") or "",
            "reason": payload.get("reason") or "",
        }
    )


def build_browser_tools(
    session_id: str | None = None,
    *,
    run_id: str | None = None,
    allow_complete: bool = False,
) -> list:
    """Session-bound browser tools. Always returned (engine may be Fake)."""

    sid = session_id or ""

    @tool
    def browser_eval(
        code: str,
        space: Optional[str] = None,
        timeout_s: Optional[int] = 60,
        headed: Optional[bool] = True,
    ) -> str:
        """Run a JS helper script in the embedded browser (ego-browser dialect).

        Helpers are pre-injected: useOrCreateTaskSpace, openOrReuseTab, snapshotText,
        click('@N'), fillInput, handOffTaskSpace, takeOverTaskSpace, pageInfo, cliLog.
        Prefer this over web_fetch or mcp_playwright_* for logged-in / click / SPA
        work. On login walls or captchas call handOffTaskSpace(reason) — the turn
        pauses and the human operates the real page.

        After a handoff resumes, call takeOverTaskSpace(same name) first; do not
        open a new Space. completeTaskSpace({keep}) is forbidden here — it must
        be its own script via a dedicated complete node / later turn.

        Args:
            code: JS (or the Python helper dialect). Helpers are already in scope.
            space: Space name (3–6 words). Default is session-<id>.
            timeout_s: Soft cap, default 60, max 180.
            headed: Whether to raise the window (handoff always forces headed).
        """
        try:
            out = get_supervisor().eval(
                code,
                space=space,
                session_id=sid or None,
                run_id=run_id,
                timeout_s=min(int(timeout_s or 60), 180),
                headed=bool(headed if headed is not None else True),
                allow_complete=allow_complete,
            )
        except BrowserLocked as e:
            return f"[error] {e}"
        except Exception as e:  # noqa: BLE001 — builtin contract
            return f"[error] browser_eval failed: {type(e).__name__}: {e}"
        if isinstance(out, dict) and out.get("interrupt") == "handoff":
            _interrupt_handoff(out)
            return _dumps(out)
        if isinstance(out, dict) and out.get("error"):
            return str(out["error"])
        return _dumps(out)

    @tool
    def browser_snapshot(space: Optional[str] = None) -> str:
        """Accessibility snapshot of the current Space (text + @N refs)."""
        sup = get_supervisor()
        name = (space or "").strip()
        if not name:
            rec = sup.use_or_create(
                f"session-{sid[:8]}" if sid else "default",
                session_id=sid or None,
                run_id=run_id,
            )
            name = rec["name"]
        try:
            snap = sup.snapshot(name)
        except BrowserLocked as e:
            return f"[error] {e}"
        except Exception as e:  # noqa: BLE001
            return f"[error] snapshot failed: {type(e).__name__}: {e}"
        return str(snap.get("text") or _dumps(snap))

    @tool
    def browser_handoff(space: Optional[str] = None, reason: Optional[str] = "") -> str:
        """Hand the current Space to the human (login / captcha / payment).

        Pauses the turn. After the human clicks 交还, takeOver the SAME space.
        """
        sup = get_supervisor()
        name = (space or "").strip()
        if not name:
            rec = sup.use_or_create(
                f"session-{sid[:8]}" if sid else "default",
                session_id=sid or None,
                run_id=run_id,
            )
            name = rec["name"]
        try:
            out = sup.hand_off(name, reason=reason or "")
        except BrowserLocked as e:
            return f"[error] {e}"
        except Exception as e:
            # hand_off raises BrowserHandoff; eval-style dict is also possible
            from ..browser.helpers import BrowserHandoff

            if isinstance(e, BrowserHandoff):
                _interrupt_handoff(
                    {"space": e.space, "url": e.url, "reason": e.reason}
                )
                return _dumps(
                    {"interrupt": "handoff", "space": e.space, "url": e.url, "reason": e.reason}
                )
            return f"[error] handoff failed: {type(e).__name__}: {e}"
        if isinstance(out, dict) and out.get("skipped"):
            return _dumps(out)
        return _dumps(out)

    @tool
    def browser_screenshot(space: Optional[str] = None) -> str:
        """Capture a PNG of the current Space and register it as an artifact."""
        sup = get_supervisor()
        name = (space or "").strip()
        if not name:
            rec = sup.use_or_create(
                f"session-{sid[:8]}" if sid else "default",
                session_id=sid or None,
                run_id=run_id,
            )
            name = rec["name"]
        try:
            out = sup.screenshot(name, session_id=sid or None)
        except BrowserLocked as e:
            return f"[error] {e}"
        except Exception as e:  # noqa: BLE001
            return f"[error] screenshot failed: {type(e).__name__}: {e}"
        return _dumps(out)

    return [browser_eval, browser_snapshot, browser_handoff, browser_screenshot]


# Silence unused-import lint for AGENT_LOCKED (documented in module docstring).
_ = AGENT_LOCKED
