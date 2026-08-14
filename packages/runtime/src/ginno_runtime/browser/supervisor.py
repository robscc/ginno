"""BrowserSupervisor — Space / ownership / eval / handoff (design §7 / §12.2).

Single source of truth. Tools, REST, the workflow BrowserNode, and the Goal
driver all go through this object. The engine (Fake / Chrome / later CEF) is
swappable; Space state lives on disk under ``~/.ginno/browser/``.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from typing import Any

from . import spaces as space_store
from .engine import BrowserEngine, choose_engine
from .helpers import BrowserHandoff, HelperHost, run_script
from .ownership import AGENT_LOCKED, OWNER_AGENT, OWNER_DELEGATED, OWNER_USER

log = logging.getLogger(__name__)


class BrowserLocked(RuntimeError):
    """Agent tools hard-stop while ownership is delegated / user-owned."""


def _url_keep(prev: str, new: str) -> str:
    """A freshly restarted engine reports about:blank; don't let that
    clobber the Space's persisted URL (restart reattach needs it)."""
    if new in ("", "about:blank") and prev not in ("", "about:blank"):
        return prev
    return new or prev


class BrowserSupervisor:
    def __init__(self, engine: BrowserEngine | None = None) -> None:
        space_store.ensure_browser_layout()
        self._engine: BrowserEngine | None = engine
        self._engine_lock = threading.Lock()
        self._lock = threading.RLock()
        self._reattached = False
        self._last_promo_check = 0.0

    # -- engine ------------------------------------------------------------ #

    def _eng(self) -> BrowserEngine:
        if self._engine is None:
            with self._engine_lock:
                if self._engine is None:
                    self._engine = choose_engine()
        else:
            self._maybe_promote_cef()
        return self._engine

    def _maybe_promote_cef(self) -> None:
        """Startup race: the sidecar's first choose_engine() can run before
        the CEF host finishes cef_initialize, caching the Chrome screencast
        fallback forever. Promote to CEF once the host goes live."""
        eng = self._engine
        if eng is None or getattr(eng, "kind", None) == "cef":
            return
        now = time.monotonic()
        if now - self._last_promo_check < 10.0:
            return
        self._last_promo_check = now
        from .cef import read_cef_status, try_cef

        rec = read_cef_status()
        if not rec or not rec.get("ready"):
            return
        cef = try_cef()
        log.info("cef promotion probe: try_cef=%s port=%s",
                 "ok" if cef is not None else "none", rec.get("port"))
        if cef is None:
            return
        with self._engine_lock:
            self._engine = cef
        log.info("browser engine promoted chrome->cef (host came up late)")
        try:
            eng.close()
        except Exception:
            log.exception("closing superseded chrome engine failed")
        return self._engine

    def close_sync(self) -> None:
        eng = self._engine
        self._engine = None
        if eng is not None:
            try:
                eng.close()
            except Exception:
                log.exception("browser engine close failed")

    # -- spaces ------------------------------------------------------------ #

    def list_spaces(self) -> list[dict[str, Any]]:
        return space_store.list_spaces()

    def get_space(self, name: str) -> dict[str, Any] | None:
        return space_store.get_space(name)

    def use_or_create(
        self,
        name: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        confirm_shared: bool = False,
    ) -> dict[str, Any]:
        key = (name or "").strip() or (
            f"session-{session_id[:8]}" if session_id else "default"
        )
        with self._lock:
            existing = space_store.get_space(key)
            if existing and existing["owner"] == OWNER_USER:
                raise BrowserLocked(
                    f"space {key!r} is user-owned; claim it explicitly first"
                )
            if existing and run_id:
                bound = existing.get("bound_run_id")
                if bound and bound != run_id:
                    # Two runs must not silently share a Space (design §9.4).
                    # Explicit `shared:` prefix + confirm_shared is the opt-in.
                    if not (key.startswith("shared:") and confirm_shared):
                        raise BrowserLocked(
                            f"space {key!r} is bound to run {bound}; "
                            "use space='shared:…' and confirm_shared=true to share"
                        )
            rec = existing or {
                "name": key,
                "owner": OWNER_AGENT,
                "bound_session_id": session_id,
                "bound_run_id": run_id,
            }
            if existing:
                # Same-name reuse (design §4.2 / §9.3). Do not recreate.
                if session_id and not rec.get("bound_session_id"):
                    rec["bound_session_id"] = session_id
                if run_id and not rec.get("bound_run_id"):
                    rec["bound_run_id"] = run_id
            rec["owner"] = rec.get("owner") or OWNER_AGENT
            rec = space_store.upsert_space(rec)
            self._eng().ensure_space(key)
            info = self._eng().page_info(key)
            rec["url"] = info.get("url") or rec.get("url") or ""
            rec["title"] = info.get("title") or rec.get("title") or ""
            rec["headed"] = bool(self._eng().headed())
            rec = space_store.upsert_space(rec)
            space_store.write_state(
                {"active_space": key, "url": rec.get("url") or "", "focus": rec["owner"]}
            )
            return rec

    def _require(self, name: str) -> dict[str, Any]:
        rec = space_store.get_space(name)
        if not rec:
            raise KeyError(f"no such space {name!r}")
        return rec

    def _guard_agent(self, name: str) -> dict[str, Any]:
        rec = self._require(name)
        if rec["owner"] in AGENT_LOCKED:
            raise BrowserLocked(
                f"space {name!r} is {rec['owner']}; agent tools hard-stop until takeOver"
            )
        return rec

    def navigate(self, name: str, url: str, *, human: bool = False) -> dict[str, Any]:
        from .helpers import normalize_url

        url = normalize_url(url)
        rec = self._require(name)
        if human:
            # Address bar / pane. Claiming the Space is the same as a click.
            if rec["owner"] == OWNER_AGENT:
                rec = self._delegate_for_human(rec, reason="address bar")
        else:
            self._guard_agent(name)
        from .risk import is_risky_url

        # Human already chose the URL — don't bounce them into another handoff.
        if not human and is_risky_url(url):
            rec = space_store.get_space(name) or {"name": name}
            rec["pending_risky_url"] = url
            # Goal.waiting_human() only looks at owner == delegated. Flip first
            # so the driver actually stops (design §10.1 / §13).
            rec = self._delegate_for_human(rec, reason=f"high-risk domain requires human: {url}")
            rec["pending_risky_url"] = url
            space_store.upsert_space(rec)
            raise BrowserHandoff(name, url=url, reason=rec.get("reason") or f"high-risk domain requires human: {url}")
        info = self._eng().navigate(name, url)
        rec = space_store.get_space(name) or {"name": name}
        rec["url"] = info.get("url") or url
        rec["title"] = info.get("title") or rec.get("title") or ""
        rec.pop("pending_risky_url", None)
        space_store.upsert_space(rec)
        space_store.write_state(
            {"active_space": name, "url": rec["url"], "focus": rec.get("owner")}
        )
        return rec

    def snapshot(self, name: str) -> dict[str, Any]:
        # Observation is allowed even while delegated (the human is on the page;
        # the agent may still *look*, just not click). User-owned stays locked.
        rec = self._require(name)
        if rec["owner"] == OWNER_USER:
            raise BrowserLocked(f"space {name!r} is user-owned")
        snap = self._eng().snapshot(name)
        rec["url"] = _url_keep(rec.get("url") or "", snap.get("url") or "")
        rec["title"] = snap.get("title") or rec.get("title") or ""
        space_store.upsert_space(rec)
        return snap

    def click(self, name: str, ref: str) -> dict[str, Any]:
        self._guard_agent(name)
        return self._eng().click(name, str(ref).lstrip("@"))

    def fill(self, name: str, ref: str, value: str) -> dict[str, Any]:
        self._guard_agent(name)
        return self._eng().fill(name, str(ref).lstrip("@"), value)

    def evaluate(self, name: str, expression: str) -> Any:
        self._guard_agent(name)
        return self._eng().evaluate(name, expression)

    def hover(self, name: str, ref: str) -> dict[str, Any]:
        self._guard_agent(name)
        return self._eng().hover(name, str(ref).lstrip("@"))

    def select(self, name: str, ref: str, value: str) -> dict[str, Any]:
        self._guard_agent(name)
        return self._eng().select(name, str(ref).lstrip("@"), value)

    def scroll(self, name: str, dx: int = 0, dy: int = 600) -> dict[str, Any]:
        self._guard_agent(name)
        return self._eng().scroll(name, dx, dy)

    def dispatch_key(self, name: str, key: str) -> dict[str, Any]:
        self._guard_agent(name)
        return self._eng().dispatch_key(name, key)

    def upload_file(self, name: str, ref: str, path: str) -> dict[str, Any]:
        self._guard_agent(name)
        return self._eng().upload_file(name, str(ref).lstrip("@"), path)

    def element_eval(self, name: str, ref: str, expression: str) -> Any:
        self._guard_agent(name)
        return self._eng().element_eval(name, str(ref).lstrip("@"), expression)

    def cdp(self, name: str, method: str, params: dict | None = None) -> Any:
        self._guard_agent(name)
        return self._eng().cdp(name, method, params)

    def list_tabs(self, name: str) -> list[dict[str, Any]]:
        rec = self._require(name)
        tabs = self._eng().list_tabs(name)
        rec["tabs"] = [t.get("id") for t in tabs if isinstance(t, dict) and t.get("id")]
        space_store.upsert_space(rec)
        return tabs

    def open_tab(self, name: str, url: str = "about:blank", *, human: bool = False) -> dict[str, Any]:
        rec = self._require(name)
        if human:
            if rec["owner"] == OWNER_AGENT:
                rec = self._delegate_for_human(rec, reason="new tab")
        else:
            self._guard_agent(name)
        opener = getattr(self._eng(), "open_tab", None)
        if opener is None:
            return {"ok": False, "error": "engine has no open_tab"}
        from .helpers import normalize_url

        out = opener(name, normalize_url(url) if url else "about:blank")
        rec = space_store.get_space(name) or rec
        info = self._eng().page_info(name)
        rec["url"] = _url_keep(rec.get("url") or "", info.get("url") or "")
        rec["title"] = info.get("title") or rec.get("title") or ""
        space_store.upsert_space(rec)
        return {"ok": True, "tab": out, "space": rec}

    def close_tab(self, name: str, tab_id: str | None = None, *, human: bool = False) -> dict[str, Any]:
        rec = self._require(name)
        if human:
            if rec["owner"] == OWNER_AGENT:
                rec = self._delegate_for_human(rec, reason="close tab")
        else:
            self._guard_agent(name)
        return self._eng().close_tab(name, tab_id)

    def switch_tab(self, name: str, tab_id: str, *, human: bool = False) -> dict[str, Any]:
        rec = self._require(name)
        if human:
            if rec["owner"] == OWNER_AGENT:
                rec = self._delegate_for_human(rec, reason="switch tab")
        else:
            self._guard_agent(name)
        info = self._eng().switch_tab(name, tab_id)
        rec = space_store.get_space(name) or rec
        rec["url"] = _url_keep(rec.get("url") or "", info.get("url") or "")
        rec["title"] = info.get("title") or rec.get("title") or ""
        space_store.upsert_space(rec)
        return rec

    def list_downloads(self, name: str | None = None) -> list[dict[str, Any]]:
        from . import downloads as dl

        if name:
            self._require(name)
        return dl.list_downloads(name)

    def screenshot(
        self,
        name: str,
        *,
        session_id: str | None = None,
        project_slug: str | None = None,
    ) -> dict[str, Any]:
        rec = self._require(name)
        if rec["owner"] == OWNER_USER:
            raise BrowserLocked(f"space {name!r} is user-owned")
        png = self._eng().screenshot(name)
        saved = self._persist_screenshot(png, name, session_id=session_id, project_slug=project_slug)
        return {"ok": True, "path": saved.get("path"), "artifact": saved.get("artifact"), **self.page_info(name)}

    def _persist_screenshot(
        self,
        png: bytes,
        space: str,
        *,
        session_id: str | None,
        project_slug: str | None,
    ) -> dict[str, Any]:
        from .. import paths
        from ..artifacts import store as art_store

        slug = project_slug
        if not slug and session_id:
            from ..session_meta import _session_slug

            slug = _session_slug(session_id)
        if not slug:
            dest_dir = space_store.browser_dir() / "screenshots"
        else:
            dest_dir = (
                paths.session_results_dir(slug, session_id)
                if session_id
                else paths.project_dir(slug)
            )
        dest_dir.mkdir(parents=True, exist_ok=True)
        fname = f"browser-{space.replace('/', '-')}-{int(time.time())}.png"
        dest = dest_dir / fname
        dest.write_bytes(png)
        art = None
        if slug:
            try:
                art = art_store.add_artifact(
                    slug, "file", f"browser {space}", str(dest), session_id
                )
            except Exception:
                log.exception("screenshot artifact register failed")
        return {"path": str(dest), "artifact": art}

    def set_bounds(self, x: int, y: int, width: int, height: int) -> dict[str, Any]:
        # Viewport only — there is no OS Chrome window to dock.
        return self.set_viewport(int(width), int(height))

    def set_viewport(
        self,
        width: int,
        height: int,
        *,
        space: str | None = None,
        dpr: float = 1.0,
    ) -> dict[str, Any]:
        return self._eng().set_viewport(space or "", int(width), int(height), float(dpr or 1.0))

    def latest_frame(self, name: str) -> dict[str, Any]:
        self._require(name)
        return self._eng().latest_frame(name)

    def _delegate_for_human(self, rec: dict[str, Any], reason: str = "") -> dict[str, Any]:
        """Flip agent → delegated without raising BrowserHandoff.

        Used when the person already has their finger on the pane. The REST
        layer broadcasts the handoff so the chat card still appears.
        """
        rec["owner"] = OWNER_DELEGATED
        rec["reason"] = reason or rec.get("reason") or "pane input"
        rec["headed"] = False
        rec = space_store.upsert_space(rec)
        space_store.write_state(
            {"active_space": rec["name"], "url": rec.get("url") or "", "focus": OWNER_DELEGATED}
        )
        return rec

    def dispatch_input(self, name: str, event: dict[str, Any]) -> dict[str, Any]:
        rec = self._require(name)
        ev = event or {}
        kind = str(ev.get("type") or "")
        claimed = False
        if rec["owner"] == OWNER_AGENT:
            # Hover must not steal the Space. A real click / key / wheel is
            # the person saying "I want this page" — flip and deliver.
            if kind in ("", "mouseMoved"):
                raise BrowserLocked(f"space {name!r} is agent-owned; take over first")
            rec = self._delegate_for_human(rec, reason="pane input")
            claimed = True
        out = self._eng().dispatch_input(name, ev)
        if claimed:
            out = dict(out or {})
            out["handoff"] = True
            out["space"] = rec
        return out

    def page_info(self, name: str) -> dict[str, Any]:
        rec = space_store.get_space(name)
        if not rec:
            return {"url": "", "title": ""}
        try:
            info = self._eng().page_info(name)
        except Exception:
            info = {"url": rec.get("url") or "", "title": rec.get("title") or ""}
        return info

    # -- ownership --------------------------------------------------------- #

    def hand_off(self, name: str, reason: str = "") -> dict[str, Any]:
        rec = self._require(name)
        if rec["owner"] == OWNER_USER:
            return {"done": False, "skipped": "user-owned", "space": name}
        rec["owner"] = OWNER_DELEGATED
        rec["reason"] = reason or rec.get("reason") or ""
        rec["headed"] = False
        info = self.page_info(name)
        rec["url"] = info.get("url") or rec.get("url") or ""
        rec["title"] = info.get("title") or rec.get("title") or ""
        rec = space_store.upsert_space(rec)
        try:
            self._eng().activate(name)
        except Exception:
            pass
        space_store.write_state(
            {"active_space": name, "url": rec["url"], "focus": OWNER_DELEGATED}
        )
        raise BrowserHandoff(name, url=rec["url"], reason=rec["reason"])

    def take_over(self, name: str) -> dict[str, Any]:
        rec = self._require(name)
        if rec["owner"] == OWNER_USER:
            return {"done": False, "skipped": "user-owned", "space": name}
        rec["owner"] = OWNER_AGENT
        rec["reason"] = ""
        rec = space_store.upsert_space(rec)
        space_store.write_state(
            {"active_space": name, "url": rec.get("url") or "", "focus": OWNER_AGENT}
        )
        return rec

    def claim_user(self, name: str) -> dict[str, Any]:
        """Human Space: the person owns this tab; agent cannot complete it."""
        rec = space_store.get_space(name) or {"name": name}
        rec["owner"] = OWNER_USER
        rec = space_store.upsert_space(rec)
        self._eng().ensure_space(name)
        space_store.write_state(
            {"active_space": name, "url": rec.get("url") or "", "focus": OWNER_USER}
        )
        return rec

    def complete(
        self,
        name: str,
        *,
        keep: bool = True,
        from_complete_node: bool = False,
    ) -> dict[str, Any]:
        rec = space_store.get_space(name)
        if not rec:
            return {"ok": False, "error": f"no such space {name!r}"}
        if rec["owner"] == OWNER_USER:
            return {"done": False, "skipped": "user-owned", "space": name}
        if not from_complete_node:
            return {
                "ok": False,
                "error": "[error] complete must be its own script/node (ego PR #29)",
            }
        rec["keep"] = bool(keep)
        rec["owner"] = OWNER_AGENT
        if keep:
            rec = space_store.upsert_space(rec)
            return {"ok": True, "kept": True, "space": rec}
        try:
            self._eng().close_space(name)
        except Exception:
            log.exception("close_space failed")
        space_store.delete_space(name)
        st = space_store.read_state()
        if st.get("active_space") == name:
            space_store.write_state({"active_space": None, "url": "", "focus": None})
        return {"ok": True, "kept": False, "space": name}

    def waiting_human(self, session_id: str | None = None) -> bool:
        for s in space_store.list_spaces():
            if s.get("owner") != OWNER_DELEGATED:
                continue
            if session_id and s.get("bound_session_id") not in (None, session_id):
                continue
            return True
        return False

    def _maybe_reattach(self, eng: Any, st: dict[str, Any], spaces: list[dict[str, Any]]) -> None:
        """The in-process CEF browser is brand new (about:blank) after every
        app restart — the persisted Space still points at a dead tab id.
        Re-navigate the active Space's URL once so the tile is not blank.
        Engine-level only: ownership / handoff state is untouched."""
        if self._reattached or getattr(eng, "kind", None) != "cef":
            return
        self._reattached = True
        active = (st or {}).get("active_space") or ""
        rec = next((s for s in spaces if s.get("name") == active), None)
        url = (rec or {}).get("url") or ""
        if rec is None or not url or url == "about:blank":
            return
        from .risk import is_risky_url

        if is_risky_url(url):
            return

        def _work() -> None:
            try:
                info = eng.page_info(active)
                cur = (info or {}).get("url") or ""
                if cur in ("", "about:blank"):
                    eng.navigate(active, url)
                    log.info("cef reattach: %s -> %s", active, url)
            except Exception:
                log.exception("cef reattach failed for %s", active)

        threading.Thread(target=_work, daemon=True).start()

    def state(self) -> dict[str, Any]:
        from .engine import FakeEngine, last_engine_error

        # Opening the pane / retrying after reset should actually start the
        # engine; also the only reliable heartbeat for CEF promotion when the
        # placeholder (fake) engine is cached and nothing else calls _eng().
        try:
            self._eng()
        except Exception:
            log.exception("engine start from state() failed")
        st = space_store.read_state()
        spaces = space_store.list_spaces()
        eng = self._engine
        if eng is None:
            kind = "idle"
        elif isinstance(eng, FakeEngine):
            kind = "fake"
        else:
            kind = str(getattr(eng, "kind", None) or "chrome")
        if eng is not None:
            self._maybe_reattach(eng, st, spaces)
        return {
            **st,
            "spaces": spaces,
            "waiting_human": any(s.get("owner") == OWNER_DELEGATED for s in spaces),
            "headed": bool(eng.headed()) if eng is not None else False,
            "engine": kind,
            "engine_error": last_engine_error(),
        }

    # -- eval -------------------------------------------------------------- #

    def eval(
        self,
        code: str,
        space: str | None = None,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        timeout_s: int = 60,
        headed: bool = True,
        allow_complete: bool = False,
    ) -> dict[str, Any]:
        """Run a helper script. ``BrowserHandoff`` is re-raised for the tool/node."""
        name = (space or "").strip() or (
            f"session-{session_id[:8]}" if session_id else "default"
        )
        rec = self.use_or_create(name, session_id=session_id, run_id=run_id)
        if rec["owner"] in AGENT_LOCKED:
            raise BrowserLocked(
                f"space {name!r} is {rec['owner']}; takeOver first, do not open a new Space"
            )
        if headed:
            try:
                self._eng().activate(name)
            except Exception:
                pass
        host = HelperHost(
            self,
            rec["name"],
            session_id=session_id,
            run_id=run_id,
            allow_complete=allow_complete,
        )
        cap = max(5, min(int(timeout_s or 60), 180))

        def _run() -> dict[str, Any]:
            try:
                return run_script(host, code)
            except BrowserHandoff as h:
                return {
                    "interrupt": "handoff",
                    "space": h.space,
                    "url": h.url,
                    "reason": h.reason,
                }

        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run)
            try:
                out = fut.result(timeout=cap)
            except FuturesTimeout:
                return {
                    "error": f"[error] browser_eval timed out after {cap}s",
                    "recoverable": True,
                    "space": host.space,
                }
        if isinstance(out, dict) and out.get("interrupt") == "handoff":
            return out
        info = self.page_info(host.space)
        if isinstance(out, dict) and out.get("ok"):
            out["url"] = info.get("url")
            out["title"] = info.get("title")
            out["space"] = host.space
        return out
