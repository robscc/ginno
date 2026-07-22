# Ginno Architecture

Personal AI Agent — Tauri desktop app, Next.js UI, LangGraph runtime, local file storage.

## 1. Design Principles

- **Claude-Code-inspired**: hooks, skills, slash commands, MCP, permissions, sessions, memory, project metadata mirror.
- **No database**: all state on disk under `~/.ginno/`. LangGraph checkpointer is file-based.
- **Local-first**: user projects live in `~/workspace/<proj>/`; agent metadata under `~/.ginno/projects/<slug>/`.
- **Open-source, source-controllable**: LangGraph (MIT) + AgentScope-style abstractions where useful.
- **Dynamic graph**: LangGraph with `add_conditional_edges` + `Command(goto=...)` + `Send()` + subgraph dispatch + `interrupt()` for HITL. Topology compiled; dispatch dynamic.

## 2. Process Topology

```
┌──────────────────────────────────────────────────────────┐
│  Tauri Shell (Rust)                                      │
│  ┌────────────────────┐   ┌──────────────────────────┐  │
│  │  Next.js webview   │◄─►│  Python sidecar          │  │
│  │  (static export)   │   │  FastAPI + LangGraph     │  │
│  └────────────────────┘   └────────────┬─────────────┘  │
└─────────────────────────────────────────┼────────────────┘
                                          │
                          ┌───────────────┴───────────────┐
                          │  ~/.ginno/  (file storage)     │
                          └───────────────────────────────┘
```

- HTTP REST for one-shot ops (sessions, skills, settings).
- WebSocket for streaming (token deltas, tool events, permission requests, hook events).

## 3. LangGraph Main Graph

```
START → load_context → agent ──┬──► (no tool calls) → END
                              │
                              ▼ (has tool calls)
                          permission ──┬──► allow → tools ──► post_hooks ──► agent
                                       └──► ask  → interrupt() → resume ────┘
                                       └──► deny → cancel ─────────────────► agent
```

State fields:
- `messages` (add_messages reducer)
- `workspace` (cwd)
- `active_skills` (list of skill names loaded into context)
- `pending_tool_calls`

Persistence: every node write triggers `FileCheckpointer.put(...)`, atomic JSON write to `~/.ginno/projects/<slug>/sessions/<session_id>.json`.

## 4. File Checkpointer

- Subclass of `BaseCheckpointSaver`.
- One JSON file per session, with a `checkpoints[]` array inside (or one file per checkpoint — TBD).
- Atomic write (temp + rename).
- Supports `aget`, `aput`, `alist`, time-travel resume.

## 5. Skills

- Format: `SKILL.md` with frontmatter (`name`, `description`, `trigger: user-invocable | model-invocable | both`, `tools: [...]`, optional scripts).
- Loaded at `load_context` node: skill names + descriptions injected into system prompt.
- User-invocable: `/<skill-name>` slash command in UI → injects SKILL.md body as user message.
- Model-invocable: `use_skill(name)` tool returns SKILL.md body.
- Scripts in skill directory auto-registered as scoped tools.

## 6. MCP

- `~/.ginno/mcp/mcp.json` registry (stdio / SSE / streamable-http).
- At runtime startup, spawn all stdio servers, connect to all HTTP servers.
- Convert MCP tools → LangChain `BaseTool`, register in `ToolNode`.
- Obsidian MCP server is the primary KB access path; `Read/Grep/Glob` over the vault dir is the fallback.

## 7. Hooks

- Events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`.
- Settings: `~/.ginno/settings.json` → `hooks.<Event> = [{ matcher, command }]`.
- Dispatcher: pipe JSON context to hook process stdin, read JSON stdout, apply effects (block / inject / rewrite).

## 8. Permissions

- Settings: `permissions.allow / deny / ask` with glob matchers over tool + args.
- `permission` graph node: match each `pending_tool_calls` against policy.
  - allow → pass through to tools.
  - deny → return tool result with refusal.
  - ask  → `interrupt({ tool_call, reason })`, resume on UI response.

## 9. Memory

- **Structured**: `~/.ginno/MEMORY.md` index + `memory/*.md` entries；由自动捕获 + `POST /memory/summarize` 提炼写入（**无** `memory.save/read/forget` 工具，路线中）。
- **Session**: file checkpointer.
- **Semantic** (optional): LanceDB over `memory/*.md` and Obsidian vault; tools: `memory.recall`, `obsidian.recall`.

## 10. Packaging

1. `uv sync` in `packages/runtime` → install Python deps.
2. `pyinstaller --onefile` → `ginno-runtime` binary (bundled as Tauri sidecar resource).
3. `next build && next export` → `apps/web/out/`.
4. `tauri build` → `.dmg` / `.msi` / `.AppImage`.

Expected size: 25–40 MB.

## 11. Roadmap

| Phase | Scope | Done when |
|---|---|---|
| P0 | Skeleton: runtime + web + desktop shell; one ReAct round-trip; resume | smoke test passes |
| P1 | MCP loader + Skill loader + Obsidian MCP | `/<skill>` works, agent searches vault |
| P2 | Hooks dispatcher + permission interrupt | sensitive tool prompts user |
| P3 | PyInstaller + Tauri build | single `.dmg` runs |
| P4 | MEMORY.md + LanceDB Obsidian index | cross-session remembers preferences |
| P5 | Sessions / Skills / MCP / Memory / Settings UI pages | all pages usable |
