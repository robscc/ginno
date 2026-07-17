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
├── docs/
│   └── architecture.md
└── scripts/
    └── dev.sh          # run all three processes in dev
```

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

Skeleton scaffold — see `docs/architecture.md` for the design and roadmap.
