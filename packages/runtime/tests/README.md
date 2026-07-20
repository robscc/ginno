# Ginno runtime tests

Automated test suite for the Python sidecar (`ginno_runtime`). It covers the
runtime at three depths — **unit**, **API integration**, and **WebSocket E2E** —
all hermetic: no network, no API key, no real LLM, and no browser.

## Run

```bash
# from packages/runtime
uv run --group test pytest            # everything
uv run --group test pytest -m unit    # pure unit tests
uv run --group test pytest -m api     # FastAPI HTTP integration
uv run --group test pytest -m e2e     # WebSocket end-to-end (real LangGraph)

# or from the repo root
pnpm test
bash packages/runtime/scripts/test.sh -m e2e
```

## How isolation works

Every store, the file checkpointer, and all settings read through
`paths.home()`, which honors the `$GINNO_HOME` env var **at call time**. The
autouse `isolated_home` fixture (in `tests/conftest.py`) points `$GINNO_HOME` at
a fresh `tmp_path` per test and clears the server's process-wide globals
(`_SESSIONS`, `_mcp`, `_hooks`). Result: no test touches the real `~/.ginno`,
and none leaks state into another. Tests pass in any order or in isolation.

## The fake LLM

`ginno_runtime/testing/fake_model.py` provides `ScriptedChatModel`, which replays
a fixed list of `AIMessage` turns (text and/or tool calls) instead of calling a
real LLM. Driving the **real compiled graph** with it exercises the whole agent
loop — tool execution, permission interrupts, checkpointer persistence — with
zero network.

Key rules (validated against langchain-core 1.4.x / langgraph 1.2.x):

- Override `_generate` (drives the node result, `updates` mode, routing) and
  `_astream` (drives the `messages` mode the WS layer turns into `token.delta` /
  `tool.start`). `bind_tools` is overridden to `return self` (the base raises
  `NotImplementedError`, and the graph calls it whenever a tool is allowed).
- A streamed tool call is split into a **name-first chunk with empty args**
  (fires the server's `tool.start`) followed by an args chunk. `script_tool_call`
  + `_iter_chunks` handle this.
- Structured-output tools (`render_widget`, `attach_ref`) and `workflow_*` are
  surfaced by the server from the **complete `tool_calls`** on the AIMessage in
  `updates` mode — so put the full `tool_calls` on the `_generate` return value.
- When the script is exhausted the model returns a terminal tool-less message so
  the graph always reaches `END` (a block/ask that routes back to the agent
  would otherwise loop forever and hang a WS test).

Build turns with the helpers:

```python
from ginno_runtime.testing.fake_model import script, script_tool_call

model = [
    script(tool_calls=[script_tool_call("write_file", {"path": "a.txt", "content": "hi", "workspace": ws})]),
    script(text="done"),
]
```

## Test tiers

| Tier | Marker | Style | What it covers |
|------|--------|-------|----------------|
| Unit | `-m unit` | sync + some `async` | `paths`, permission policy, skills, stores, agents, providers, checkpointer, hooks, built-in & agent tools, graph helpers, `build_model` seam |
| API | `-m api` | sync `TestClient` | `/sessions`, agents/todos/workflows/providers/skills/mcp/artifacts CRUD, health |
| E2E | `-m e2e` | sync `TestClient` WebSocket | the real graph via `/ws/sessions/{id}`: chat happy path, permission ask→resume, agent routing & `tools_allow`, structured output, todos, persistence, hooks |

**Sync vs async:** API/E2E tests are sync and use `TestClient` (as a context
manager, so the FastAPI lifespan runs and seeds the home). Unit tests that drive
`build_graph` directly are `async` (`asyncio_mode = "auto"`). Never call
`TestClient` from inside an `async def` test.

## The `GINNO_FAKE_LLM` production seam

`models.build_model` checks `GINNO_FAKE_LLM`; when set, it returns a
deterministic `ScriptedChatModel` (script sourced from `GINNO_FAKE_LLM_SCRIPTS`,
a JSON list of `{"content", "tool_calls"}` turns, or an inline JSON string)
instead of a real provider. **Off by default — zero effect on production.** This
lets a subprocess-launched server (full-process / CLI e2e) run deterministically
without a real API key. In-process tests instead monkeypatch
`ginno_runtime.server.build_model` for per-test control.
