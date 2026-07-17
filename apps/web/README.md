# @ginno/web

Next.js UI for Ginno. Static export — served by the Tauri webview in
release, by `next dev` on `:3000` in development.

## Develop

```bash
pnpm --filter @ginno/web dev
```

The chat panel talks to the Python sidecar on `127.0.0.1:8787`
(`pnpm dev:runtime` in another terminal). If the sidecar is down, the
UI shows a "disconnected" indicator — it does not block typing.

## Build (static export)

```bash
pnpm --filter @ginno/web build
# → ./out/
```

## Pages (P0 done, rest pending)

- [x] `/` — Chat (skeleton, WS echo only)
- [ ] `/sessions` — session list + resume + time-travel
- [ ] `/skills` — skill browser + editor
- [ ] `/mcp` — MCP server registry
- [ ] `/memory` — MEMORY.md + entries
- [ ] `/settings` — settings.json editor (hooks/permissions/env/model)
