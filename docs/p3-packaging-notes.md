# P3 Packaging Notes

## Build artifacts

- `apps/desktop/target/release/bundle/macos/Ginno.app` — Tauri shell + bundled `ginno-runtime` (a PyInstaller **onedir** bundle, which also serves the web UI)
- `apps/desktop/target/release/bundle/dmg/Ginno_0.1.0_aarch64.dmg` — installer

`make app` runs the whole pipeline; the steps below are what it does.

## Reproduce

```bash
# 1. Build the web static export (the runtime bundles + serves it)
pnpm --filter @ginno/web build

# 2. Build the Python runtime as a PyInstaller ONEDIR bundle, with the web
#    export embedded as web_out/
cd packages/runtime
OUT=$PWD/../apps/web/out          # NOTE: quote as "${OUT}:web_out" in zsh,
                                  # else `$OUT:web_out` is parsed as a zsh
                                  # variable modifier and silently breaks.
uv run --extra docs pyinstaller --noconfirm --onedir --paths src --name ginno-runtime \
  --collect-all langchain_openai --collect-all langchain_anthropic \
  --collect-all langgraph --collect-all mcp --collect-all pydantic \
  --collect-all pandas --collect-all python_calamine --collect-all openpyxl \
  --collect-all docx --collect-all pptx --collect-all pypdf \
  --add-data "${OUT}:web_out" \
  bin/ginno-runtime.py
# → dist/ginno-runtime/ (executable + _internal/ with the .so/.pyc payload)
#
# Why ONEDIR instead of ONEFILE: --onefile re-extracts ~3000 files to a fresh
# $TMPDIR/_MEIxxxxxx on every launch, and macOS endpoint-security scanning of
# each freshly-written library made cold starts take 15-25s *every time*. The
# onedir layout lives at a stable, signed path inside Ginno.app, so the OS/EDR
# validates the libraries once and caches the verdict — repeat starts drop to
# ~1-2s. (Measured on this machine: onefile heavy-import test 10.5s/run vs
# onedir 0.34s/run after the first.)
#
# `--extra docs` installs the file-parsing deps (pandas/python-docx/python-pptx/
# pypdf/openpyxl/calamine) into the build env. files/extractors.py imports them
# lazily (only when a file is actually parsed), and the `--collect-all <lib>`
# flags bundle them anyway, so nothing heavy needs importing at startup
# (`src/ginno_runtime/_frozen_imports.py` is now an intentional no-op).

# 3. Stage the onedir bundle as a Tauri resource
rm -rf ../apps/desktop/resources/runtime
mkdir -p ../apps/desktop/resources/runtime
cp -R dist/ginno-runtime/ ../apps/desktop/resources/runtime/
# tauri.conf.json lists "resources/runtime/**" under bundle.resources, so the
# folder lands in Ginno.app/Contents/Resources/resources/runtime/ and lib.rs
# launches Contents/Resources/resources/runtime/ginno-runtime directly
# (std::process::Command — no tauri-plugin-shell sidecar).

# 4. Build Tauri app (needs Rust toolchain; beforeBuildCommand rebuilds web)
cd ../apps/desktop
pnpm tauri build
# → target/release/bundle/{macos/Ginno.app, dmg/Ginno_0.1.0_aarch64.dmg}
```

## macOS code signing (required — the white-screen trap)

A build that isn't properly code-signed produces a very confusing failure: the
sidecar starts fine (`curl http://127.0.0.1:8787/` → 200, `~/.ginno/logs/sidecar.log`
shows `Uvicorn running`), yet the window is **blank** and the sidecar log has
**zero** requests from the webview. Cause: an unsigned / only *linker-signed*
bundle fails the signature check that WKWebView's `com.apple.WebKit.Networking`
helper performs on its parent process, so it refuses to issue *any* request on
the webview's behalf — the page never loads `http://127.0.0.1:8787`. The sidecar
is a separate process that doesn't go through WebKit networking, so it keeps
looking healthy. (Confirmed via `sample`/`log stream`: app run-loop idle,
WebCore alive, but no navigation; system log spams
`failed to fetch .../_CodeSignature/CodeRequirements-1 error=-10`.)

`tauri build` only signs if it has an identity; with none configured **and** none
in the keychain it silently leaves the linker signature → white screen. Fix is in
`apps/desktop/tauri.conf.json` (`bundle.macOS`):

- `signingIdentity: "-"` — ad-hoc sign every binary (sidecar, main, `.app`) as
  part of the bundle step, *before* the dmg is built, so the dmg is correct too.
  Override with a real identity via the `APPLE_SIGNING_IDENTITY` env var for
  distribution / notarization.
- `hardenedRuntime: true` + `entitlements: "entitlements.plist"` — the PyInstaller
  sidecar `dlopen`s `libpython3.11.dylib` (and bundled `.so`s) extracted to a temp
  `_MEI*` dir; under the hardened runtime that needs
  `com.apple.security.cs.disable-library-validation` (else
  `[PYI-16724:ERROR] ... different Team IDs` and the sidecar never binds → a
  *second* white-screen mode). `allow-unsigned-executable-memory` / `allow-jit`
  cover Python and the webview's `unsafe-eval`. The plist is non-sandboxed, so no
  network/sandbox keys are needed.

`make app` asserts the produced `.app` is **not** linker-signed and fails loudly
otherwise, so this can't silently regress.

Troubleshooting a `[PYI-16724:ERROR]` on launch after a previously-bad build: a
stale `_MEI*` extraction can outlive its process; clear it
(`rm -rf $TMPDIR/_MEI*`) and any lingering `ginno-runtime` (`pkill -9 -f
ginno-runtime`) before relaunching.

## File drag & drop into the chat (desktop)

The composer uses the **HTML5** drag-and-drop API (`onDrop` → `dataTransfer.files`).
Tauri's default `dragDropEnabled: true` means *"Tauri's internal DnD is on and DOM
DnD is off"* — the webview swallows OS file drops and `dataTransfer.files` is always
empty ([tauri#3558](https://github.com/tauri-apps/tauri/issues/3558), [docs](https://v2.tauri.app/reference/config/)).
`apps/desktop/tauri.conf.json` therefore sets the window's `dragDropEnabled: false`
so drops reach the JS handler. Without this, dragging a file into the packaged app
silently does nothing (it works in dev/browser where the flag doesn't apply).

## How the packaged app serves the UI (same-origin)

The Tauri webview loads `http://127.0.0.1:8787` directly (Tauri `build.frontendDist`
is that URL). The sidecar serves the bundled Next export from the same origin:

- `server.py` resolves the web dir from `sys._MEIPASS/web_out` (frozen) or the
  repo's `apps/web/out` (dev), mounts `/_next` as `StaticFiles`, and a catch-all
  `GET /{path}` maps clean URLs to the exported `*.html` (with an `index.html`
  SPA fallback).
- This avoids the `tauri://localhost` → `http://...` cross-protocol / mixed-content
  block entirely, because the webview and the API share one origin.

### Startup race & splash

Tauri creates the webview before `setup()` runs, so the webview could try
`http://127.0.0.1:8787` before the runtime is listening. `apps/desktop/src/lib.rs`
spawns the runtime in `setup()`, then polls the port for a short grace period
(~3s). Warm starts are ready inside that window, so the webview's initial
navigation succeeds and nothing else happens.

On a cold start (first launch after install/update, while the OS/EDR is still
validating the freshly bundled libraries) the runtime isn't up in time, so
`lib.rs` **navigates the webview to a self-contained splash page** (a `data:`
URL — it needs no server) instead of letting it hit the dead port. The splash
shows a spinner + elapsed time and polls `/api/health` every 300ms; the moment
the runtime responds it does `location.replace("http://127.0.0.1:8787/")` and
the normal UI loads. The Rust side never blocks waiting for readiness, so the
window stays responsive throughout.

Verified in `~/.ginno/logs/sidecar.log`: after `startup complete` the webview
issues `GET /` + every `/_next/*` chunk (200), then the store init
(`/health /agents /sessions /todos /providers /workflows /workflow_runs /artifacts`),
Next RSC prefetches (`/kb.txt?_rsc`, `/settings/model-api.txt?_rsc`), and finally
`WebSocket /ws/sessions/<id> [accepted]` — i.e. the packaged UI hydrates and the
chat socket connects.

### MCP servers connect in the background

`server.py`'s lifespan binds the HTTP port before connecting configured MCP
servers (`connect_all` runs as a background task). A slow or broken MCP server
therefore can't delay the UI; a session created before connections finish just
starts without those tools (`/api/mcp/reload` or a new session picks them up).
`MCPServerConfig.from_dict` also accepts the Claude-style `"type"` key as an
alias for `"transport"`.

## Verified flow

Packaged `.app`: webview loads the UI same-origin and the chat WebSocket connects
(see sidecar log excerpt above). The same code path is shown visually in dev via
`docs/smoke/ui-*.png` (workspace, settings/agents, workflow tab, KB).

Dev (`pnpm dev`): web on `:3000`, sidecar on `:8787`; full ReAct + multi-agent +
widget + TODO + workflow flows verified end-to-end (see `docs/smoke/`).
