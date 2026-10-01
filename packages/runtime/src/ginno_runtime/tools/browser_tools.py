"""Browser tools (browser-companion-extension-design.md §4).

16 tools behind one dispatch gateway: when the companion extension is
connected (扩展轨) calls go through the relay; otherwise the dedicated-profile
Chrome backend (B 轨) serves them per ``fallback_profile_mode``. Tool schemas
embed their own usage guidance (设计 §4 — guidance lives in the schema, not
the system prompt).

Builtin contract: never raise — failures degrade to ``[error] …`` results.
Screenshots return ``[text, image_url]`` content blocks (vision input for the
model; ``graph.strip_old_images`` preserves the ToolMessage class when
trimming old ones).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from ..browser.cdp import CDPError
from ..browser.config import load_browser_config
from ..browser.executor import get_profile_backend
from ..browser.relay import RelayError, invoke_tool, relay_state

BROWSER_TOOL_NAMES = (
    "browser_computer", "browser_read_page", "browser_find",
    "browser_form_input", "browser_navigate", "browser_page_text",
    "browser_js", "browser_console", "browser_network",
    "browser_file_upload", "browser_resize_window", "browser_tabs_context",
    "browser_tabs_create", "browser_tabs_close", "browser_handoff",
    "browser_stop",
)

_COMPUTER_ACTIONS = (
    "left_click", "right_click", "double_click", "triple_click", "type",
    "screenshot", "zoom", "scroll", "scroll_to", "hover", "left_click_drag",
    "key", "wait",
)

# module-level stop flag (browser_stop sets it; long loops check it)
_stop_requested = False

# handoff release event (browser_handoff blocks on it; the Connectors UI
# "已接管/继续" button sets it via api/connectors)
_handoff_events: dict[str, asyncio.Event] = {}


def _fallback_note(cfg) -> str:
    mode = cfg.fallback_profile_mode
    if mode == "off":
        return ("[error] 浏览器扩展未连接,且备用浏览器实例已关闭(fallback=off)。"
                "请让用户在 连接器 页面安装扩展或启用备用实例。")
    return ""


def _err(prefix: str, e: Exception) -> str:
    msg = getattr(e, "message", None) or str(e)
    return f"[error] {prefix}: {msg}"


def _ext_result_to_content(res: dict) -> Any:
    """Relay (MCP-style) result → tool return value (str or content blocks)."""
    blocks = res.get("content") or []
    texts, images = [], []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            texts.append(b.get("text", ""))
        elif b.get("type") == "image" and b.get("data"):
            images.append({
                "type": "image_url",
                "image_url": {"url": f"data:{b.get('mimeType', 'image/jpeg')}"
                                     f";base64,{b['data']}"},
            })
    if not images:
        return "\n".join(t for t in texts if t)
    return [{"type": "text", "text": "\n".join(t for t in texts if t)}] + images


def _shot_blocks(data_b64: str, note: str) -> Any:
    return [
        {"type": "text", "text": note},
        {"type": "image_url",
         "image_url": {"url": f"data:image/jpeg;base64,{data_b64}"}},
    ]


async def _dispatch(tool_name: str, args: dict, slow: bool = False) -> Any:
    """Gateway: relay if the extension is connected, else the profile backend."""
    global _stop_requested
    cfg = load_browser_config()
    reg_state = relay_state()
    if reg_state.connected:
        try:
            _stop_requested = False
            res = await invoke_tool(tool_name, args, slow=slow)
            return _ext_result_to_content(res)
        except RelayError as e:
            if not _looks_recoverable(e):
                raise
    # B 轨 fallback
    if cfg.fallback_profile_mode == "off":
        raise RelayError(_fallback_note(cfg))
    backend = get_profile_backend()
    backend.cfg = cfg
    try:
        out = await _profile_run(backend, tool_name, args)
    except Exception:
        # 状态上抛给连接器卡片,但异常继续走 never-raise 之外的真实路径
        _report_profile("error", "B 轨执行失败")
        raise
    _report_profile("connected", "运行中")
    return out


def _report_profile(status: str, detail: str) -> None:
    try:
        from ..connectors import registry as _reg
        _reg.registry().report("browser-profile", status, detail,
                               extra=({"idle": True} if status == "disconnected" else {}))
    except Exception:  # noqa: BLE001 — 状态上报绝不影响工具执行
        pass


def _looks_recoverable(e: RelayError) -> bool:
    """Only fall to B 轨 on transport-level failures; real tool errors
    (the extension answered with an error) must surface as-is."""
    msg = str(e)
    return ("未连接" in msg or "队列已满" in msg or "发送到扩展失败" in msg
            or "执行超时" in msg)


async def _profile_run(b, name: str, a: dict) -> Any:
    """Map a tool invocation onto the ProfileBackend (B 轨)."""
    global _stop_requested
    tid = a.get("tabId") or 0
    if name == "browser_computer":
        action = a.get("action", "")
        mods = _parse_modifiers(a.get("modifiers"))
        if a.get("ref") and action in (
                "left_click", "right_click", "double_click", "triple_click",
                "hover", "scroll_to"):
            if action == "scroll_to":
                c = await b.scroll_to(tid, a["ref"])
                return f"Scrolled to {a['ref']} at ({round(c['x'])}, {round(c['y'])})"
            c = await b.resolve_ref_coords(tid, a["ref"])
            a = {**a, "coordinate": [c["x"], c["y"]]}
        if action in ("left_click", "right_click", "double_click", "triple_click"):
            c = a.get("coordinate") or []
            await b.click(tid, action, c[0], c[1], mods)
            label = {"left_click": "Clicked", "right_click": "Right-clicked",
                     "double_click": "Double-clicked",
                     "triple_click": "Triple-clicked"}[action]
            return f"{label} at ({round(c[0])}, {round(c[1])})"
        if action == "hover":
            c = a.get("coordinate") or []
            await b.hover(tid, c[0], c[1])
            return f"Hovered at ({round(c[0])}, {round(c[1])})"
        if action == "left_click_drag":
            s, e = a.get("start_coordinate") or [], a.get("coordinate") or []
            await b.drag(tid, s[0], s[1], e[0], e[1], mods)
            return f"Dragged from ({round(s[0])},{round(s[1])}) to ({round(e[0])},{round(e[1])})"
        if action == "type":
            r = await b.type_text(tid, a.get("text", ""),
                                  stop_check=lambda: _stop_requested)
            return f"Typed {r['typed']} characters"
        if action == "key":
            r = await b.press_key(tid, a.get("text", ""), a.get("repeat", 1),
                                  stop_check=lambda: _stop_requested)
            extra = f" ({r['repeat']}x)" if r["repeat"] > 1 else ""
            return f"Pressed key: {r['pressed']}" + extra
        if action == "scroll":
            r = await b.scroll(tid, a.get("scroll_direction", "down"),
                               a.get("scroll_amount", 3),
                               *(a.get("coordinate") or [640, 360]))
            return f"Scrolled {r['scrolled']} x{r['amount']}"
        if action == "screenshot":
            r = await b.screenshot(tid, a.get("quality", "low"))
            return _shot_blocks(r["data"], r["note"])
        if action == "zoom":
            r = await b.screenshot(tid, a.get("quality", "medium"),
                                   region=a.get("region"))
            return _shot_blocks(r["data"], r["note"])
        if action == "wait":
            await asyncio.sleep(min(a.get("duration", 1.0), 10.0))
            return f"Waited {a.get('duration', 1.0)}s"
        raise CDPError(f'未知的 computer action "{action}"。可选:{", ".join(_COMPUTER_ACTIONS)}')
    if name == "browser_read_page":
        out = await b.read_page(tid, a.get("filter", "all"),
                                a.get("depth", 15), a.get("max_chars", 50000),
                                a.get("ref_id"))
        if isinstance(out, dict) and out.get("error"):
            raise CDPError(out["error"])
        return out["tree"]
    if name == "browser_find":
        out = await b.find_elements(tid, a.get("query", ""), a.get("max_results", 20))
        rows = out.get("results", [])
        if not rows:
            return (f"[find] 没有匹配 {a.get('query')!r} 的元素。注意 find 只做字面"
                    "匹配——换用页面上实际出现的词,或用 browser_read_page 直接浏览。")
        return "[find] " + a.get("query", "") + "\n" + "\n".join(
            f'[{r["ref"]}] {r["role"]} "{r["text"]}" (score {r["score"]})'
            for r in rows)
    if name == "browser_form_input":
        out = await b.form_input(tid, a.get("ref", ""), a.get("value"))
        if not out.get("success"):
            raise CDPError(out.get("error", "form_input 失败"))
        return f'Filled {a.get("ref")} (field: {out.get("fieldName")})'
    if name == "browser_navigate":
        url = (a.get("url") or "").strip()
        force = bool(a.get("force"))
        if url in ("back", "forward"):
            info = await b.history(tid, url)
            return (f"Navigated {url} to: {info.get('url', '')}\n"
                    f"Title: {info.get('title', '')}")
        url = _normalize_url(url)
        r = await b.navigate(tid, url, force=force)
        if r.get("handled") and not r.get("accepted"):
            return ("[error] 页面的 beforeunload 处理器拦下了导航(有未保存的更改,"
                    "已按默认策略保留)。确认要丢弃更改请带 force=true 重试。")
        redir = "" if r["url"] == url else f" (redirected from {url})"
        return (f"Navigated to: {r['url']}{redir}\nTitle: {r['title']}\n"
                f"Duration: {r['durationS']}s")
    if name == "browser_page_text":
        out = await b.page_text(tid, a.get("max_chars", 50000))
        if out.get("error"):
            raise CDPError(out["error"])
        title, url = out.get("title", ""), out.get("url", "")
        return f"Title: {title}\nURL: {url}\n\n{out.get('content', '')}"
    if name == "browser_js":
        val = await b.eval_js(tid, a.get("text", ""))
        if val is None:
            return "undefined"
        if isinstance(val, str):
            return val
        import json as _json
        return _json.dumps(val, ensure_ascii=False, indent=2)
    if name == "browser_console":
        rows = await b.console_messages(tid, a.get("pattern"),
                                        bool(a.get("onlyErrors")),
                                        a.get("limit", 100))
        if a.get("clear"):
            await b.clear_console(tid)
        if not rows:
            return "No console messages recorded"
        import datetime
        lines = []
        for r in rows:
            ts = datetime.datetime.fromtimestamp(
                r["timestamp"] / 1000).strftime("%H:%M:%S")
            lines.append(f'[{ts}] [{r["type"].upper():<9}] {r["args"]}')
        suffix = ", buffer cleared" if a.get("clear") else ""
        return f"Console messages ({len(rows)}{suffix}):\n\n" + "\n".join(lines)
    if name == "browser_network":
        rows = await b.network_requests(tid, a.get("urlPattern"),
                                        a.get("limit", 100))
        if a.get("clear"):
            await b.clear_network(tid)
        if not rows:
            return "No network requests recorded"
        lines = [f'{r["method"]:<4} {str(r["status"] or "pending"):>7} '
                 f'[{str(r["type"])[:10]:<10}] {r["url"]}' for r in rows]
        suffix = ", buffer cleared" if a.get("clear") else ""
        return f"Network requests ({len(rows)}{suffix}):\n\n" + "\n".join(lines)
    if name == "browser_file_upload":
        paths = a.get("paths") or []
        _validate_upload_paths(paths)
        out = await b.file_upload(tid, a.get("ref", ""), paths,
                                  trigger_ref=a.get("triggerRef"))
        files = ", ".join(f"{f['name']} ({f['size']}B)" for f in out.get("files", []))
        return (f"Files selected at browser level: {files}\n"
                f"(若页面的上传/附件流程未消费文件,可用 browser_network 查看上传请求确认。)")
    if name == "browser_resize_window":
        await b.resize_window(tid, a.get("width", 1280), a.get("height", 800))
        return f"Resized window to {a.get('width')}x{a.get('height')}"
    if name == "browser_tabs_context":
        rows = await b.tabs_context()
        if not rows:
            return "当前没有打开的标签页。用 browser_tabs_create 新开一个。"
        return "[tabs]\n" + "\n".join(
            f'- tabId={r["tabId"]}{" (loading)" if r.get("loading") else ""} '
            f'{r.get("title", "")}\n  {r.get("url", "")}' for r in rows)
    if name == "browser_tabs_create":
        r = await b.new_tab(a.get("url") or "about:blank")
        return f'Created tab tabId={r["tabId"]}'
    if name == "browser_tabs_close":
        await b.close_tab(int(a.get("tabId", 0)))
        return f"Closed tab {a.get('tabId')}"
    raise CDPError(f"未实现的浏览器工具: {name}")


def _parse_modifiers(spec: str | None) -> int:
    if not spec:
        return 0
    out = 0
    for part in spec.lower().split("+"):
        out |= {"alt": 1, "ctrl": 2, "control": 2, "meta": 4, "cmd": 4,
                "command": 4, "shift": 8, "win": 4, "windows": 4}.get(part.strip(), 0)
    return out


def _normalize_url(url: str) -> str:
    if not url:
        raise CDPError("url 不能为空")
    import re
    if re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I) or url in ("about:blank",):
        u = url
    else:
        u = "https://" + url
    if u.startswith(("chrome://", "chrome-extension://", "devtools://",
                     "view-source:", "javascript:", "edge://", "about:")
            ) and u != "about:blank":
        raise CDPError(f"Blocked URL: {u}")
    return u


def _validate_upload_paths(paths: list[str]) -> None:
    """File uploads may only touch paths inside the session workspace, a
    mounted context dir, or ~/.ginno — the browser must not become a
    file-permission side door (设计 §6)."""
    from .. import paths as gpaths
    from .builtin import resolve_mounts

    allowed: list[Path] = [gpaths.home()]
    for root, _acc in resolve_mounts(getattr(_validate_upload_paths, "_ctx_dirs", None)):
        allowed.append(root)
    ws = getattr(_validate_upload_paths, "_workspace", None)
    if ws:
        allowed.append(Path(ws).expanduser())
    for p in paths:
        rp = Path(p).expanduser()
        if not rp.is_file():
            raise CDPError(f"路径不存在或不是普通文件: {p}")
        if not any(_inside(rp, root) for root in allowed):
            raise CDPError(
                f"路径 {p} 不在会话可访问的目录内(工作区/挂载目录/~/.ginno)。"
                "浏览器上传不能绕过文件权限;请让用户把文件放进挂载目录。")


def _inside(p: Path, root: Path) -> bool:
    try:
        p.resolve().relative_to(Path(root).expanduser().resolve())
        return True
    except ValueError:
        return False


def _sensitive_guard(cfg, url: str) -> str | None:
    d = cfg.sensitive_match(url)
    if not d:
        return None
    # 用户在 连接器 页面确认过的域名放行(connector config confirmed_domains)
    try:
        from ..connectors import registry as _reg
        confirmed = _reg.registry().read_config("chrome-extension").get(
            "confirmed_domains") or []
    except Exception:  # noqa: BLE001
        confirmed = []
    if d in confirmed:
        return None
    return (f"[error] 该站点({d})在受保护域名列表中(支付/邮箱/云控制台)。"
            "请先用 ask_user 征得用户明确同意;用户在 连接器 页面确认后即可重试。")


def build_browser_tools(session_id: str | None = None,
                        workspace: str | None = None,
                        context_dirs: list[dict] | None = None) -> list:
    """The 16 browser tools, or [] when browser is disabled in settings."""
    cfg = load_browser_config()
    if not cfg.enabled:
        return []
    _validate_upload_paths._workspace = workspace
    _validate_upload_paths._ctx_dirs = context_dirs

    @tool
    async def browser_computer(
        action: str,
        tabId: int,
        coordinate: list[float] | None = None,
        start_coordinate: list[float] | None = None,
        region: list[float] | None = None,
        ref: str | None = None,
        text: str | None = None,
        scroll_direction: str | None = None,
        scroll_amount: int | None = None,
        duration: float | None = None,
        repeat: int | None = None,
        modifiers: str | None = None,
        quality: str | None = None,
    ) -> Any:
        """Use a mouse and keyboard to interact with a web browser tab, and take
        screenshots. Consult a screenshot to determine element coordinates before
        clicking; click the CENTER of elements, not their edges.

        Actions:
        - left_click / right_click / double_click / triple_click: click at
          `coordinate` [x, y] (viewport pixels), or at element center when
          `ref` is given instead (auto-scrolls into view).
        - hover: move the cursor without clicking (reveals tooltips/menus).
        - type: type `text` (usable after clicking a field).
        - key: press keys from `text` — "Enter", "Backspace Backspace",
          shortcuts like "cmd+a"/"ctrl+a"; `repeat` presses the sequence N times.
        - scroll: `scroll_direction` up/down/left/right, `scroll_amount` 1-10
          (default 3) wheel ticks, at `coordinate` (default viewport center).
        - left_click_drag: drag from `start_coordinate` to `coordinate`.
        - screenshot: capture the tab. `quality` low (default — enough for
          navigation and reading UI) / medium (small text) / high (fine detail
          only; bigger and slower).
        - zoom: capture `region` [x0, y0, x1, y1] for close inspection of
          small elements.
        - scroll_to: scroll `ref` into view.
        - wait: sleep `duration` seconds (0-10, default 1).

        `modifiers` for click actions: ctrl/shift/alt/cmd combinable with "+".
        Without a valid tabId, call browser_tabs_context first."""
        if action not in _COMPUTER_ACTIONS:
            return (f'[error] 未知 action "{action}"。可选: ' + ", ".join(_COMPUTER_ACTIONS))
        # argument validation (教学式错误 — 每条都带下一步, 设计 §5.7)
        if action in ("left_click", "right_click", "double_click", "triple_click",
                      "hover") and not coordinate and not ref:
            return (f'[error] computer "{action}" 需要 coordinate [x, y],'
                    "或传 read_page/find 拿到的 ref。")
        if action == "left_click_drag" and (not start_coordinate or not coordinate):
            return '[error] left_click_drag 需要 start_coordinate 和 coordinate 两组 [x, y]。'
        if action == "zoom" and not region:
            return '[error] zoom 需要 region [x0, y0, x1, y1]。'
        if action == "type" and not text:
            return '[error] type 需要 text。'
        if action == "key" and not text:
            return '[error] key 需要 text,如 "Enter" / "cmd+a" / "Backspace Backspace"。'
        if action == "scroll_to" and not ref:
            return "[error] scroll_to 需要 read_page/find 的 ref。"
        if action in ("left_click", "right_click", "double_click",
                      "triple_click", "scroll") and coordinate and len(coordinate) < 2:
            return "[error] coordinate 必须是 [x, y] 两个数。"
        try:
            slow = action in ("screenshot", "zoom")
            return await _dispatch("browser_computer", {
                "action": action, "tabId": tabId,
                **({"coordinate": coordinate} if coordinate else {}),
                **({"start_coordinate": start_coordinate} if start_coordinate else {}),
                **({"region": region} if region else {}),
                **({"ref": ref} if ref else {}),
                **({"text": text} if text is not None else {}),
                **({"scroll_direction": scroll_direction} if scroll_direction else {}),
                **({"scroll_amount": scroll_amount} if scroll_amount else {}),
                **({"duration": duration} if duration is not None else {}),
                **({"repeat": repeat} if repeat else {}),
                **({"modifiers": modifiers} if modifiers else {}),
                **({"quality": quality} if quality else {}),
            }, slow=slow)
        except (CDPError, RelayError) as e:
            return _err("browser_computer", e)

    @tool
    async def browser_read_page(
        tabId: int,
        filter: str = "all",
        depth: int = 15,
        max_chars: int = 50000,
        ref_id: str | None = None,
    ) -> str:
        """Get an accessibility-tree representation of the page with stable
        element references (`ref_N`) usable by browser_computer /
        browser_form_input / browser_file_upload.

        Output is an indented tree, one line per element:
        `[ref_12] button "Sign in" (disabled)`. Limited to max_chars (default
        50000); when truncated, re-read with ref_id to focus a subtree, or
        raise max_chars. filter: "all" (default, depth-capped) or
        "interactive" (buttons/links/inputs only, any depth)."""
        try:
            return await _dispatch("browser_read_page", {
                "tabId": tabId, "filter": filter, "depth": depth,
                "max_chars": max_chars, **({"ref_id": ref_id} if ref_id else {})})
        except (CDPError, RelayError) as e:
            return _err("browser_read_page", e)

    @tool
    async def browser_find(query: str, tabId: int,
                           max_results: int = 20) -> str:
        """Find elements by LITERAL keyword match against element text,
        aria-label, title and role. Does NOT understand synonyms or intent —
        if the page says "Buy Now", searching "purchase button" won't match.
        Use words that actually appear on the element; prefer short specific
        terms. Returns up to max_results refs with scores. No match → fall
        back to browser_read_page and browse directly."""
        try:
            return await _dispatch("browser_find", {
                "query": query, "tabId": tabId, "max_results": max_results})
        except (CDPError, RelayError) as e:
            return _err("browser_find", e)

    @tool
    async def browser_form_input(ref: str, value: Any, tabId: int) -> str:
        """Set a form field's value by element ref (from browser_read_page /
        browser_find). checkbox → boolean; select → option value or visible
        text; other inputs/textarea/contenteditable → string. Dispatches
        framework-compatible events (React/Vue safe). For file inputs use
        browser_file_upload instead."""
        try:
            return await _dispatch("browser_form_input",
                                   {"ref": ref, "value": value, "tabId": tabId})
        except (CDPError, RelayError) as e:
            return _err("browser_form_input", e)

    @tool
    async def browser_navigate(url: str, tabId: int,
                               force: bool = False) -> str:
        """Navigate a tab to an http(s) URL (bare domains get https://
        prefixed), or pass "back"/"forward" for history. If the page shows a
        "Leave site?" dialog (unsaved changes), navigation is BLOCKED and an
        error is returned by default; pass force=true to discard those
        changes. Returns the final URL (redirects noted), title and duration."""
        try:
            guard = _sensitive_guard(load_browser_config(), url)
            if guard and url not in ("back", "forward"):
                return guard
            return await _dispatch("browser_navigate",
                                   {"url": url, "tabId": tabId, "force": force})
        except (CDPError, RelayError) as e:
            return _err("browser_navigate", e)

    @tool
    async def browser_page_text(tabId: int, max_chars: int = 50000) -> str:
        """Extract the raw text of an article-like page (prioritizes article/
        main content over boilerplate). Best for reading posts, docs, news.
        Returns title + URL + plain text (whitespace-collapsed, capped at
        max_chars)."""
        try:
            return await _dispatch("browser_page_text",
                                   {"tabId": tabId, "max_chars": max_chars})
        except (CDPError, RelayError) as e:
            return _err("browser_page_text", e)

    @tool
    async def browser_js(text: str, tabId: int) -> str:
        """Execute JavaScript in the page. No `return` statements — write the
        expression whose value you want (e.g. `window.location.href` or a
        statement body). The last expression's value comes back as text.
        Arbitrary code execution: requires user approval per permission
        settings."""
        try:
            return await _dispatch("browser_js", {"text": text, "tabId": tabId})
        except (CDPError, RelayError) as e:
            return _err("browser_js", e)

    @tool
    async def browser_console(
        tabId: int, pattern: str | None = None,
        onlyErrors: bool = False, clear: bool = False, limit: int = 100,
    ) -> str:
        """Read the tab's console messages (log/warn/error/exceptions),
        buffered passively since attach. ALWAYS pass a `pattern` regex —
        unfiltered buffers are mostly noise. onlyErrors=true keeps error/
        exception rows only; clear=true empties the buffer after reading."""
        try:
            return await _dispatch("browser_console", {
                "tabId": tabId, **({"pattern": pattern} if pattern else {}),
                "onlyErrors": onlyErrors, "clear": clear, "limit": limit})
        except (CDPError, RelayError) as e:
            return _err("browser_console", e)

    @tool
    async def browser_network(
        tabId: int, urlPattern: str | None = None,
        clear: bool = False, limit: int = 100,
    ) -> str:
        """Read the tab's HTTP requests (XHR/fetch/documents/…), buffered
        passively; status appears once responses land; buffer resets on
        cross-domain navigation. Filter with urlPattern (substring, e.g.
        "/api/" or a domain). Useful to verify an action actually hit the
        backend (e.g. after browser_file_upload)."""
        try:
            return await _dispatch("browser_network", {
                "tabId": tabId, **({"urlPattern": urlPattern} if urlPattern else {}),
                "clear": clear, "limit": limit})
        except (CDPError, RelayError) as e:
            return _err("browser_network", e)

    @tool
    async def browser_file_upload(
        paths: list[str], tabId: int,
        ref: str | None = None, triggerRef: str | None = None,
    ) -> str:
        """Select local files into the page's file input, no OS dialog shown.
        Prefer `ref` of the actual input[type=file] (from browser_read_page).
        If only an Upload/Attach button is visible, pass its ref as
        `triggerRef` — the file chooser it opens gets intercepted. `paths`
        must be absolute, existing files inside the session's accessible
        directories (workspace / mounted context folders). The result verifies
        the input actually received the files; use browser_network afterwards
        to confirm the site consumed them."""
        try:
            return await _dispatch("browser_file_upload", {
                "paths": paths, "tabId": tabId,
                **({"ref": ref} if ref else {}),
                **({"triggerRef": triggerRef} if triggerRef else {})}, slow=True)
        except (CDPError, RelayError) as e:
            return _err("browser_file_upload", e)

    @tool
    async def browser_resize_window(width: int, height: int, tabId: int) -> str:
        """Resize the browser window (400-7680 x 300-4320). Useful for
        responsive layouts or standardizing screenshot sizes."""
        try:
            return await _dispatch("browser_resize_window",
                                   {"width": width, "height": height, "tabId": tabId})
        except (CDPError, RelayError) as e:
            return _err("browser_resize_window", e)

    @tool
    async def browser_tabs_context() -> str:
        """List the browser tabs Ginno can drive, with tabId / title / url /
        loading state. Call this FIRST in any browser task — existing tabs may
        belong to other sessions; for a new conversation prefer
        browser_tabs_create for a fresh tab."""
        try:
            return await _dispatch("browser_tabs_context", {})
        except (CDPError, RelayError) as e:
            return _err("browser_tabs_context", e)

    @tool
    async def browser_tabs_create(url: str | None = None) -> str:
        """Open a new empty tab (or navigate it to `url`) in Ginno's browser
        tab group and return its tabId."""
        try:
            return await _dispatch(
                "browser_tabs_create",
                {"url": url} if url else {})
        except (CDPError, RelayError) as e:
            return _err("browser_tabs_create", e)

    @tool
    async def browser_tabs_close(tabId: int) -> str:
        """Close a tab by tabId (only tabs Ginno opened/can drive)."""
        try:
            return await _dispatch("browser_tabs_close", {"tabId": tabId})
        except (CDPError, RelayError) as e:
            return _err("browser_tabs_close", e)

    @tool
    async def browser_handoff(tabId: int, note: str = "") -> str:
        """Pause and hand the tab to the user for something only a human
        should do (login, captcha, payment confirmation). The UI shows a
        takeover card; this call WAITS (up to 5 min) until the user releases
        it. Use when a page asks for credentials/OTP or a sensitive
        confirmation — never type passwords yourself."""
        key = str(tabId)
        ev = asyncio.Event()
        _handoff_events[key] = ev
        try:
            await asyncio.wait_for(ev.wait(), timeout=300)
            return ("用户已完成操作并交回控制权。用 browser_computer 的 "
                    "screenshot action 查看当前页面状态后继续。")
        except TimeoutError:
            return "[error] 等待接管超时(5 分钟)。用户可能不在;请询问后再试。"
        finally:
            _handoff_events.pop(key, None)

    @tool
    async def browser_stop() -> str:
        """Request the in-flight browser action to stop (interrupts typing /
        key-repeat loops at the next character). The user's Stop button does
        the same; call this when the model itself notices a wrong action."""
        global _stop_requested
        _stop_requested = True
        try:
            from ..browser.relay import request_stop
            request_stop()
        except Exception:  # noqa: BLE001
            pass
        return "已请求停止当前浏览器动作。"

    return [
        browser_computer, browser_read_page, browser_find,
        browser_form_input, browser_navigate, browser_page_text,
        browser_js, browser_console, browser_network, browser_file_upload,
        browser_resize_window, browser_tabs_context, browser_tabs_create,
        browser_tabs_close, browser_handoff, browser_stop,
    ]


def release_handoff(tab_id: int | str) -> bool:
    ev = _handoff_events.get(str(tab_id))
    if ev:
        ev.set()
        return True
    return False
