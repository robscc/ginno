# P3 Packaging Notes

## Build artifacts

- `apps/desktop/target/release/bundle/macos/Ginno.app` — 48MB, Tauri shell + bundled `ginno-runtime` sidecar
- `apps/desktop/target/release/bundle/dmg/Ginno_0.1.0_aarch64.dmg` — 40MB installer

## Reproduce

```bash
# 1. Build Python sidecar
cd packages/runtime
uv run pyinstaller --onefile --paths src --name ginno-runtime \
  --collect-all langchain_openai --collect-all langchain_anthropic \
  --collect-all langgraph --collect-all mcp --collect-all pydantic \
  bin/ginno-runtime.py
# → dist/ginno-runtime (35MB Mach-O arm64)

# 2. Place as Tauri sidecar with target-triple suffix
TRIPLE=$(rustc -vV | grep host | awk '{print $2}')
cp packages/runtime/dist/ginno-runtime apps/desktop/binaries/ginno-runtime-$TRIPLE

# 3. Build Tauri app (needs Rust toolchain)
cd apps/desktop
pnpm tauri build
# → target/release/bundle/{macos/Ginno.app, dmg/Ginno_0.1.0_aarch64.dmg}
```

## What works in the packaged app

- Tauri shell launches and renders the embedded Next.js static export.
- Tauri spawns the `ginno-runtime` sidecar via `externalBin` — confirmed in
  `~/.ginno/logs/sidecar.log` and `ps -p` showing the binary running.
- Sidecar connects to MCP stdio servers (env inherited from parent so
  `npx` is found).
- All REST endpoints (`/health`, `/sessions`, `/mcp`, `/skills`) respond.

## Known limitation (P4)

The Tauri webview runs at the `tauri://localhost` origin. Fetching
`http://127.0.0.1:8787/...` from that origin is blocked by the macOS
WKWebView cross-protocol restriction — even with `connect-src` whitelisted
in `app.security.csp`. Symptom: webview's `listSessions()` (GET) reaches
the sidecar (200 OK), but `createSession()` (POST with JSON body) never
fires.

### Workarounds (pick one for P4)

1. **Serve the static export from the sidecar itself** — set Tauri's
   `frontendDist` to `http://127.0.0.1:8787` and have FastAPI mount
   `apps/web/out/` as StaticFiles. Same-origin, no CSP issue. The webview
   shows a brief "loading" while the sidecar boots.

2. **Tauri command bridge** — instead of HTTP, route sidecar calls
   through Tauri's `invoke()` Rust bridge. The Rust side proxies to the
   sidecar's localhost port. Bypasses WKWebView entirely.

3. **Use `tauri-plugin-http`** — official plugin that allows fetch from
   the webview to arbitrary origins including localhost.

Option 1 is the simplest. Option 2 matches AgentScope 2.0's app/
architecture. Option 3 is the least code.

## Verified flow (without Tauri webview)

Serving `apps/web/out/index.html` via `python3 -m http.server 5173` and
the runtime via `uvicorn ... :8787`, the full ReAct round-trip works:
chat → qwen3.7-plus streams tokens → calls MCP `read_text_file` →
permission prompt → Allow → tool executes → assistant summarizes. See
`docs/smoke/ginno-smoke-p3-static-frontend.png`.

The packaged `.app` launches and spawns the sidecar correctly; only the
webview ↔ sidecar HTTP bridge is blocked by the cross-protocol
limitation noted above.
