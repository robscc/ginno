"""Browser REST: Space list / navigate / handoff / takeover / eval.

Sidecar is the authority. The pane polls ``GET /api/browser/state`` and the
WS layer also pushes ``browser.space`` / ``browser.handoff``.
"""

from __future__ import annotations

from fastapi import APIRouter

from ..browser import get_supervisor, reset_supervisor
from ..browser.helpers import BrowserHandoff
from ..browser.supervisor import BrowserLocked
from ..server_shared import _log, _push_global_event, spawn_bg

router = APIRouter()


def _broadcast(event: str, data: dict) -> None:
    spawn_bg(_push_global_event(event, data))


@router.get("/api/browser/state")
async def browser_state() -> dict:
    try:
        return {"ok": True, **get_supervisor().state()}
    except Exception as e:  # noqa: BLE001
        _log.exception("browser_state failed")
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "spaces": []}


@router.get("/api/browser/spaces")
async def list_spaces() -> dict:
    return {"ok": True, "spaces": get_supervisor().list_spaces()}


@router.post("/api/browser/spaces")
async def create_or_use_space(data: dict) -> dict:
    data = data or {}
    name = (data.get("name") or "").strip()
    owner = (data.get("owner") or "agent").strip()
    try:
        if owner == "user":
            rec = get_supervisor().claim_user(name or "我的")
        else:
            rec = get_supervisor().use_or_create(
                name,
                session_id=data.get("session_id"),
                run_id=data.get("run_id"),
                confirm_shared=bool(data.get("confirm_shared")),
            )
    except BrowserLocked as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _broadcast("browser.space", rec)
    return {"ok": True, "space": rec}


@router.post("/api/browser/spaces/{name}/navigate")
async def navigate_space(name: str, data: dict) -> dict:
    url = (data or {}).get("url") or ""
    human = bool((data or {}).get("human"))
    before = get_supervisor().get_space(name)
    try:
        rec = get_supervisor().navigate(name, url, human=human)
    except BrowserHandoff as h:
        payload = {
            "space": h.space,
            "url": h.url,
            "reason": h.reason,
            "owner": "agentDelegatedToUser",
        }
        _broadcast("browser.handoff", payload)
        _broadcast("browser.space", get_supervisor().get_space(name) or payload)
        return {"ok": True, "interrupt": "handoff", **payload}
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if human and (before or {}).get("owner") == "agent" and rec.get("owner") == "agentDelegatedToUser":
        _broadcast(
            "browser.handoff",
            {
                "space": name,
                "url": rec.get("url") or url,
                "reason": rec.get("reason") or "address bar",
                "owner": "agentDelegatedToUser",
            },
        )
    _broadcast("browser.space", rec)
    return {"ok": True, "space": rec}


@router.post("/api/browser/spaces/{name}/handoff")
async def handoff_space(name: str, data: dict | None = None) -> dict:
    reason = ((data or {}).get("reason") or "").strip()
    try:
        get_supervisor().hand_off(name, reason=reason)
    except BrowserHandoff as h:
        payload = {
            "space": h.space,
            "url": h.url,
            "reason": h.reason,
            "owner": "agentDelegatedToUser",
        }
        _broadcast("browser.handoff", payload)
        _broadcast("browser.space", get_supervisor().get_space(name) or payload)
        return {"ok": True, "interrupt": "handoff", **payload}
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rec = get_supervisor().get_space(name)
    return {"ok": True, "space": rec}


@router.get("/api/browser/spaces/{name}")
async def get_space(name: str) -> dict:
    rec = get_supervisor().get_space(name)
    if not rec:
        return {"ok": False, "error": f"no such space {name!r}"}
    return {"ok": True, "space": rec}


@router.post("/api/browser/spaces/{name}/takeover")
async def takeover_space(name: str) -> dict:
    try:
        rec = get_supervisor().take_over(name)
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _broadcast("browser.space", rec)
    return {"ok": True, "space": rec}


@router.post("/api/browser/spaces/{name}/complete")
async def complete_space(name: str, data: dict | None = None) -> dict:
    keep = True if data is None else bool((data or {}).get("keep", True))
    try:
        rec = get_supervisor().complete(name, keep=keep, from_complete_node=True)
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _broadcast("browser.complete", {"space": name, "keep": keep, **(rec if isinstance(rec, dict) else {})})
    return {"ok": True, **(rec if isinstance(rec, dict) else {"space": rec})}


@router.post("/api/browser/spaces/{name}/screenshot")
async def screenshot_space(name: str, data: dict | None = None) -> dict:
    data = data or {}
    try:
        out = get_supervisor().screenshot(
            name,
            session_id=data.get("session_id"),
            project_slug=data.get("project_slug"),
        )
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _broadcast("browser.extract", {"space": name, "path": out.get("path")})
    return {"ok": True, **out}


@router.post("/api/browser/dock")
async def dock_browser(data: dict) -> dict:
    """Compat alias: tile size → viewport. No OS window is moved."""
    return await set_viewport(data)


@router.post("/api/browser/viewport")
async def set_viewport(data: dict) -> dict:
    data = data or {}
    try:
        out = get_supervisor().set_viewport(
            int(data.get("width") or 0),
            int(data.get("height") or 0),
            space=(data.get("space") or None),
            dpr=float(data.get("dpr") or 1.0),
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": bool(out.get("ok", True)), **out}


@router.get("/api/browser/spaces/{name}/frame")
async def space_frame(name: str):
    from fastapi.responses import Response

    try:
        rec = get_supervisor().latest_frame(name)
    except KeyError as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    raw = rec.get("bytes") or b""
    if not raw:
        return {"ok": False, "error": rec.get("error") or "no frame"}
    mime = rec.get("mime") or "image/jpeg"
    return Response(
        content=raw,
        media_type=mime,
        headers={
            "Cache-Control": "no-store",
            "X-Frame-Width": str(rec.get("width") or 0),
            "X-Frame-Height": str(rec.get("height") or 0),
        },
    )


@router.post("/api/browser/spaces/{name}/input")
async def space_input(name: str, data: dict) -> dict:
    try:
        out = get_supervisor().dispatch_input(name, data or {})
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if out.get("handoff"):
        rec = get_supervisor().get_space(name) or out.get("space") or {}
        payload = {
            "space": name,
            "url": rec.get("url") if isinstance(rec, dict) else "",
            "reason": (rec.get("reason") if isinstance(rec, dict) else "") or "pane input",
            "owner": "agentDelegatedToUser",
        }
        _broadcast("browser.handoff", payload)
        if isinstance(rec, dict):
            _broadcast("browser.space", rec)
    return {"ok": bool(out.get("ok", True)), **out}


@router.get("/api/browser/import-chrome")
async def import_chrome_status() -> dict:
    from ..browser.profile import import_status

    return {"ok": True, **import_status()}


@router.post("/api/browser/import-chrome")
async def import_chrome(data: dict | None = None) -> dict:
    from ..browser.profile import import_chrome_profile

    data = data or {}
    out = import_chrome_profile(
        profile_id=str(data.get("profile") or data.get("profile_id") or "Default"),
        include_extensions=bool(data.get("include_extensions")),
        force=bool(data.get("force")),
    )
    return out


@router.post("/api/browser/eval")
async def eval_in_space(data: dict) -> dict:
    data = data or {}
    try:
        out = get_supervisor().eval(
            data.get("code") or "",
            space=data.get("space"),
            session_id=data.get("session_id"),
            run_id=data.get("run_id"),
            timeout_s=int(data.get("timeout_s") or 60),
            headed=bool(data.get("headed", True)),
            allow_complete=bool(data.get("allow_complete")),
        )
    except BrowserLocked as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    if isinstance(out, dict) and out.get("interrupt") == "handoff":
        _broadcast("browser.handoff", out)
        return {"ok": True, **out}
    if isinstance(out, dict) and out.get("error"):
        return {"ok": False, **out}
    rec = get_supervisor().get_space((out or {}).get("space") or data.get("space") or "")
    if rec:
        _broadcast("browser.space", rec)
    return {"ok": True, "result": out}


@router.get("/api/browser/spaces/{name}/tabs")
async def list_tabs(name: str) -> dict:
    try:
        tabs = get_supervisor().list_tabs(name)
    except KeyError as e:
        return {"ok": False, "error": str(e), "tabs": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "tabs": []}
    return {"ok": True, "tabs": tabs}


@router.post("/api/browser/spaces/{name}/tabs")
async def open_tab(name: str, data: dict | None = None) -> dict:
    data = data or {}
    human = bool(data.get("human"))
    try:
        out = get_supervisor().open_tab(name, data.get("url") or "about:blank", human=human)
    except BrowserHandoff as h:
        payload = {"space": h.space, "url": h.url, "reason": h.reason, "owner": "agentDelegatedToUser"}
        _broadcast("browser.handoff", payload)
        return {"ok": True, "interrupt": "handoff", **payload}
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rec = out.get("space") if isinstance(out, dict) else None
    if rec:
        _broadcast("browser.space", rec)
    return {"ok": bool(out.get("ok", True)), **out}


@router.post("/api/browser/spaces/{name}/tabs/{tab_id}/activate")
async def activate_tab(name: str, tab_id: str, data: dict | None = None) -> dict:
    human = bool((data or {}).get("human", True))
    try:
        rec = get_supervisor().switch_tab(name, tab_id, human=human)
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    _broadcast("browser.space", rec)
    return {"ok": True, "space": rec}


@router.post("/api/browser/spaces/{name}/tabs/{tab_id}/close")
async def close_tab(name: str, tab_id: str, data: dict | None = None) -> dict:
    human = bool((data or {}).get("human", True))
    try:
        out = get_supervisor().close_tab(name, tab_id, human=human)
    except (BrowserLocked, KeyError) as e:
        return {"ok": False, "error": str(e)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    rec = get_supervisor().get_space(name)
    if rec:
        _broadcast("browser.space", rec)
    return {"ok": bool(out.get("ok", True)), **out}


@router.get("/api/browser/spaces/{name}/downloads")
async def list_space_downloads(name: str) -> dict:
    try:
        items = get_supervisor().list_downloads(name)
    except KeyError as e:
        return {"ok": False, "error": str(e), "downloads": []}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "downloads": []}
    return {"ok": True, "downloads": items}


@router.get("/api/browser/downloads")
async def list_all_downloads() -> dict:
    try:
        items = get_supervisor().list_downloads(None)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "downloads": []}
    return {"ok": True, "downloads": items}


@router.post("/api/browser/reset")
async def reset_browser() -> dict:
    """Test/ops: drop the singleton and reap Chrome."""
    reset_supervisor()
    return {"ok": True}
