"""FastAPI sidecar: HTTP REST + WebSocket for the desktop UI.

Streams LangGraph events over WebSocket: token deltas, tool start/end,
permission requests (HITL), and final message boundaries.

Structure: this module is the app shell (app object, lifespan, CORS, static
UI mount, main). Endpoint domains live in ``ginno_runtime/api/`` routers;
process-wide mutable state lives in ``server_shared``; session-index helpers
live in ``session_meta``. The many re-exports at the bottom keep historical
``server.<name>`` references (tests, frozen builds) working.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import asynccontextmanager
from urllib.parse import parse_qs

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import lang, paths
from . import startup_tasks
from ._version import __version__

# 壳 pid 看守 / 后台 MCP 连接两个纯 lifespan 辅助已随启动任务表迁至
# startup_tasks.py（2026-11 批次 7 表化）；re-export 保住历史
# ``server.<name>`` 引用（tests/unit/test_shell_watchdog.py 等）。
from .startup_tasks import (  # noqa: F401
    _connect_mcp_background,
    _watch_shell_pid,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown 任务表驱动（批次 7 表化，见 startup_tasks.py）。

    启动/关闭的每个步骤（原约 330 行平铺 try 块 + create_task）现在住在
    :data:`startup_tasks.STARTUP_TASKS` / :data:`startup_tasks.SHUTDOWN_TASKS`
    声明表里，由 :func:`startup_tasks.execute_phase` 按表序执行、按
    fail_policy 分流。表序即原平铺顺序（逐块一一对应，守卫测试钉死）；
    本函数只是装载表 + 执行器调用的薄壳。

    语义要点（与原平铺代码一致）：startup 段任一 abort 任务失败 → 异常
    向上传播，``yield`` 不执行、下方 finally（shutdown 表）**不运行**
    （try 尚未进入）；shutdown 段仅在 yield 之后运行，abort 任务失败在
    finally 内抛出。任务间句柄（MCP connect / scheduler / devswap /
    shell watch / lastwords recover）经 :class:`startup_tasks.LifespanHandles`
    跨两阶段传递。
    """
    handles = startup_tasks.LifespanHandles()
    await startup_tasks.execute_phase(
        startup_tasks.STARTUP_TASKS, startup_tasks.PHASE_STARTUP, handles
    )
    try:
        yield
    finally:
        await startup_tasks.execute_phase(
            startup_tasks.SHUTDOWN_TASKS, startup_tasks.PHASE_SHUTDOWN, handles
        )


app = FastAPI(title="Ginno Runtime", version=__version__, lifespan=lifespan)


@app.exception_handler(StarletteHTTPException)
async def _unwrap_structured_http_error(request, exc: StarletteHTTPException):
    """Return a structured ``detail`` dict as the response body itself.

    Handlers raise ``HTTPException(404, detail={"ok": False, "error": ...})`` so
    the wire shape matches the ``{ok:false, ...}`` house style instead of
    FastAPI's ``{"detail": {...}}`` wrapper (API-05). String details keep the
    default ``{"detail": "..."}`` shape — existing clients read it.
    """
    if isinstance(exc.detail, dict):
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.detail,
            headers=getattr(exc, "headers", None),
        )
    return await http_exception_handler(request, exc)


# P0-1 (security-audit 2026-11-12): loopback origin allowlist — the previous
# ``allow_origins=["*"]`` let ANY web page read cross-origin responses from
# 8787. The packaged webview is same-origin (it loads http://127.0.0.1:8787
# directly) and dev runs on :3000 — those are the only origins that ever
# needed CORS. Everything else is blocked one layer out by
# OriginGuardMiddleware (mounted below, outermost).
from .origin_guard import (  # noqa: E402
    DEFAULT_ALLOWED_ORIGINS,
    LOOPBACK_ORIGIN_RE,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(DEFAULT_ALLOWED_ORIGINS),
    # dev web (:3000) and packaged e2e sidecars (bind(0) random port) fetch
    # cross-origin from loopback hosts — same loopback-any-port rule as the
    # guard; a browser cannot label a remote page with a loopback origin.
    allow_origin_regex=LOOPBACK_ORIGIN_RE.pattern,
    allow_methods=["*"],
    allow_headers=["*"],
)


class SecurityHeadersMiddleware:
    """Baseline hardening headers for every response (SEC-02).

    The app is local-only, but the WebView is a browser: nosniff keeps a
    renamed upload from being interpreted as script, no-referrer stops file
    paths leaking to external links, and SAMEORIGIN blocks framing. CSP is
    deliberately NOT added here — the app's inline bootstrap needs its own
    policy (``/api/code/file`` already ships one for generated HTML).
    """

    HEADERS = (
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (b"x-frame-options", b"SAMEORIGIN"),
    )

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def _send(message):
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers") or [])
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in self.HEADERS if k not in present)
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, _send)


app.add_middleware(SecurityHeadersMiddleware)


class RequestLocaleMiddleware:
    """Bind the request locale from ``X-Ginno-Language`` (i18n-design.md §5).

    Pure-ASGI (deliberately not BaseHTTPMiddleware) so the contextvar is set
    in the SAME task that runs the endpoint: for websocket scopes the binding
    wraps the whole connection, and every task the session endpoint spawns
    mid-connection (turn jobs via ``asyncio.create_task``, ``spawn_bg``)
    inherits it through context copy — the locale stays effective for entire
    turns, including work scheduled later in the connection. Valid values are
    ``en`` / ``zh-CN``; anything else (or nothing sent) binds nothing and
    :func:`lang.current_locale` falls back to settings resolution. Since the
    browser WebSocket API cannot send custom headers, a websocket/http scope
    WITHOUT the header falls back to the ``lang`` query parameter (same
    validation).
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        token = None
        for name, value in scope.get("headers") or []:
            if name == b"x-ginno-language":
                token = lang.bind_request_locale(value.decode("latin-1").strip())
                break
        if token is None:
            # Browser WebSocket API cannot send custom headers — the web side
            # falls back to a ``?lang=`` query parameter (same normalization/
            # validation: only en / zh-CN bind, anything else is ignored and
            # :func:`lang.current_locale` resolves from settings).
            params = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
            values = params.get("lang")
            if values:
                token = lang.bind_request_locale(values[0])
        if token is None:
            await self.app(scope, receive, send)
            return
        try:
            await self.app(scope, receive, send)
        finally:
            lang.reset_request_locale(token)


app.add_middleware(RequestLocaleMiddleware)

# settings-integrity 硬线（2026-11-22 复核补强）：settings.json 损坏期间
# 除「受限恢复 UI」白名单（/api/health、/api/settings* 读写面、静态资产）
# 外的一切 HTTP → 503、一切 WebSocket → close(1013)——turn/调度/渠道/
# A2A/文件操作全部拒发。动态判态，repair 落盘即自动解除。startup 侧的
# 执行/恢复任务跳过线在 startup_tasks.py（skip_when_settings_corrupt）。
from .settings_gate import SettingsIntegrityGate  # noqa: E402

app.add_middleware(SettingsIntegrityGate)

# P0-1 (security-audit 2026-11-12): loopback origin guard — outermost layer.
# Blocks browser-driven requests (fetch/WS always carry a forge-proof Origin)
# whose origin is not the app's own webview/sidecar/dev origins, while
# non-browser local clients (curl, harness, rtk, e2e drivers — no Origin
# header) pass. chrome-extension:// is allowed for the companion relay.
from .origin_guard import OriginGuardMiddleware  # noqa: E402

app.add_middleware(OriginGuardMiddleware)


# ---- serve the web UI from the sidecar (same origin as the API) ----
# In the packaged app the Tauri webview loads http://127.0.0.1:8787 directly,
# which avoids the tauri:// -> http cross-origin / mixed-content block and the
# startup race. The Next static export is bundled into the binary (web_out/) by
# PyInstaller; in dev we fall back to the repo's apps/web/out.
import sys as _sys  # noqa: E402
from pathlib import Path as _Path  # noqa: E402


def _web_out_dir() -> _Path | None:
    meipass = getattr(_sys, "_MEIPASS", None)
    if meipass:
        p = _Path(meipass) / "web_out"
    else:
        p = _Path(__file__).resolve().parents[4] / "apps" / "web" / "out"
    return p if p.exists() else None


WEB_OUT = _web_out_dir()

if WEB_OUT is not None and (WEB_OUT / "_next").exists():
    from starlette.staticfiles import StaticFiles as _StaticFiles

    app.mount("/_next", _StaticFiles(directory=str(WEB_OUT / "_next")), name="next-static")


# ---- API routers (per-domain modules under ginno_runtime/api/) ------------
# Included before the catch-all web route at the bottom of this module.
from .api import badge as _badge_api  # noqa: E402
from .api import code as _code_api  # noqa: E402
from .api import code_fsops as _code_fsops_api  # noqa: E402
from .api import config as _config_api  # noqa: E402
from .api import env_settings as _env_settings_api  # noqa: E402
from .api import connectors as _connectors_api  # noqa: E402
from .api import devswap as _devswap_api  # noqa: E402
from .api import doctor as _doctor_api  # noqa: E402
from .api import diagnostics as _diagnostics_api  # noqa: E402
from .api import distill as _distill_api  # noqa: E402
from .api import export as _export_api  # noqa: E402
from .api import files as _files_api  # noqa: E402
from .api import tasks as _tasks_api  # noqa: E402
from .api import folders as _folders_api  # noqa: E402
from .api import knowledge as _knowledge_api  # noqa: E402
from .api import instincts as _instincts_api  # noqa: E402
from .api import mcp_apps as _mcp_apps_api  # noqa: E402
from .api import memory as _memory_api  # noqa: E402
from .api import plans as _plans_api  # noqa: E402
from .api import schedule as _schedule_api  # noqa: E402
from .api import search as _search_api  # noqa: E402
from .api import sessions as _sessions_api  # noqa: E402
from .api import stream as _stream_api  # noqa: E402
from .api import tasks_counts as _tasks_counts_api  # noqa: E402
from .api import tasks_overview as _tasks_overview_api  # noqa: E402
from .api import team_generate as _team_gen_api  # noqa: E402
from .api import team_templates as _team_tpl_api  # noqa: E402
from .api import teams as _teams_api  # noqa: E402
from .api import todos as _todos_api  # noqa: E402
from .api import usage as _usage_api  # noqa: E402
from .api import workflows as _workflows_api  # noqa: E402
from .mods import api as _mods_api  # noqa: E402

app.include_router(_code_api.router)
app.include_router(_diagnostics_api.router)
app.include_router(_doctor_api.router)
app.include_router(_devswap_api.router)
app.include_router(_search_api.router)
app.include_router(_export_api.router)
app.include_router(_distill_api.router)
app.include_router(_badge_api.router)
app.include_router(_connectors_api.router)
app.include_router(_code_fsops_api.router)
app.include_router(_config_api.router)
app.include_router(_env_settings_api.router)
app.include_router(_plans_api.router)
app.include_router(_files_api.router)
app.include_router(_tasks_overview_api.router)
app.include_router(_tasks_counts_api.router)
app.include_router(_tasks_api.router)
app.include_router(_folders_api.router)
app.include_router(_knowledge_api.router)
app.include_router(_instincts_api.router)
app.include_router(_mcp_apps_api.router)
app.include_router(_memory_api.router)
app.include_router(_mods_api.router)
app.include_router(_schedule_api.router)
app.include_router(_sessions_api.router)
app.include_router(_stream_api.router)
app.include_router(_teams_api.router)
app.include_router(_team_gen_api.router)
app.include_router(_team_tpl_api.router)
app.include_router(_todos_api.router)
app.include_router(_usage_api.router)
app.include_router(_workflows_api.router)


@app.get("/api/health")
async def health() -> dict:
    from . import diagnostics, runtime_watch
    from ._build import build_info

    return {
        "ok": True,
        "version": __version__,
        "build": build_info(),
        **runtime_watch.status(),
        **diagnostics.startup_summary(),
    }


@app.post("/api/web/test-search")
async def web_test_search(body: dict) -> dict:
    """Probe a search engine with a neutral query (Settings → Web 搜索)."""
    from .web.config import load_web_config
    from .web.engines import EngineError
    from .web.engines import search as engine_search

    cfg = load_web_config()
    engine = ((body or {}).get("engine") or cfg.default_engine or "").strip()
    try:
        hits = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: engine_search("ginno", engine, cfg.engine_cfg(engine), cfg.timeout_s, 3),
        )
        return {"ok": True, "results": len(hits)}
    except (EngineError, Exception) as e:  # noqa: BLE001 — surface as payload
        if isinstance(e, EngineError):
            return {"ok": False, "error": str(e)}
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


@app.post("/api/open-external")
def open_external(body: dict) -> dict:
    """Open a URL in the system browser (citations-design.md §5.7).

    WKWebView won't hand external links to the OS browser on its own, so the
    desktop SourcesBlock routes web-citation clicks through the sidecar.
    Guard = ``_assert_openable_host``: scheme + literal-IP/localhost checks,
    NO DNS probe — the browser resolves under its own proxy rules, and a
    local fake-IP resolver (Clash/Surge 198.18.0.0/15) must not make every
    external link read as reserved (2026-11-03: all link clicks dead).

    Sync (not ``async def``): the guard + browser launch are blocking, so
    FastAPI runs this in the threadpool instead of stalling the event loop
    (and every live WS stream) on a slow/unresolvable host.
    """
    url = (body or {}).get("url") or ""
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "url required"}
    try:
        import webbrowser

        from .web.fetch import _assert_openable_host

        _assert_openable_host(url.strip())
        webbrowser.open(url.strip())
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ---- compatibility facade -------------------------------------------------
# Historical call sites (tests, tooling) import these names from this module.
# They live in server_shared / session_meta / api.* now; re-exported here so
# ``from ginno_runtime.server import <name>`` keeps working.
from .api.config import (  # noqa: E402, F401
    PutProvidersRequest,
    _agent_lookup,
    _refresh_session_metas,
)
from .api.files import (  # noqa: E402, F401
    UPLOAD_MAX_BYTES,
    _attachment_headers,
    _compact_schema,
    _dir_stats,
    _heal_artifact_ref,
    _heal_workspace_ref,
    _is_orphaned_session,
    _normalize_file_ref,
    _register_artifact_file,
    _safe_upload_name,
    _session_file_guard,
    _session_workspace,
    _unique_dest,
)
from .api.messages_ui import (  # noqa: E402, F401
    LEGACY_WS_UPDATE_MARKERS,
    TOOL_OUTPUT_WS_LIMIT,
    _ai_content_blocks,
    _content_ui_blocks,
    _human_ui_blocks,
    _image_block_url,
    _messages_to_ui,
    _run_id_in,
    _tool_args_preview,
    _tool_content_str,
    _truncate_for_ws,
    parse_skill_wrap,
    skill_display_text,
)
from .api.sessions import (  # noqa: E402, F401
    GOAL_GRACE_S,
    CreateSessionRequest,
    PatchSessionRequest,
    _agent_icon,
    _default_title,
    _emit_goal_event,
    _ensure_session,
    _first_agent_id,
    _goal_agent,
    _goal_error_status,
    _goal_slug,
    _resolve_provider_model,
    _start_goal_driver,
    _stop_goal_driver,
    _turn_last_error,
)
from .api.stream import (  # noqa: E402, F401
    CHUNK_TIMEOUT_S,
    _resolve_attached_files,
    _run_resume,
    _run_stream,
    _stream_graph,
    _tool_file_effects,
)
from .api.workflows import (  # noqa: E402, F401
    _reconcile_orphan_runs,
    _remove_run_artifacts,
    _run_checkpoint_path,
    _run_workflow_bg,
    _set_run_status,
    _shutdown_run_tasks,
    _shutdown_synth_tasks,
    _spawn_run_task,
    _wf_build_deps,
)

# build_model moved out of this module's code paths (sessions/workflows import
# it directly), but the name stays for reference compatibility.
from .models import build_model  # noqa: E402, F401
from .server_shared import (  # noqa: E402, F401
    _GOAL_DRIVERS,
    _PENDING_RESUME,
    _RUNNING_TURNS,
    _SESSION_WS,
    _SESSIONS,
    _TURN_LOCKS,
    _USAGE_BY_SESSION,
    _WF_RUN_TASKS,
    _WS_SEND_TIMEOUT_S,
    _ensure_turn_log,
    _ev,
    _log,
    _push_global_event,
    _push_session_event,
    _try_send,
    _turn_lock,
)
from .session_meta import (  # noqa: E402, F401
    _find_meta,
    _resolve_session_meta,
    _session_meta_list,
    _session_meta_patch,
    _session_meta_remove,
    _session_meta_upsert,
    _session_slug,
)


@app.get("/{full_path:path}")
async def _serve_web(full_path: str):
    """Serve the bundled Next static export (same origin as the API)."""
    from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

    # NEW-01 (harness run-20261118): an unknown ``/api/*`` path must fall into
    # the API error contract (404 JSON), never into the SPA fallback below —
    # the static catch-all used to swallow it with 200 + index.html, so a
    # frontend fetch got HTML where it expected JSON (parse error masking the
    # real 404) and boundary probes couldn't tell broken endpoints from real
    # pages. Registered API routes win over this catch-all by routing order,
    # so only genuinely unknown endpoints land here.
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse(
            status_code=404,
            content={"ok": False, "error": f"unknown API endpoint: /{full_path}"},
        )
    if WEB_OUT is None:
        return HTMLResponse("frontend not bundled", status_code=404)
    # HTML entry points must never be cached: they reference content-hashed
    # /_next chunks, and a heuristically-cached stale page (WKWebView) would
    # keep the webview on an old frontend build after a rebuild. The chunks
    # themselves are immutable (hashed names) and cache freely. The route .txt
    # RSC payloads are no-store too: they are regenerated per build under the
    # SAME name (no content hash in the filename).
    no_store = {"Cache-Control": "no-store"}
    root = WEB_OUT.resolve()

    def _safe(rel: str) -> _Path | None:
        """Resolve ``rel`` inside WEB_OUT only (guards %-decoded traversal)."""
        try:
            f = (WEB_OUT / rel).resolve()
        except OSError:
            return None
        return f if f.is_file() and f.is_relative_to(root) else None

    p = full_path.strip("/")
    if p == "":
        return FileResponse(WEB_OUT / "index.html", headers=no_store)
    # Exact files shipped with the export — critically the route ``.txt`` RSC
    # payloads (workflows.txt, settings/*.txt, …). The App Router fetches them
    # for soft navigation; answering with index.html instead forces a FULL page
    # reload on every menu switch, wiping all in-memory UI state (in-flight
    # summarize loading, open modals, chat drafts).
    exact = _safe(p)
    if exact is not None:
        headers = no_store if p.endswith((".html", ".txt")) else None
        return FileResponse(exact, headers=headers)
    cand = _safe(p + ".html")
    if cand is not None:
        return FileResponse(cand, headers=no_store)
    idx = _safe(p + "/index.html")
    if idx is not None:
        return FileResponse(idx, headers=no_store)
    return FileResponse(WEB_OUT / "index.html", headers=no_store)  # SPA fallback


def ensure_sole_runtime(port: int, *, timeout: float = 1.5) -> None:
    """单实例守卫（2026-11-19 启动序修复）：端口已被占 → 打印持有者并退出。

    为什么必须在 main() 最早、uvicorn/app 构造之前：uvicorn 的 lifespan
    （孤儿收编/任务收编/订阅重建等共享状态操作）先于绑定失败暴露
    （2026-11-19 实锤三次：本地 e2e 误启第二 runtime 实例，它看不到主
    实例内存运行态，把磁盘 meta 里 running 的子代理误判孤儿 settle 成
    stopped，随后才 Errno 48 退出——退出前已污染共享状态，主实例子代理
    无辜被杀）。守卫用 connect 探测：端口有监听者即打印其健康指纹并以
    退出码 3 退出，**任何会话状态一行都不碰**；端口空闲则原样放行。
    """
    import json as _json
    import socket as _socket
    import urllib.request as _urlreq

    probe = _socket.socket()
    probe.settimeout(timeout)
    try:
        probe.connect(("127.0.0.1", port))
    except OSError:
        return  # 端口无人监听 → 本实例是唯一 runtime，正常启动
    finally:
        probe.close()
    holder = ""
    try:
        with _urlreq.urlopen(
            f"http://127.0.0.1:{port}/api/health", timeout=timeout
        ) as r:
            h = _json.loads(r.read())
        b = h.get("build") or h.get("build_fingerprint") or h.get("fingerprint")
        if isinstance(b, dict):
            b = b.get("fingerprint") or b
        holder = f"（build={b} version={h.get('version')}）"
    except Exception:  # noqa: BLE001 — 拿不到持有者信息不拦退出
        holder = "（持有者健康信息不可得）"
    print(
        f"[ginno-runtime] 127.0.0.1:{port} 已被另一个 Ginno runtime 占用"
        f"{holder}，本实例直接退出、不触碰任何会话状态（单实例守卫）。",
        file=sys.stderr,
        flush=True,
    )
    raise SystemExit(3)


def main() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("GINNO_RUNTIME_PORT", "8787"))
    # 单实例守卫先于一切（含 app/lifespan 构造——孤儿收编等共享状态操作
    # 都在 lifespan 里，绑定失败在他们之后才暴露；详见函数 docstring）。
    ensure_sole_runtime(port)
    # timeout_graceful_shutdown：SIGTERM 后对 in-flight 连接/任务的等待设 3s
    # 上限（uvicorn 默认无限等——2026-11-04 实锤：优雅关闭卡死在 "Waiting
    # for background tasks to complete"，壳的 SIGTERM-first 6s 预算每次等满才
    # SIGKILL，退出从瞬间变 ~7s，且 lifespan finally（stop_all 后台命令/MCP
    # close/Chrome 随退）从未执行过）。到期 uvicorn 取消剩余任务并**继续走
    # lifespan shutdown** → finally 真正跑、进程 ~3s 内自然退出；壳的 6s 预算
    # 退化为纯兜底。dev 的 uv run uvicorn 同参数见 sidecar.rs spawn 梯子。
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=port,
        log_level="info",
        timeout_graceful_shutdown=3,
    )
