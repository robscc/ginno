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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from . import agents as agents_reg
from . import paths, usage_store
from . import server_shared as shared
from . import workflows as wf_store
from .hooks.dispatcher import HookDispatcher
from .mcp.registry import MCPRegistry
from .todos import store as todo_store


@asynccontextmanager
async def lifespan(app: FastAPI):
    # _ensure_turn_log / _log / _refresh_session_metas / _reconcile_orphan_runs /
    # _shutdown_run_tasks resolve at call time from the module-level (facade)
    # imports below — the app cannot start before this module fully imports.
    paths.ensure_layout()
    # Attach the trace-file handler up front so pre-turn lifecycle lines
    # (session_create / ws_open / ws_close) land in the log, not just turn lines.
    _ensure_turn_log()
    # Drop usage logs past the retention window (usage-stats-design.md §4.3).
    # Best-effort and cheap (a directory glob); never blocks startup on failure.
    try:
        usage_store.cleanup()
    except Exception:
        _log.exception("usage cleanup failed (continuing)")
    # Best-effort, idempotent move of legacy session files into their per-session
    # dirs. Runs before `yield`, so nothing (uploads/previews/watchers) can race
    # it. Must never block startup on failure.
    try:
        from . import migration as _migration

        _migration.migrate_session_files()
    except Exception:
        _log.exception("session-files migration failed (continuing)")
    shared._hooks = HookDispatcher.from_settings()
    todo_store.ensure_seeded()
    agents_reg.ensure_todo_tools()
    agents_reg.ensure_research_discipline()
    agents_reg.ensure_goal_tools()
    agents_reg.ensure_web_tools()
    # Upgraded installs never got the web tools in permissions.allow (defaults
    # seed only fresh homes) — migrate so they don't fall through to `ask`.
    try:
        from .permission.policy import ensure_web_permissions

        ensure_web_permissions()
    except Exception:
        _log.exception("web permissions migration failed (continuing)")
    wf_store.ensure_seeded()
    # Reconcile workflow runs left "running" by a previous crash/quit: at this
    # point no background task can be alive, so every "running" run is an orphan
    # that would otherwise stay stuck forever. Best-effort; never blocks startup.
    try:
        _reconcile_orphan_runs()
    except Exception:
        _log.exception("run_reconciliation_failed")
    # Heal session metas frozen with a provider/model by older builds (or a
    # config edited outside the UI) so topbar + rebuilt graphs use current config.
    _refresh_session_metas()
    shared._mcp = MCPRegistry()
    shared._mcp.load()
    # Do NOT block port bind on MCP connections. A slow/hung MCP server can
    # take up to its per-server timeout, and the HTTP/WS server (and thus the
    # UI) must come up regardless. Connect in the background; a session created
    # before connections finish simply starts without those tools (the
    # /api/mcp/reload endpoint or a new session picks them up once ready).
    mcp_connect_task = asyncio.create_task(_connect_mcp_background())
    try:
        yield
    finally:
        if not mcp_connect_task.done():
            mcp_connect_task.cancel()
        # Cancel live workflow-run tasks and mark any still-running run
        # interrupted so a clean quit never strands a "running" run. A hard
        # kill -9 skips this; startup reconciliation is the backstop.
        try:
            await _shutdown_run_tasks()
        except Exception:
            _log.exception("run_shutdown_failed")
        if shared._mcp:
            await shared._mcp.close_all()


async def _connect_mcp_background() -> None:
    try:
        await shared._mcp.connect_all()
    except Exception:
        _log.exception("background MCP connect_all failed")


app = FastAPI(title="Ginno Runtime", version="0.1.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


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
from .api import config as _config_api  # noqa: E402
from .api import files as _files_api  # noqa: E402
from .api import knowledge as _knowledge_api  # noqa: E402
from .api import memory as _memory_api  # noqa: E402
from .api import sessions as _sessions_api  # noqa: E402
from .api import stream as _stream_api  # noqa: E402
from .api import todos as _todos_api  # noqa: E402
from .api import usage as _usage_api  # noqa: E402
from .api import workflows as _workflows_api  # noqa: E402

app.include_router(_config_api.router)
app.include_router(_files_api.router)
app.include_router(_knowledge_api.router)
app.include_router(_memory_api.router)
app.include_router(_sessions_api.router)
app.include_router(_stream_api.router)
app.include_router(_todos_api.router)
app.include_router(_usage_api.router)
app.include_router(_workflows_api.router)


@app.get("/api/health")
async def health() -> dict:
    return {"ok": True, "version": "0.1.0"}


@app.post("/api/web/test-search")
async def web_test_search(body: dict) -> dict:
    """Probe a search engine with a neutral query (Settings → Web 搜索)."""
    from .web.config import load_web_config
    from .web.engines import EngineError, search as engine_search

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
    desktop SourcesBlock routes web-citation clicks through the sidecar. The
    same guard as web_fetch applies: http/https + public hosts only, so a
    crafted citation can't launch an internal address.

    Sync (not ``async def``): the DNS guard + browser launch are blocking, so
    FastAPI runs this in the threadpool instead of stalling the event loop
    (and every live WS stream) on a slow/unresolvable host.
    """
    url = (body or {}).get("url") or ""
    if not isinstance(url, str) or not url.strip():
        return {"ok": False, "error": "url required"}
    try:
        import webbrowser

        from .web.fetch import _assert_public_host

        _assert_public_host(url.strip())
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
    _image_block_url,
    _messages_to_ui,
    _run_id_in,
    _tool_args_preview,
    _tool_content_str,
    _truncate_for_ws,
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
    from fastapi.responses import FileResponse, HTMLResponse

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


def main() -> None:
    import os

    import uvicorn

    port = int(os.environ.get("GINNO_RUNTIME_PORT", "8787"))
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
