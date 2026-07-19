# P3 Packaging Notes

## Build artifacts

- `apps/desktop/target/release/bundle/macos/Ginno.app` — Tauri shell + bundled `ginno-runtime` sidecar (which also serves the web UI)
- `apps/desktop/target/release/bundle/dmg/Ginno_0.1.0_aarch64.dmg` — installer

## Reproduce

```bash
# 1. Build the web static export (the sidecar bundles + serves it)
pnpm --filter @ginno/web build

# 2. Build the Python sidecar, bundling the web export as web_out/
cd packages/runtime
OUT=$PWD/../apps/web/out          # NOTE: quote as "${OUT}:web_out" in zsh,
                                  # else `$OUT:web_out` is parsed as a zsh
                                  # variable modifier and silently breaks.
uv run pyinstaller --onefile --paths src --name ginno-runtime \
  --collect-all langchain_openai --collect-all langchain_anthropic \
  --collect-all langgraph --collect-all mcp --collect-all pydantic \
  --add-data "${OUT}:web_out" \
  bin/ginno-runtime.py
# → dist/ginno-runtime (~37MB Mach-O arm64)

# 3. Place as Tauri sidecar with target-triple suffix
TRIPLE=$(rustc -vV | grep host | awk '{print $2}')
cp dist/ginno-runtime ../apps/desktop/binaries/ginno-runtime-$TRIPLE

# 4. Build Tauri app (needs Rust toolchain; beforeBuildCommand rebuilds web)
cd ../apps/desktop
pnpm tauri build
# → target/release/bundle/{macos/Ginno.app, dmg/Ginno_0.1.0_aarch64.dmg}
```

## How the packaged app serves the UI (same-origin)

The Tauri webview loads `http://127.0.0.1:8787` directly (Tauri `build.frontendDist`
is that URL). The sidecar serves the bundled Next export from the same origin:

- `server.py` resolves the web dir from `sys._MEIPASS/web_out` (frozen) or the
  repo's `apps/web/out` (dev), mounts `/_next` as `StaticFiles`, and a catch-all
  `GET /{path}` maps clean URLs to the exported `*.html` (with an `index.html`
  SPA fallback).
- This avoids the `tauri://localhost` → `http://...` cross-protocol / mixed-content
  block entirely, because the webview and the API share one origin.

### Startup race

Tauri creates the webview before `setup()` runs, so the webview could try
`http://127.0.0.1:8787` before the sidecar is listening. `apps/desktop/src/lib.rs`
spawns the sidecar in `setup()` and then **blocks on a `TcpStream::connect_timeout`
poll** (up to ~20s) until the port accepts, so the sidecar is ready by the time the
webview navigates. Verified in `~/.ginno/logs/sidecar.log`: after `startup complete`
the webview issues `GET /` + every `/_next/*` chunk (200), then the store init
(`/health /agents /sessions /todos /providers /workflows /workflow_runs /artifacts`),
Next RSC prefetches (`/kb.txt?_rsc`, `/settings/model-api.txt?_rsc`), and finally
`WebSocket /ws/sessions/<id> [accepted]` — i.e. the packaged UI hydrates and the
chat socket connects.

## Verified flow

Packaged `.app`: webview loads the UI same-origin and the chat WebSocket connects
(see sidecar log excerpt above). The same code path is shown visually in dev via
`docs/smoke/ui-*.png` (workspace, settings/agents, workflow tab, KB).

Dev (`pnpm dev`): web on `:3000`, sidecar on `:8787`; full ReAct + multi-agent +
widget + TODO + workflow flows verified end-to-end (see `docs/smoke/`).
