# Ginno

Personal AI Agent — desktop app inspired by Claude Code, built on LangGraph.

## Stack

- **Shell**: Tauri (Rust + native webview)
- **UI**: Next.js (static export) + shadcn/ui
- **Runtime**: Python + LangGraph + FastAPI (sidecar, bundled via PyInstaller)
- **Storage**: local files under `~/.ginno/` (no database)
- **Workspace**: user projects live in `~/workspace/<proj>/`; agent metadata under `~/.ginno/projects/<slug>/`

## Repo layout

```
ginno/
├── apps/
│   ├── desktop/        # Tauri shell: spawns Python sidecar, hosts webview
│   └── web/            # Next.js UI (static export)
├── packages/
│   └── runtime/        # Python: FastAPI + LangGraph + Skills/MCP/Hooks/Permissions
├── docs/               # architecture.md + subsystem design docs
└── scripts/
    └── dev.sh          # run all three processes in dev
```

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│  Tauri shell (Rust)  →  WKWebView                        │
│    / chat · /workflows Studio · /kb · /pin · /settings   │
│    Studio: left = recipes + runs + decision inbox;       │
│      centre = design canvas / run observer /             │
│      supervisor console / versions;                      │
│      right = node inspector / run context                │
└───────────────────────────┬──────────────────────────────┘
                            ▼  http://127.0.0.1:8787 (same-origin)
┌──────────────────────────────────────────────────────────┐
│  Python sidecar — FastAPI (:8787)                        │
│   • serves Next.js static export + REST /api/**          │
│   • WS /api/ws/sessions/{sid}  session/turn stream       │
│       → chat workspace run cards (turn stream)           │
│   • WS /api/ws/runs/{run_id}   run snapshot replay       │
│       + live push → Studio observer / decision inbox     │
└───────────────────────────┬──────────────────────────────┘
                            ▼
        ~/.ginno/  (all state: plain files, no DB)
```

Both WebSocket channels are served same-origin by the sidecar: the session
channel drives the chat workspace, the run channel (snapshot replay + live
push) drives the Studio's run observer and supervisor decision inbox — it is
also the only live channel for headless runs. Full details:
[`docs/architecture.md`](docs/architecture.md).

## ~/.ginno layout

```
~/.ginno/
├── settings.json          # hooks / permissions / env / model
├── config.json            # UI theme, providers
├── MEMORY.md              # long-term memory index
├── memory/*.md            # memory entries
├── projects/<slug>/       # per-project agent metadata
│   ├── GINNO.md           # project-level rules
│   ├── sessions/*.json    # file-based checkpointer
│   ├── plans/  todos/     # task state
│   └── skills/            # project-scoped skills
├── skills/<name>/SKILL.md # global skills
├── mcp/mcp.json           # MCP server registry
├── hooks/                 # hook scripts
├── vectorstore/           # LanceDB (Obsidian index)
├── usage/                 # token-usage logs (requests-YYYY-MM-DD.jsonl)
└── logs/
```

## Develop

```bash
# install deps
pnpm install
cd packages/runtime && uv sync && cd ../..

# run all (web + runtime + desktop shell)
pnpm dev

# or individually
pnpm dev:web        # Next.js on :3000
pnpm dev:runtime    # FastAPI on :8787
pnpm dev:desktop    # Tauri (loads web, spawns sidecar)
```

## Status

In active daily use. See `docs/architecture.md` for the current architecture (Studio + workflow engine + sidecar).
