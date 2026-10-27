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

## Lint policy

`next lint` runs as part of `next build`, so every warning below shows up in
`make app` output — keep the build warning-free.

- `@next/next/no-img-element` is **off** project-wide (`.eslintrc.json`). The app
  is a static export with `images: { unoptimized: true }` served from the local
  Tauri webview, and the `<img>` sites are user/attachment images (markdown
  URLs, `data:`/`blob:` previews, arbitrary dimensions) plus the local app icon.
  `next/image` would add required width/height constraints (or `fill` wrappers)
  without any optimization benefit in a local desktop app.
- `react-hooks/exhaustive-deps` stays **on**. Fix real dependency bugs; if an
  effect is intentionally run-once, use `// eslint-disable-next-line
  react-hooks/exhaustive-deps` with a one-line reason (see `MonacoEditor.tsx`).

## Pages (P0 done, rest pending)

- [x] `/` — Chat (skeleton, WS echo only)
- [ ] `/sessions` — session list + resume + time-travel
- [ ] `/skills` — skill browser + editor
- [ ] `/mcp` — MCP server registry
- [ ] `/memory` — MEMORY.md + entries
- [ ] `/settings` — settings.json editor (hooks/permissions/env/model)
