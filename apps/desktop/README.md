# @ginno/desktop

Tauri shell — spawns the Python sidecar and hosts the Next.js webview.

## Dev

```bash
pnpm --filter @ginno/desktop tauri dev
```

Requires:
- Rust toolchain (`rustup`)
- The Next.js web app on `:3000` (Tauri auto-runs `beforeDevCommand`)
- The Python sidecar on `:8787` (`pnpm dev:runtime` in another terminal)

## Production build

```bash
# 1. Build the Python sidecar binary
pnpm build:runtime

# 2. Copy it into the binaries dir with target-triple suffix
cp packages/runtime/dist/ginno-runtime apps/desktop/binaries/ginno-runtime-$(rustc -vV | grep host | awk '{print $2}')

# 3. Build the desktop app
pnpm --filter @ginno/desktop tauri build
```

Outputs land in `apps/desktop/target/release/bundle/`.

## Architecture

```
Tauri (Rust)
  ├── spawns ginno-runtime (Python sidecar)  [release only]
  ├── serves ../web/out as webview           [release]
  └── loads http://localhost:3000 in dev     [dev]
```

Frontend talks to the sidecar via HTTP/WebSocket on `127.0.0.1:8787`.
The port is exposed to JS via the `sidecar_port()` Tauri command.
