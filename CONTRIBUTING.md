# Contributing to Ginno

> **中文 TL;DR**：欢迎 Issue / PR（中英文均可）。开发环境 `pnpm install` + `uv sync`，跑 `pnpm dev`；
> 提 PR 前请确保 CI 会跑的三件事本地先过：`pytest`（runtime）、`pnpm lint && pnpm typecheck && pnpm test`（web）、`cargo fmt --check`（Rust）。
> 改了 UI 文案必须跑 `make check`（i18n 目录一致性）。发版流程见下方 Release runbook。

## Dev setup

Requirements: **Node ≥ 20 · pnpm ≥ 9 · Python ≥ 3.11 (via uv) · Rust (stable)**.

```bash
git clone https://github.com/robscc/ginno
cd ginno
pnpm install                        # web + desktop shell deps
cd packages/runtime && uv sync      # Python runtime deps
cd ../..

pnpm dev                            # web:3000 + runtime:8787 + Tauri shell, all at once
```

Layout at a glance:

| Path | What |
|---|---|
| `apps/web` | Next.js 14 UI (static export, served by the sidecar in production) |
| `apps/desktop` | Tauri 2 Rust shell |
| `packages/runtime` | Python sidecar (FastAPI + LangGraph), all agent logic |
| `packages/mod-runner`, `crates/mod-broker` | mods compatibility layer |
| `packages/extension` | Chrome extension (browser companion) |
| `docs/` | architecture + one design doc per subsystem |

## Testing

**Python runtime** (`packages/runtime`) — the suite is hermetic: no network,
no API keys, no MCP servers.

```bash
cd packages/runtime
uv run --group test pytest -q                    # everything
uv run --group test pytest -m unit -q            # pure unit tests
uv run --group test pytest -m api -q             # FastAPI integration
uv run --group test pytest -m e2e -q             # full LangGraph + WebSocket
```

Markers: `unit` / `api` / `e2e` — pick the narrowest one that covers your change.
With coverage: add `--cov=ginno_runtime --cov-report=term` (CI uploads to Codecov).

**Web** (`apps/web`) — vitest, node environment, pure-logic tests:

```bash
cd apps/web
pnpm test            # vitest run
pnpm lint && pnpm typecheck
```

**Smoke** — one command proves the whole machine turns over (fake LLM):
server assembly → one chat turn → one workflow run.

```bash
make smoke
```

## Commits & PRs

- Conventional Commits (`feat:`, `fix:`, scope optional — `feat(runtime): …`, `fix(runtime,web): …`).
- Fill in the PR template; link the issue if there is one.
- CI must be green: runtime tests, web lint+typecheck+test+build, Rust fmt+check.
- UI copy changes (any `messages/{en,zh-CN}` or runtime i18n catalog) must pass
  `make check` — en and zh-CN catalogs are diffed key-by-key, including ICU placeholders.

### Packaging gotcha (read before building a .app)

`make app` refuses to run while Ginno is running — rebuilding the PyInstaller
bundle under a live process corrupts lazy imports (`zlib error: incorrect
header check`) and **only a full app restart recovers**. Quit Ginno (⌘Q, not
just closing the window) before building. See `docs/troubleshooting.md`.

## Release runbook (maintainer)

1. Quit the running Ginno app.
2. Bump versions together: `apps/desktop/tauri.conf.json` (`version`),
   `apps/web/package.json`, `packages/runtime/pyproject.toml`.
3. Update `CHANGELOG.md` (Keep a Changelog format; move Unreleased → new version).
4. Commit as `release: vX.Y.Z`, then tag and push:
   ```bash
   git tag vX.Y.Z && git push origin main vX.Y.Z
   ```
5. The `Release` workflow builds the dmg on a macOS runner (ad-hoc signed) and
   attaches it to the GitHub Release. Edit the release notes (bilingual) afterwards.
6. Local builds: `make app` → `apps/desktop/target/release/bundle/dmg/Ginno_*.dmg`
   (self-signed "Ginno Local Code Signing", see `docs/p3-packaging-notes.md`).

## Reporting bugs & proposing features

Use the issue templates (bug report / feature request). For security issues,
**do not open a public issue** — see [SECURITY.md](SECURITY.md).

## License

By contributing you agree that your contributions are licensed under the
[MIT License](LICENSE).
