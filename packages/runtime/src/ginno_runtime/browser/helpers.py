"""Ego-compatible helper surface, executed in a restricted Python sandbox.

M0 does not ship a real JS VM. Agent scripts are written in the ego-browser
JS dialect (``await useOrCreateTaskSpace(...)``) and transpiled to Python
before ``exec``. Only the pre-injected helpers are visible — no ``import``,
``open``, or ``os``.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import urlparse

from .ownership import OWNER_USER


# --------------------------------------------------------------------------- #
# URL / wait helpers used by both the sandbox and ChromeEngine
# --------------------------------------------------------------------------- #


_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*://", re.I)
_HOSTISH_RE = re.compile(
    r"^(?:localhost|(?:\d{1,3}\.){3}\d{1,3}|[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+)(?::\d+)?(?:[/?#].*)?$",
    re.I,
)
_LOGIN_HINTS = (
    "login",
    "signin",
    "sign-in",
    "sign_in",
    "log-in",
    "passport",
    "sso",
    "cas.",
    "accounts.",
    "auth.",
    "oauth",
    "captcha",
    "challenge",
    "verify",
    "verification",
    "2fa",
    "mfa",
    "password",
    "log in",
    "sign in",
)


def normalize_url(url: str) -> str:
    """Bare hosts become https://… so `ai.sf-express.com` actually navigates.

    about: / ginno: / file: / data: / http(s): pass through. Empty → about:blank.
    """
    raw = (url or "").strip()
    if not raw:
        return "about:blank"
    if raw.startswith(("about:", "ginno:", "file:", "data:", "javascript:")):
        return raw
    if _SCHEME_RE.match(raw):
        return raw
    if _HOSTISH_RE.match(raw) or raw.startswith("localhost"):
        return "https://" + raw.lstrip("/")
    return raw


def looks_like_login_wall(url: str = "", title: str = "", snap: str = "") -> bool:
    blob = " ".join((url or "", title or "", (snap or "")[:2000])).lower()
    if not blob.strip():
        return False
    return any(h in blob for h in _LOGIN_HINTS)


def _opt_get(opts: Any, *keys: str, default: Any = None) -> Any:
    """Read a helper option from either kwargs or a JS-style options object."""
    if isinstance(opts, dict):
        for k in keys:
            if k in opts and opts[k] is not None:
                return opts[k]
    return default


def _is_blank_url(url: str) -> bool:
    raw = (url or "").strip().lower()
    if not raw or raw == "about:blank" or raw.startswith("about:blank"):
        return True
    parsed = urlparse(raw)
    return parsed.scheme in ("", "about") and (parsed.path in ("", "blank") or parsed.netloc == "")


class BrowserHandoff(Exception):
    """Raised by ``handOffTaskSpace`` so the caller can lift it to a graph interrupt."""

    def __init__(self, space: str, url: str = "", reason: str = "") -> None:
        super().__init__(reason or space)
        self.space = space
        self.url = url
        self.reason = reason


# --------------------------------------------------------------------------- #
# JS → Python transpile (M0 subset: helpers + literals + assignment + return)
# --------------------------------------------------------------------------- #


_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.S)
_AWAIT = re.compile(r"\bawait\s+")
_DECL = re.compile(r"\b(?:const|let|var)\s+")
_BOOL = re.compile(r"\btrue\b")
_FALSE = re.compile(r"\bfalse\b")
_NULL = re.compile(r"\bnull\b")
_UNDEF = re.compile(r"\bundefined\b")
_RETURN = re.compile(r"^(\s*)return\b", re.M)
_UNQUOTED_KEY = re.compile(r"([{\[,]\s*)([A-Za-z_][A-Za-z0-9_]*)\s*:")


def transpile_js(src: str) -> str:
    """Best-effort JS-subset → Python. Good enough for the ego helper dialect."""
    text = _COMMENT_BLOCK.sub("", src or "")
    # Strip `//` comments but keep `https://` / `ginno://` URL schemes.
    text = re.sub(r"(?<!:)//.*?$", "", text, flags=re.M)
    text = _AWAIT.sub("", text)
    text = _DECL.sub("", text)
    text = _BOOL.sub("True", text)
    text = _FALSE.sub("False", text)
    text = _NULL.sub("None", text)
    text = _UNDEF.sub("None", text)
    text = _RETURN.sub(r"\1__result__ =", text)
    # `{ wait: True }` → `{ "wait": True }` (repeat for nested objects)
    prev = None
    while prev != text:
        prev = text
        text = _UNQUOTED_KEY.sub(r'\1"\2":', text)
    return text


_BANNED = re.compile(
    r"\b(__import__|importlib|exec|eval|compile|open|input|getattr|setattr|"
    r"globals|locals|vars|dir|type|classmethod|staticmethod|delattr|"
    r"memoryview|breakpoint)\b"
)


def run_script(host: "HelperHost", code: str) -> dict[str, Any]:
    """Execute helper script. Never raises into the graph except BrowserHandoff."""
    src = (code or "").strip()
    if not src:
        return {"ok": True, "log": list(host.logs), "return": None}
    py = transpile_js(src)
    if _BANNED.search(py):
        return {
            "error": "[error] script uses a banned name (import/exec/open/…)",
            "recoverable": False,
        }
    ns: dict[str, Any] = {"__result__": None}
    ns.update(host.namespace())
    try:
        exec(py, {"__builtins__": {}}, ns)  # noqa: S102 — sandbox: empty builtins
    except BrowserHandoff:
        raise
    except SyntaxError:
        # Fall back: treat the original as Python (tests / BrowserNode authors).
        try:
            exec(src, {"__builtins__": {}}, ns)  # noqa: S102
        except BrowserHandoff:
            raise
        except Exception as e:
            return {
                "error": f"[error] script failed: {type(e).__name__}: {e}",
                "recoverable": True,
                "log": list(host.logs),
            }
    except Exception as e:
        return {
            "error": f"[error] script failed: {type(e).__name__}: {e}",
            "recoverable": True,
            "log": list(host.logs),
        }
    return {
        "ok": True,
        "log": list(host.logs),
        "return": _jsonable(ns.get("__result__")),
        "space": host.space,
    }


def _jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (str, int, float, bool)):
        return v
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    try:
        json.dumps(v)
        return v
    except TypeError:
        return str(v)


# --------------------------------------------------------------------------- #
# Helper host — names match ego-browser SKILL.md
# --------------------------------------------------------------------------- #


class HelperHost:
    def __init__(
        self,
        supervisor: Any,
        space: str,
        *,
        session_id: str | None = None,
        run_id: str | None = None,
        allow_complete: bool = False,
    ) -> None:
        self.sup = supervisor
        self.space = space
        self.session_id = session_id
        self.run_id = run_id
        self.allow_complete = allow_complete
        self.logs: list[str] = []

    def namespace(self) -> dict[str, Any]:
        names = (
            "listTaskSpaces",
            "useOrCreateTaskSpace",
            "claimTaskSpace",
            "handOffTaskSpace",
            "takeOverTaskSpace",
            "waitForAgentControl",
            "completeTaskSpace",
            "closeTaskSpace",
            "listTabs",
            "openOrReuseTab",
            "closeTab",
            "gotoAndWait",
            "currentTab",
            "switchTab",
            "gotoUrl",
            "pageInfo",
            "ensureRealTab",
            "snapshotText",
            "captureScreenshot",
            "drainEvents",
            "click",
            "fillInput",
            "hover",
            "select",
            "scroll",
            "dispatchKey",
            "uploadFile",
            "js",
            "elementEval",
            "cdp",
            "cliLog",
        )
        ns = {n: getattr(self, n) for n in names}
        # snake_case aliases so Python-authored BrowserNode scripts stay readable
        ns.update(
            {
                "use_or_create": self.useOrCreateTaskSpace,
                "hand_off": self.handOffTaskSpace,
                "take_over": self.takeOverTaskSpace,
                "complete": self.completeTaskSpace,
                "snapshot_text": self.snapshotText,
                "page_info": self.pageInfo,
                "goto_url": self.gotoUrl,
                "fill_input": self.fillInput,
                "cli_log": self.cliLog,
            }
        )
        return ns

    # -- task space -------------------------------------------------------- #

    def listTaskSpaces(self) -> list[dict[str, Any]]:
        from . import spaces as space_store

        return space_store.list_spaces()

    def useOrCreateTaskSpace(self, name: str, **kw: Any) -> dict[str, Any]:
        rec = self.sup.use_or_create(
            name,
            session_id=self.session_id,
            run_id=self.run_id,
            confirm_shared=bool(kw.get("confirm_shared") or kw.get("confirmShared")),
        )
        self.space = rec["name"]
        return rec

    def claimTaskSpace(self, name: str) -> dict[str, Any]:
        from . import spaces as space_store

        rec = space_store.get_space(name)
        if not rec:
            return {"ok": False, "error": f"no such space {name!r}"}
        if rec["owner"] == OWNER_USER:
            return {"done": False, "skipped": "user-owned"}
        self.space = name
        return self.sup.use_or_create(
            name, session_id=self.session_id, run_id=self.run_id
        )

    def handOffTaskSpace(self, reason: str = "", **_kw: Any) -> None:
        self.sup.hand_off(self.space, reason=reason or "")

    def takeOverTaskSpace(self, name: str | None = None, **_kw: Any) -> dict[str, Any]:
        target = name or self.space
        rec = self.sup.take_over(target)
        self.space = target
        return rec

    def waitForAgentControl(self, **_kw: Any) -> dict[str, Any]:
        # Ginno's protocol-level deviation: handoff is a graph interrupt, not a
        # blocking wait. This helper is a documented no-op so ego scripts port.
        return {"ok": True, "note": "ginno uses interrupt, not waitForAgentControl"}

    def completeTaskSpace(self, keep: Any = True, **kw: Any) -> dict[str, Any]:
        if isinstance(keep, dict):
            kw = {**keep, **kw}
            keep = keep.get("keep", True)
        if not self.allow_complete:
            return {
                "ok": False,
                "error": "[error] completeTaskSpace must be its own script/node "
                "(ego PR #29); do not mix it with the work script",
            }
        return self.sup.complete(self.space, keep=bool(keep), from_complete_node=True)

    def closeTaskSpace(self) -> dict[str, Any]:
        if not self.allow_complete:
            return {
                "ok": False,
                "error": "[error] closeTaskSpace is a complete(keep:false); "
                "use a dedicated complete node",
            }
        return self.sup.complete(self.space, keep=False, from_complete_node=True)

    # -- navigation -------------------------------------------------------- #

    def listTabs(self) -> list[dict[str, Any]]:
        return self.sup.list_tabs(self.space)

    def _nav_opts(self, wait: Any, timeout: Any, kw: dict[str, Any]) -> tuple[bool, float]:
        """Unpack ``openOrReuseTab(url, {wait, timeout})`` or kwargs."""
        opts = wait if isinstance(wait, dict) else kw
        if isinstance(wait, dict):
            do_wait = bool(_opt_get(opts, "wait", default=True))
            cap = _opt_get(opts, "timeout", "timeout_s", default=20)
        else:
            do_wait = True if wait is None else bool(wait)
            cap = timeout if timeout is not None else _opt_get(opts, "timeout", "timeout_s", default=20)
        try:
            seconds = float(cap if cap is not None else 20)
        except (TypeError, ValueError):
            seconds = 20.0
        return do_wait, max(1.0, min(seconds, 60.0))

    def _goto(self, url: str, *, wait: bool = True, timeout: float = 20.0) -> dict[str, Any]:
        target = normalize_url(url)
        info = self.sup.navigate(self.space, target)
        if wait:
            info = self._wait_settled(target, timeout)
        info = dict(info or {})
        info["requested"] = target
        if looks_like_login_wall(info.get("url") or "", info.get("title") or ""):
            info["login_wall"] = True
            info["hint"] = (
                "page looks like a login / SSO / captcha wall — "
                "call handOffTaskSpace('需要你登录') on this SAME space"
            )
        if _is_blank_url(info.get("url") or "") and not target.startswith("about:"):
            info["ok"] = False
            info["error"] = (
                f"tab is still about:blank after open({target!r}); "
                "do not invent @N — retry openOrReuseTab or tell the user to reset the browser"
            )
        return info

    def _wait_settled(self, requested: str, timeout: float) -> dict[str, Any]:
        # requested is the normalized target; SSO may bounce hosts so any
        # non-blank URL counts as settled.
        _ = requested
        deadline = time.time() + timeout
        last: dict[str, Any] = {}
        while time.time() < deadline:
            last = self.sup.page_info(self.space) or {}
            url = last.get("url") or ""
            if not _is_blank_url(url):
                return last
            time.sleep(0.2)
        return last or self.sup.page_info(self.space) or {}

    def openOrReuseTab(self, url: str, wait: Any = True, timeout: Any = 20, **kw: Any) -> dict[str, Any]:
        do_wait, cap = self._nav_opts(wait, timeout, kw)
        return self._goto(url, wait=do_wait, timeout=cap)

    def closeTab(self, tab_id: str | None = None, **_kw: Any) -> dict[str, Any]:
        return self.sup.close_tab(self.space, tab_id)

    def gotoAndWait(self, url: str, wait: Any = True, timeout: Any = 20, **kw: Any) -> dict[str, Any]:
        do_wait, cap = self._nav_opts(wait, timeout, kw)
        return self._goto(url, wait=do_wait, timeout=cap)

    def currentTab(self) -> dict[str, Any]:
        return self.sup.page_info(self.space)

    def switchTab(self, tab_id: str = "", **_kw: Any) -> dict[str, Any]:
        if not tab_id:
            return self.currentTab()
        return self.sup.switch_tab(self.space, tab_id)

    def gotoUrl(self, url: str, wait: Any = True, timeout: Any = 20, **kw: Any) -> dict[str, Any]:
        do_wait, cap = self._nav_opts(wait, timeout, kw)
        return self._goto(url, wait=do_wait, timeout=cap)

    def pageInfo(self) -> dict[str, Any]:
        return self.sup.page_info(self.space)

    def ensureRealTab(self) -> dict[str, Any]:
        info = self.sup.page_info(self.space) or {}
        url = info.get("url") or ""
        if _is_blank_url(url):
            return {
                **info,
                "ok": False,
                "blank": True,
                "error": (
                    "tab is still about:blank — call openOrReuseTab(https://…) first; "
                    "if it stays blank, tell the user to fully quit Ginno and reset the browser"
                ),
            }
        return {**info, "ok": True, "blank": False}

    # -- observation / interaction ----------------------------------------- #

    def snapshotText(self) -> str:
        return str(self.sup.snapshot(self.space).get("text") or "")

    def captureScreenshot(self, **_kw: Any) -> dict[str, Any]:
        return self.sup.screenshot(
            self.space, session_id=self.session_id, project_slug=None
        )

    def drainEvents(self) -> list:
        return []

    def click(self, ref: str, **_kw: Any) -> dict[str, Any]:
        return self.sup.click(self.space, ref)

    def fillInput(self, ref: str, value: str = "", **_kw: Any) -> dict[str, Any]:
        return self.sup.fill(self.space, ref, value)

    def hover(self, ref: str = "", **_kw: Any) -> dict[str, Any]:
        return self.sup.hover(self.space, ref)

    def select(self, ref: str = "", value: str = "", **_kw: Any) -> dict[str, Any]:
        return self.sup.select(self.space, ref, value)

    def scroll(self, dx: int = 0, dy: int = 600, **_kw: Any) -> dict[str, Any]:
        return self.sup.scroll(self.space, int(dx or 0), int(dy or 600))

    def dispatchKey(self, key: str = "", **_kw: Any) -> dict[str, Any]:
        return self.sup.dispatch_key(self.space, key)

    def uploadFile(self, ref: str = "", path: str = "", **_kw: Any) -> dict[str, Any]:
        return self.sup.upload_file(self.space, ref, path)

    def js(self, expression: str) -> Any:
        return self.sup.evaluate(self.space, expression)

    def elementEval(self, ref: str = "", expression: str = "", **_kw: Any) -> Any:
        return self.sup.element_eval(self.space, ref, expression)

    def cdp(self, method: str = "", params: Any = None, **_kw: Any) -> Any:
        return self.sup.cdp(self.space, method, params if isinstance(params, dict) else None)

    def cliLog(self, *args: Any) -> Any:
        line = " ".join(str(a) for a in args)
        self.logs.append(line)
        return args[0] if len(args) == 1 else list(args)
