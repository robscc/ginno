# ginno-runtime

Python sidecar: FastAPI + LangGraph runtime for Ginno.

## Develop

```bash
uv sync
uv run uvicorn ginno_runtime.server:app --reload --port 8787
```

## Bundle

```bash
uv run pyinstaller --onefile src/ginno_runtime/__main__.py --name ginno-runtime
```

## Layout

```
src/ginno_runtime/
├── paths.py            # ~/.ginno path resolution
├── state.py            # AgentState TypedDict
├── checkpointer.py     # FileCheckpointer (JSON-on-disk, no DB)
├── graph.py            # main LangGraph: load_context → agent → permission → tools
├── server.py           # FastAPI app + WebSocket
├── __main__.py         # PyInstaller entry
├── skills/loader.py    # SKILL.md loader
├── mcp/registry.py    # MCP server registry + tool bridge
├── hooks/dispatcher.py # hook event dispatcher
├── permission/policy.py# allow/deny/ask matcher
└── tools/builtin.py    # Read/Grep/Glob/Write/Edit/Bash
```

## Runtime layout on disk

See `docs/architecture.md` — all state under `~/.ginno/`.
