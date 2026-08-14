# Ginno Architecture

> 本文描述 **当前主干代码的真实架构**（2026-08 重写并持续同步；最近一次全量核对 2026-08-08）。
> Ginno 已从"单轮 ReAct 骨架"演进为：多 Agent 对话 + 工具/权限/技能/MCP/记忆/知识库
> + Workflow DSL 引擎 + Goal 长程自主推进 + TODO 外部平台同步 + 内置 Web 搜索与引用溯源
> 的个人 AI Agent 桌面应用。
>
> 相关文档：
> - 使用层面 → `docs/user-guide.md`
> - 知识库设计 → `docs/knowledge-and-wiki-design.md`
> - Workflow DSL → `docs/workflow-dsl-design.md`
> - Goal 设计 → `docs/goal-design.md`
> - 命令与 @提及 → `docs/commands-and-mentions-design.md`
> - 引用与来源（Wiki + WebSearch）→ `docs/citations-design.md`
> - 用量统计 → `docs/usage-stats-design.md`
> - 内嵌浏览器 → `docs/browser-embed-design.md`
> - 打包细节 → `docs/p3-packaging-notes.md`
> - 文件解析 → `docs/file-parsing-research.md`

---

## 0. 一句话总览

**Personal AI Agent**：Tauri（Rust）壳 + Next.js 静态导出 UI + Python（FastAPI + LangGraph）sidecar 运行时，
全部状态以**文件**形式落在 `~/.ginno/`，**无数据库、无账号、无云同步**，本地优先。

---

## 1. 设计原则

- **Claude-Code-inspired**：skills、slash commands、@mentions、hooks、permissions、MCP、多 Agent、会话与记忆均对齐 Claude Code 的形态。
- **Codex-harness 纪律**：WorldState 分节上下文工程（§6.4）、Goal 长程自主推进（§6.12）、自动压缩（§6.5）借鉴 Codex/Claude 的 harness 实践并适配 LangGraph。
- **No database**：所有状态是 `~/.ginno/` 下的 JSON / JSONL / Markdown 文件；LangGraph checkpointer 是自研**文件式**实现（§6.6），原子写（temp + `os.replace`）。
- **Local-first**：单用户、单机；用户产物在会话工作目录，agent 元数据在 `~/.ginno/projects/<slug>/`。
- **同源架构**：打包后 **sidecar 同时托管 UI 静态产物、REST 与 WebSocket**，webview 直接加载 `http://127.0.0.1:8787`（§3），彻底回避 Tauri 跨协议/混合内容问题。
- **动态图**：聊天主图用 LangGraph `Command(goto=…)` + `interrupt()` 做 HITL；Workflow 则把版本化 DSL **编译成 LangGraph StateGraph** 执行（§6.11）。拓扑编译期确定、运行期分支。
- **容错优先**：内置工具"永不抛异常"、`ToolNode(handle_tool_errors=True)`、压缩/迁移/watchdog 失败都不阻塞主链路（§6.3、§6.5）。

---

## 2. 技术栈

| 层 | 选型 |
|---|---|
| 壳 | Tauri 2（Rust），仅作进程管理 + webview，**不注册任何 `#[tauri::command]`** |
| UI | Next.js 14（`output:"export"` 纯静态）+ React 18 + Tailwind + shadcn 风格手写组件 + d3（仅数学）+ react-markdown |
| 运行时 | Python ≥3.11 · FastAPI + uvicorn · LangGraph ≥0.2 · langchain-core/anthropic/openai · mcp ≥1.0 · pydantic 2 |
| 状态管理 | 前端单一 React Context（`GinnoProvider`）；聊天消息态在组件 per-session refs |
| 存储 | 纯文件（JSON/JSONL/MD），无 DB；可选 LanceDB（仅语义检索向量缓存，`--extra rag`） |
| 打包 | PyInstaller **onedir** bundle（内嵌 web 静态产物）→ Tauri resource → `.app/.dmg` |
| 语言依赖管理 | `uv`（Python，runtime 非 pnpm 成员）+ `pnpm` workspace（web/desktop） |

---

## 3. 进程拓扑与同源架构

```
┌─────────────────────────────────────────────────────────────────────┐
│  Tauri Shell (Rust, apps/desktop/src/lib.rs)                        │
│   • release: std::process::Command 拉起 sidecar（先 kill 占用 8787   │
│     的 stale ginno-runtime）；日志重定向 ~/.ginno/logs/sidecar.log   │
│   • dev: 不拉起，假定 `pnpm dev:runtime` 已起                        │
│   • 冷启动未就绪 → 先导航 data: URL 内嵌 splash，轮询 /api/health     │
│   • 原生桥：拖放 `__ginnoFileDrop`；完成通知 `ginno:notify`；浏览器 tile 几何 `ginno:browser-tile`（只存矩形，业务仍在 sidecar）          │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  WKWebView ──► http://127.0.0.1:8787                          │   │
│  └──────────────────────┬───────────────────────────────────────┘   │
└─────────────────────────┼───────────────────────────────────────────┘
                          ▼
┌─────────────────────────────────────────────────────────────────────┐
│  Python Sidecar  =  FastAPI app（server.py 壳 + api/ 路由包）         │
│   • GET /_next/*、catch-all GET /{path}  → 托管 Next 静态导出        │
│       打包时来自 sys._MEIPASS/web_out，开发时来自 apps/web/out       │
│   • /api/**          → ~105 个 REST 端点（§7.1）                     │
│   • /api/ws/sessions/{sid} → WebSocket（turn 流式 + HITL，§7.2）     │
│   • 端口 8787（GINNO_RUNTIME_PORT 可改）                             │
└──────────────────────────┬──────────────────────────────────────────┘
                           ▼
              ~/.ginno/   （全部状态 · 文件 · 无数据库）
```

**关键**：静态页、REST、WS **三者同源**（都是 sidecar 的 8787）。Tauri 不托管任何前端文件，
`frontendDist` 直接就是 `http://127.0.0.1:8787`。这规避了 `tauri://localhost → http://…`
的跨协议/混合内容拦截。

### Runtime 启动流程（FastAPI lifespan）
1. `paths.ensure_layout()`：建 `~/.ginno` 目录树 + 种子文件（settings/config/MEMORY.md/mcp.json/todos.json/agents）。
2. 初始化轮转日志 `sidecar.log`（5MB×3）；`usage_store.cleanup()` 清过期用量日志（保留期裁剪）。
3. `migration.migrate_session_files()`：遗留会话文件迁移（幂等、非致命）。
4. `HookDispatcher.from_settings()`；种子 todos / workflows；agents 幂等迁移
   （`ensure_todo_tools / ensure_research_discipline / ensure_goal_tools / ensure_web_tools`）。
5. `_reconcile_orphan_runs()`：上次崩溃遗留的 "running" workflow run 置 interrupted。
6. `_refresh_session_metas()`：按当前 provider 重解析所有 session meta。
7. `MCPRegistry.load()` 后**后台** `connect_all()`——端口绑定不被慢 MCP 阻塞。
8. CORS 全开；shutdown 时取消 run 后台任务、关闭 MCP。

---

## 4. 仓库布局

```
ginno/
├── apps/
│   ├── desktop/        # Tauri 壳（Rust + tauri.conf.json + entitlements.plist）
│   └── web/            # Next.js UI（静态导出）
├── packages/
│   └── runtime/        # Python：FastAPI + LangGraph（uv 管理，非 pnpm 成员）
│       └── src/ginno_runtime/
│           ├── server.py        # FastAPI 壳：app/lifespan/CORS/静态托管/main + facade
│           ├── server_shared.py # 进程级可变状态（_SESSIONS/_SESSION_WS/…）+ 事件推送
│           ├── session_meta.py  # sessions/_index.json 存取助手
│           ├── api/             # 按域拆分的 APIRouter：sessions/stream/workflows/
│           │                    #   files/knowledge/config/todos/usage/memory/messages_ui
│           ├── graph.py         # 聊天主图（agent/permission/tools）+ 系统提示分层
│           ├── state.py         # 图状态 TypedDict
│           ├── world_state.py   # WorldState 上下文工程（分节 + diff）
│           ├── checkpointer.py  # FileCheckpointer（full/delta）
│           ├── compaction.py    # 自动摘要压缩（E3/E4）
│           ├── microcompact.py  # 清理旧工具输出占位符（E2.5）
│           ├── truncation.py    # 工具输出中段截断（E2）
│           ├── tokens.py usage.py usage_store.py  # token 估算 / 内存用量 / 持久用量日志
│           ├── paths.py         # ~/.ginno 布局 + 默认 settings 模板
│           ├── providers.py models.py  # 多 provider 注册表 / LangChain model 工厂
│           ├── commands/        # slash 命令 + @mentions 解析
│           ├── skills/          # SKILL.md 加载 + 安装
│           ├── hooks/ permission/ mcp/ agents/ memory/
│           ├── knowledge/       # LLMWiki：indexer/retriever/semantic/injection/compiler/
│           │                    #   association + citations/usage/web_usage（引用台账）
│           ├── web/             # 内置网络搜索：config/engines/fetch（§6.13）
│           ├── workflows/       # DSL/compiler/engine/nodes/supervisor/store/events
│           ├── goals/           # goal store / 模板 / 事件桥
│           ├── browser/         # BrowserSupervisor / Space 注册表 / CDP / handoff（§6.14）
│           ├── todos/           # TODO store / providers / sync_ledger
│           ├── artifacts/ files/ tools/ testing/
├── docs/               # 本文 + 各子系统设计与使用文档
├── scripts/dev.sh      # 一键起三进程
├── Makefile            # 打包流水线（web→runtime→sidecar→app）+ e2e-ui
└── package.json / pnpm-workspace.yaml
```

---

## 5. 会话模型与运行时内存结构

- **磁盘真相源**：每 project slug 一个 `sessions/_index.json`（session meta 数组）+ 每会话一个
  `<sid>.json`（FileCheckpointer）+ `<sid>.world.json`（WorldState baseline）+ `<sid>/`（文件目录）。
- **内存注册表**（`server_shared.py`）：
  - `_SESSIONS`：session_id → 已编译 graph + model + meta；**重启后由 `_ensure_session` 惰性重建**。
  - `_SESSION_WS`：session_id → 该会话**所有**活跃 socket（广播式投递，§7.3）。
  - `_RUNNING_TURNS` / `_PENDING_RESUME`：在途 turn / 待 resume 的 interrupt（重连恢复用）。
  - `_USAGE_BY_SESSION`：本次应用会话的 token 用量累计（重启清零；持久口径见 `usage_store`）。
  - `_GOAL_DRIVERS`：每个 active goal 一个自主续跑 asyncio task（§6.12）。
  - `_TURN_LOCKS` / `_WF_RUN_TASKS`：每会话 turn 串行锁 / workflow run 后台任务表。
- **一个对话 = 一个 session**；会话可中途切 Agent（历史共享）；project slug 当前固定 `default`。

---

## 6. Python Runtime 核心

### 6.1 聊天主图（`graph.py`）— 真实拓扑

只有 **3 个节点**（早期文档中的 `load_context` / `post_hooks` / `cancel` 节点**不存在**）：

```
START ──► agent ──(conditional: 有 pending_tool_calls?)──┬─► permission ──► tools ──► agent（循环）
                                                          └─► END
```

- `permission → tools` 是**无条件边**；分叉不靠边而靠 **`Command(goto=…)`**：
  tools_allow 拦截 / hook block / 用户 deny / policy deny 都返回携带 `[blocked:<tool>] …`
  AIMessage 的 `Command(goto="agent")`；全部通过才 `Command(goto="tools")`。
- **`agent` 节点**：每 turn 用 `build_stable_system` **重建** system prompt（不入 checkpoint →
  会话中途换 Agent 下一 turn 即生效）；按 `tools_allow`（fnmatch）过滤子集后 `bind_tools`；
  对历史副本 `strip_old_images`（只留最近 2 个用户轮，send-only 不落盘）；Anthropic 模型加
  `cache_control: ephemeral` 前缀缓存断点。
- **`permission` 节点**：判定顺序见 §6.9。
- **`tools` 节点**：`ToolNode(all_tools, handle_tool_errors=True)` + 工具输出中段截断（§6.5 E2）。

### 6.2 图状态字段（`state.py`）

| 字段 | 说明 |
|---|---|
| `messages` | `add_messages` reducer |
| `workspace` | **会话文件目录**（`sessions/<sid>/`，非用户传入路径） |
| `project_slug` | 项目 slug |
| `agent_id` | 当前 persona（实际读取优先用 `config.configurable.agent_id`） |
| `active_skills` | slash-skill turn 标记 |
| `pending_tool_calls` | agent→permission 的通道 |
| `attached_files` | 本轮附件 `{id,name,path,kind,schema?}` |
| `mention_context` | @提及上下文（每 turn 重置） |
| `mcp_tool_names` | 供 WorldState 检测中途 MCP 重载 |

### 6.3 工具全集（`build_all_tools`）

聊天主图与 Workflow 引擎共享同一 union 工具集。内置工具**永不抛异常**（返回 `[error] …` 字符串），
并按 session workspace 绑定（模型看不到 workspace 参数）。

| 组 | 工具名 |
|---|---|
| 文件/shell | `read_file, write_file, edit_file, glob_files, grep_files, bash` |
| 技能 | `use_skill, list_skills, install_skills, uninstall_skill` |
| 结构化输出（静默） | `render_widget, attach_ref` |
| TODO | `todo_list, todo_create, todo_update, todo_done, todo_delete, todo_link` |
| Goal | `goal_get, goal_create, goal_update`（仅有 session 时绑定） |
| Workflow | `workflow_list, workflow_create, workflow_run, workflow_step` |
| Workflow-dev | `workflow_propose_edit` |
| Artifact | `artifact_register` |
| 文档 | `parse_document, analyze_table` |
| Web | `web_search, web_fetch`（引擎可插拔；`settings.web.enabled=false` 时不注册） |
| Browser | `browser_eval`（内嵌浏览器 JS 执行；详见 §6.14） |
| MCP | `mcp_{server}_{tool}`（动态，默认含 playwright） |

> `render_widget` / `attach_ref` 是 **no-op 工具**，WS 层拦截其调用发 `widget.emit`/`ref.emit`
> 事件，不产生普通工具气泡、对所有 Agent 恒允许、免权限。图表是 `render_widget(kind="chart")`
> （**没有独立的 `render_chart` 工具**）；`analyze_table` 在隔离子进程跑 pandas（打包走
> `ginno-runtime --analyze` 隐藏模式）。
>
> **引用与来源**（`docs/citations-design.md`）：每轮维护 SourceRegistry（注入的 wiki 页 +
> web_search 结果编号 `sN`）；模型按引用契约在回复末尾附 `<ginno_citations>` 块，turn 结束时
> 解析/三态校验（verified / index_only / unverified）并记台账（`knowledge/usage.json`、
> `web_usage.json`）；历史渲染把块折叠为 `sources` 块（前端「来源」卡），`web_fetch` 仅允许公网
> http/https。

### 6.4 WorldState 上下文工程（`world_state.py`）

模型可见的"世界"切成命名 **sections**，各产小 snapshot；每 turn 与 baseline diff，变化显式通告；
稳定系统层跨 turn 字节级不变以吃 Anthropic 前缀缓存。

| section | 内容要点 |
|---|---|
| `agent` | agent_id / name / prompt_hash / tool_count |
| `goal` | goal_id / status / objective / turns_used（含 guidance） |
| `environment` | date / weekday / tz / os / ginno_home / workspace / project（**故意无时钟时间**） |
| `permissions` | bypass + allow/deny/ask 条数 |
| `skills` | 名单 + 预算索引（默认 1500 字符）+ 目录 + 安装指引 |
| `memory` | 全局/Agent 私有 MEMORY 的 hash + 全文 |
| `mcp` | count + hash（工具经 bind_tools 到达，这里只发变更通告） |

**注入路径**：
1. **稳定层**：`build_stable_system` = persona + `WorldState.render_system()` + 工具行 + 结构化输出指引。
2. **变更显式化**：`sync_world_state` 与 baseline diff；有变化 → 合并成一条 `[world state update]`
   HumanMessage 插在用户消息前 + `context.updated` chip 事件。首次同步只记 baseline 不通告。
3. **压缩后重申**：`render_reinjection` → `[world state re-injection]` 全量重申。
4. **每 turn 易变上下文**：`build_turn_context`（wiki 检索 / 附件 schema / @提及）包成 `[turn context]`
   尾消息，**不进稳定 system prompt**（保住前缀缓存）；注入内容经 `_INJECTION_PATTERNS` sanitize。

> 消息标记体系 `[world state update] / [world state re-injection] / [turn context] /
> [conversation summary] / [goal context]` 在 UI 历史端点映射为居中 context 行，`[turn context]` 隐藏。

### 6.5 上下文治理：token / 截断 / 压缩 / 用量

| 机制 | 触发 | 行为 |
|---|---|---|
| **E1 token 估算**（`tokens.py`） | 廉价本地启发 | latin≈chars/4，CJK≈1.5/字；ground truth 是 provider usage |
| **E2 工具输出截断**（`truncation.py`） | tools 节点对每 ToolMessage | 阈值 `tool_output_max_chars`（默认 20000）；保留头 60%+尾 40%，中间插标记。**持久化历史与模型视图一致** |
| **E2.5 microcompact**（`microcompact.py`） | 每 turn 开始前、无 token 阈值、无 pending interrupt | 把保留窗口（最近 `compact_keep_turns` 轮）之外的旧 ToolMessage 正文替换为 `[old tool output cleared]`；只清 > `microcompact_min_chars`（默认 500）的输出；纯状态重写、无 LLM 调用。广播 `context.microcompacted` |
| **E3 自动压缩**（`compaction.py`） | 每 turn 开始前、估算 ≥ `compact_threshold_tokens`（默认 500000）且无 pending interrupt | 按用户轮切分，保留最近 `compact_keep_turns`（默认 3）轮逐字；前缀用会话自身模型总结成 `[conversation summary]`；`RemoveMessage` 后重写 |
| **E4 压缩后重申** | 压缩成功后 | 重申 WorldState；广播 `context.compacted` |
| **图片剥离** | agent 节点 | 只留最近 2 轮用户图（send-only，落盘保留） |
| **D1 用量遥测（内存）**（`usage.py`） | 每次完整 LLM 调用 | 抽 input/output/cache_read/cache_creation，累计 `_USAGE_BY_SESSION` + `usage` WS 事件 + `cache_hit_ratio`（重启清零） |
| **D2 用量遥测（持久）**（`usage_store.py` + `api/usage.py`） | 同上，每次调用追加一行 | `~/.ginno/usage/requests-YYYY-MM-DD.jsonl` append-only（只存计数/元数据、无内容）；Settings → 用量统计页（overview/hourly/sessions/requests）流式聚合；启动与跨天清理过保留期。详见 `usage-stats-design.md` |

> 超时：`CHAT_TIMEOUT_S=180`（请求级）、`CHUNK_TIMEOUT_S=180`（chunk 看门狗）、`_WS_SEND_TIMEOUT_S=5`（socket 剪枝）。

### 6.6 FileCheckpointer（`checkpointer.py`）

- 位置 `projects/<slug>/sessions/<sid>.json`，顶层 `{"session_id", "checkpoints":[…]}` append-only。
- serde 用 LangGraph `JsonPlusSerializer`（typed 值 `{type, data:base64}`）。
- **full / delta 双模式**（`settings.context.checkpoint_mode` 默认 `"delta"`）：
  - delta 仅当新历史是父历史按消息 id 的**纯前缀追加**；`messages` 通道 `{mode:"append"}`，其余通道 full。
  - 任何历史重写（压缩/回滚/变短）→ 退化为 full。解决"长历史重复全量落盘"的二次膨胀（实测 ~79%）。
  - 重建沿 `base` 链回溯到 full 锚点再正向 fold。
  - **parent 推导**：langgraph ≥ 1.x 不再在 checkpoint 里嵌 `parent_config`，而是把父
    checkpoint id 放进 `put()` 的 `config`；读取顺序 = `config.checkpoint_id` →
    `checkpoint.parent_config`（旧版）→ 最后一条已存条目（兜底）。纯追加校验会在猜错时
    （rewind/分叉）安全退化为 full。此前只读 `parent_config` 导致 delta 全程失效、文件平方级膨胀。
  - **channel_values 只由 delta 自身重建**，不从 base 继承：langgraph 会在后续 checkpoint
    丢弃已消费的 trigger 通道（`__start__` / `branch:to:*`）；若从锚点继承会复活陈旧 trigger，
    使 `StateSnapshot.next` 误报有待执行节点（曾导致 microcompact/compaction 的 interrupt 守卫误跳过）。
- **原子写**：`RLock` 串行化读改写 + tempfile + `os.replace`。
- `put_writes` 记录每 superstep 的 writes 到 `pending_writes[]`（崩溃取证；**尚未用于 mid-step resume**）。
- **`ABANDONED_TURNS`**：stall 看门狗放弃的 turn，其迟到的 `aput` 被拒，防"复活"的挂起 run 回滚。
- 聊天用 `thread_id=session_id`，Workflow run 用 `thread_id=run_id`，同一实现。

### 6.7 Agents（`agents/`）

- `AgentConfig`：`id, name, icon, color, system_prompt, provider, model, tools_allow, memory_scope`；
  每 Agent 一个 `~/.ginno/agents/<id>.json`。
- **内置种子**：`dev`（`*` 全工具）、`research`（只读纪律）、`writer`、`workflow-dev`（仅
  `workflow_propose_edit/workflow_list`，走 diff 确认）。
- **幂等迁移**：`ensure_todo_tools / ensure_goal_tools / ensure_web_tools / ensure_research_discipline`。
- **每 Agent 私有记忆**：`agents/<id>/MEMORY.md` + `memory/`，回答时注入其 prompt。
- **路由**：`@agent` mention 产 `agent_override`；WS 优先级 `override > msg.agent_id > session.agent_id > 首个`。
- Workflow 运行时 `fork_agent` fork 一次性 scratch agent，run 结束删除。

### 6.8 Skills / Commands / @mentions

- **Skills**（`skills/`）：`SKILL.md` 带 frontmatter（`name, description, trigger, tools, todo_provider`）。
  三层目录，同名覆盖 **内置 < 全局 < 项目**；索引注入走 WorldState `SkillsSection`（非旧式
  `build_index_prompt`）。安装 `skills.installer` 由 REST `import-dir` 与工具 `install_skills` 共享。
  **`/<skill>` 本轮会把 frontmatter `tools:` 并入该 Agent 的 tools_allow**（`/browse` → `browser_*`，
  即使当前是 analyst）。不写 `tools:` 的 skill 仍只注入正文。
- **Slash commands**（`commands/`）：消息**首 token** 形如 `/name` 且命中内置命令或
  user-invocable skill 才触发（membership-gated，`/tmp/foo` 安全透传）。内置：`/help`、`/goal`。
  `/<skill>` 替换 SKILL.md 正文为 `<skill name=…>` + `User request:`。
- **@mentions**：结构化 `mentions:[{kind,id}]` 为权威，文本 `@kind:label` 仅兜底。
  kinds = `artifact / agent / workflow / memory`；`@agent` 只做路由覆盖。

### 6.9 Hooks（`hooks/`）与 Permissions（`permission/`）

- **Hooks**：事件类型 `SessionStart / UserPromptSubmit / PreToolUse / PostToolUse / Stop`，
  配置 `settings.hooks.<Event>=[{matcher, command}]`，shell 子进程 + JSON stdin/stdout，
  可 `block / inject / rewrite`。**当前主图只接线了 `PreToolUse`**（permission 节点）。
- **permission 节点判定顺序**：
  1. `render_widget/attach_ref` 直接放行；
  2. 产品内工具（todo/goal/workflow/artifact/skill + workflow-dev）**永不询问**；
  3. 非 bypass 下按 Agent `tools_allow` 拦截（越权直接拒，不弹框）；
  4. **PreToolUse hooks**（bypass 下仍执行，用户规则权威）；
  5. 非 bypass 下 `PermissionPolicy.decide`（匹配顺序 **deny → ask → allow**，默认 `ask`），
     `ask` → `interrupt({kind:"permission_request", tool, args})`。
- **`bypass_permissions` 默认 ON**：跳过 tools_allow 与 policy（一切放行），**仅 hooks 仍执行**。
- **HITL interrupt 全链路**：图暂停 → WS 发 `permission.request` + 置 `_PENDING_RESUME` →
  客户端回 `permission_response{decision}` → `Command(resume=…)` 续图；deny 注入 `[blocked:…]` 回 agent；
  **断线重连时重放未决 interrupt**（permission_request 与 version_propose）。

### 6.10 MCP（`mcp/`）

- 注册表 `~/.ginno/mcp/mcp.json`（`mcpServers`），transport = stdio / sse / streamable-http。
- **默认种子 Playwright MCP**（`npx @playwright/mcp --headless`，复用本机 Chrome）。
- 每 MCP tool 包成 LangChain `StructuredTool`，命名 `mcp_{server}_{tool}`；参数规范化统一 `json.dumps`。
- 后台连接 + 每 server 超时；单个失败不阻塞 HTTP 启动。`POST /api/mcp/reload` 重连。
- 知识库**不依赖** Obsidian MCP——`/api/kb/*`（非 wiki 那组）只是对所有 MCP server 的
  `search_files/list_directory` 透传，与内存 Wiki 索引是两条独立路径。

### 6.11 Memory（`memory/`）

- **自动捕获**：每轮 assistant 文本（sanitize 去注入标记）追加 `memory/pool/*.jsonl`。
- **手动/自动总结**：`POST /api/memory/summarize` 用 LLM 把 pool + 现有 MEMORY.md 提炼合并写回。
- **注入**：全局 `MEMORY.md` 每轮注入所有 Agent（`<injected_memory>`）；Agent 私有记忆并行注入。

### 6.12 Goal 长程目标（`goals/` + server goal driver）

- **每会话至多一个 goal**，`projects/<slug>/goals.json` 以 session_id 为键；`goal_id` 是乐观并发令牌。
- 状态机 `active / paused / blocked / usage_limited / complete`；**模型只能置 complete/blocked**
  （`goal_update`），pause/resume 是用户控制。
- **自主推进（goal driver）**：每个 active goal 一个 asyncio task；会话空闲 + 3s 宽限期后，
  **无头（无需客户端 socket）** 注入 continuation 消息跑下一 turn。**无轮数上限**（上下文交给 E3 压缩），
  失败按错误映射停止（rate limit→usage_limited，其他→blocked）防循环；Agent 切换自动暂停。
- **注入**：continuation 是隐藏 user 消息模板 `<ginno_goal kind=…>`（objective XML 转义、声明为数据）；
  活跃 goal 的存在性进 WorldState `GoalSection`。
- REST `GET/PUT/DELETE /api/sessions/{sid}/goal` + WS `goal.updated/cleared` + `/goal` 命令。

### 6.13 Web 搜索（`web/` + `tools/web_tools.py`）

- **内置工具**：`web_search(query, max_results?, engine?)` / `web_fetch(url)`——builtin 契约
  （永不抛异常，返回 `[error] …`）；`settings.web.enabled=false` 时不注册。
- **引擎可插拔**（`web/engines.py`，纯 stdlib 网络）：`duckduckgo`（默认，免 key，HTML 解析 +
  `uddg` 重定向解包）/ `searxng`（自建 base_url）/ `tavily`（API key）；注册表开放增补。
- **fetch 守卫**（`web/fetch.py`）：仅 http/https；主机**只解析一次**，混合应答（公网+内网 IP）
  整体拒绝；**连接钉死在已验证的地址上**（socket 直连验证过的 IP，无第二次可被 DNS-rebinding
  竞争的解析，TOCTOU 安全）；重定向手动跟随（≤5 跳）且**每一跳**重新解析+验证；正文经 stdlib
  HTMLParser 提取、上限截断。
- **来源登记**：搜索结果逐条进当轮 SourceRegistry（编号 `sN`、depth=snippet）；`web_fetch` 把
  匹配 URL 升级为 depth=fetched（引用校验与前端「来源」卡的数据源，见 §6.3 注）。
- **遥测**：`knowledge/web_usage.json` 按引擎记 searches/hits_cited（被引率=引擎有效性），
  按域名记 cited/fetched；`GET /kb/wiki/web-usage` 供 Settings → Web 搜索页展示。
 权限默认 `allow`（只读网络）；`ensure_web_tools` 把两工具并入 research/writer；
  `ensure_web_permissions` 迁移把两工具补进**升级安装**的 `permissions.allow`
  （默认种子只对全新安装生效，不迁移的话 bypass 关闭时会落到 `ask`，goal 无头续轮会卡死在权限弹窗）。

### 6.14 Browser — 内嵌浏览器（`browser/`）

> 详细设计见 `docs/browser-embed-design.md`。本节为架构级摘要。

- **产品形态**：工作区变 **Chat | Browser** 分栏，右侧不是截图回放，是一台带 Space/标签/地址栏的真 Chromium。
  人日常在 Human Space 浏览，Agent 在自己的 Space 里干活，登录共用（同一 profile），互不抢焦点。
- **所有权三态**（复制 ego-lite 契约）：`agent` → `agentDelegatedToUser`（handoff 等登录/验证码）→ `user` →
  `agent`（takeOver 完成）。`handOff()` 通过 LangGraph `interrupt({kind:"browser_handoff"})` 实现，
  与现有 `HumanNode` 的 `interrupt({kind:"human"})` 同模式；`takeOver()` 走 `/decide` + `/resume`。
- **工具契约**：`browser_eval(code, space?, timeout_s=180)` 在指定 Space 跑 ego-browser 方言；
  helpers 预注入。`openOrReuseTab` 会把裸主机名补成 `https://` 并等到非 `about:blank`。
  Chrome 新标签走 `PUT /json/new`（GET 会 405）。`/browse` 本轮授予 `browser_*`。
  超时 180s（绕过 bash 30s 默认），handoff 期间 Goal driver 自动暂停。
- **引擎两阶段**：生产优先打包 CEF 原生子视图（Helper.app + `libginno_cef.dylib` +
  宿主写出 `~/.ginno/browser/cef-cdp.json`）；宿主没起来则回退无头系统 Chrome +
  独立 profile + CDP screencast。`try_cef()` 只在 helpers **并且** CDP 真的在听时
  才返回实例，不会假装 native tile。引擎切换时节点/协议/数据模型**不动**。
- **BrowserSupervisor**（Python sidecar）：所有浏览器实例的权威管理者。维护 Space 注册表
  （`~/.ginno/browser/spaces.json`）、CDP 连接、ownership 状态机、handoff 协议。
- **Snapshot 85% 诚实**：CDP accessibility tree + 自研 refMap（`@N` / `loc=`）；M2 补同源 iframe
  与开放 shadow 提示。跨源 iframe / 封闭 shadow DOM 仍省略。
- **Workflow 一等节点**：DSL v1 扩展 `type: "browser"` 节点（action: `eval | snapshot | handoff | complete`），
  与 `step / branch / loop / human` 并列。工作流可写「打开审批页 → 等用户登录 → 抓取数据 → 关闭」
  的确定性流程，handoff 卡与聊天 human node 同模式。
- **Goal 集成**：Goal driver 续跑前检查 `browser_state`；`waiting_human`（handoff 中）时**不续跑**。
  高风险导航会先 flip owner 再 raise，driver 不会空转。

---

## 7. API 表面

### 7.1 REST（~105 端点，前缀 `/api`，按组；路由分散在 `api/` 各 router + `server.py`）

| 组 | 代表端点 |
|---|---|
| health | `GET /health` |
| usage（内存+持久） | `GET /sessions/{sid}/usage`（内存累计）· `GET /usage/overview /hourly /sessions /sessions/{sid} /requests`（持久 JSONL 聚合，用量统计页） |
| sessions | `POST/GET /sessions` · `PATCH/DELETE /sessions/{sid}` · `GET /sessions/{sid}/history` |
| goals | `GET/PUT/DELETE /sessions/{sid}/goal` |
| skills | `GET /skills` · `POST /skills` · `DELETE /skills/{name}` · `POST /skills/import-dir` |
| workflows | CRUD + `/versions`、`/versions/diff`、`/rollback`、`/summarize-from-session` |
| workflow_runs | `POST /workflow_runs`（绑定 session 后台跑）· `/{id}/cancel /resume /decide /events /_await` |
| todos | CRUD + `/todo-providers` · `/todos/sync-status` · `/todos/pull` · `/todos/{id}/push` |
| artifacts | `GET /artifacts` · `/{id}/metadata` · `PUT /{id}` · `DELETE /{id}` |
| files / session-files | `POST /files`（上传）· `/{id}/preview /download /save-to-downloads` · `attach-path` · session-files 浏览/reveal/删除 |
| settings/providers/agents/mcp | `GET/PUT /settings` · `GET/PUT /providers`（保存清空 `_SESSIONS`）· `/providers/{id}/verify /search_probe` · agents CRUD · `GET /mcp` · `PUT /mcp` · `POST /mcp/reload` |
| kb / memory | `GET /kb/servers /search /list`（MCP 透传）· `GET/PUT/POST /kb/wiki/*`（probe/search/list/stats/page/index/ingest/build/related/discover/orphans/backlinks/config）· `GET /memory` · `POST /memory/summarize` |
| 引用遥测 | `GET /kb/wiki/usage`（wiki 台账）· `POST /kb/wiki/usage/reset` · `GET /kb/wiki/web-usage`（web 引擎/域名） |
| web / 外链 | `POST /web/test-search`（引擎探活）· `POST /open-external`（系统浏览器打开，公网守卫） |
| 其他 | catch-all `GET /{path}`（SPA 兜底）· `WS /api/ws/sessions/{sid}` |

### 7.2 WebSocket 协议（`/api/ws/sessions/{session_id}`）

- **server→client** 帧为扁平 JSON `{"event", "turn_id"?, …}`。client→server 为 `{"type", …}`，
  type ∈ `invoke / permission_response / turn_state / ping`。
- **turn 生命周期事件**：`turn.start, token.delta, thinking.delta, tool.start, tool.end,
  permission.request, version.propose, widget.emit, ref.emit, workflow.emit, usage, message.end,
  error, keepalive(15s)`。
- **面板/资源同步事件**：`todos.changed / workflows.changed / artifacts.changed / skills.changed /
  agents.changed`、`preview.emit / preview.invalidate`、`run.bind / run.event / run.status`、
  `context.updated / context.microcompacted / context.compacted`、`goal.updated / goal.cleared`、
  `notice / turn.state / pong`。

### 7.3 断线重连 / resume（关键设计）

1. **广播式投递**：turn 事件发往该会话 `_SESSION_WS` 的**所有** socket；发送超时 5s 即剪枝。
2. **中断恢复**：WS 建立时检查 pending interrupts，重推 `permission_request`/`version_propose`
   并重新武装 `_PENDING_RESUME`/`_RUNNING_TURNS`——重启后内存标志丢失也能续。
3. **`turn_state` 探测**：客户端重连后发 `turn_state`；无在途 turn 则改从 `/history` 对账。
4. **stall 看门狗**：`CHUNK_TIMEOUT_S=180` 超时不取消（防重试层吞 CancelledError），而是放弃
   并把 turn_id 加入 `ABANDONED_TURNS` 拒后续 checkpoint 写。
5. **文件 watcher**：每 WS 连接一个 5s stat 轮询，mtime 变化 → `preview.invalidate`。
6. **跨重启 Goal 恢复**：WS 打开即 `_start_goal_driver`，active goal 自动续跑。

---

## 8. Workflows（`workflows/`）— 版本化 DSL + LangGraph 引擎

- **DSL 是 JSON dict**（`dsl.py`），"1:1 编译到 LangGraph"。形状：
  `{dsl_version, name, description, entry, nodes[], edges[], context{schema,initial}, supervisor}`。
  v1 节点类型 `step/branch/loop/human`（经节点注册表开放扩展）。
- **compiler**（`compiler.py`）把校验后的 DSL 编译成 `langgraph.StateGraph`，图状态
  `WorkflowState{context, results, loop_iters, loop_vars, events, inputs, outputs}`；
  节点类自带 `make_node/add_edges`，**新节点类型零编译器改动**。
- **类型化节点系统**（`nodes/`）：`BaseNode` 契约（params/inputs/outputs schema、coerce、execute）；
  `@register_node` 注册；扩展途径 = 导入自注册 / entry-point `ginno_runtime.workflow_nodes` /
  环境变量 `GINNO_NODE_PLUGINS`。内置：`AgentNode(step)`、`LLMNode`、`BranchNode`、`LoopNode`、
  `HumanNode`（`interrupt`）、`PassNode`。边 `transform` 支持 `map/expr/pick/defaults/fn`。
- **表达式沙箱**（`expr.py`）：AST 白名单（禁 import/exec/任意调用、禁 dunder），
  `{{expr}}` 模板；防 LLM 病态表达式（≤4000 字符 / 300 AST 节点）。
- **Supervisor**（`supervisor.py`）：节点校验失败时的恢复器，动作空间 `coerce/patch_dsl/retry/skip/abort`；
  默认确定性无依赖，可 `set_decider` 换 LLM/策略；每次介入记 `supervisor_intervene` 事件。
- **engine**（`engine.py`）：`run_workflow` 是 async generator——编译并绑 `FileCheckpointer`
  （`thread_id=run_id`），`astream` 把节点事件逐条 yield；图停在 interrupt 则 yield `paused`，否则 `done`；
  `resume_workflow` 以 `Command(resume=…)` 续流。
- **存储**：`workflows/<id>/meta.json` + `versions/<n>.json`（不可变全量快照，回滚=旧版本复制为新版本）；
  **runs 全局**存 `~/.ginno/workflow_runs/<run_id>.json` + `.events.jsonl`（**非 per-project**）。
  run 钉住执行的 `dsl_version`，携带 `session_id` 与 `present_in_session_id`（"run 回到对话"）。
- **两条执行路径**：legacy 工具路径（`workflow_run/step`，WS 正则解析 run_id）与引擎路径
  （`POST /api/workflow_runs` 后台 task）；聊天中触发的 run 会绑定当前 session 改由真引擎驱动，
  经 `run.bind/run.event/run.status` 推入会话。
- **对话式编辑**：`workflow-dev` Agent 调 `workflow_propose_edit` → `interrupt(kind="version_propose")`
  → 聊天弹 violet diff 确认卡，Apply 才写新版本。**独立于权限系统**。
- **从会话总结**：`POST /api/workflows/summarize-from-session` 用 LLM 把会话轨迹蒸馏成 DSL **草稿**（不落盘）。

---

## 9. Knowledge Base（`knowledge/`）— Obsidian Vault 上的 LLMWiki

- **核心**：一个 wiki 条目 = 一个带 YAML frontmatter 的 markdown 文件；索引是**内存** `WikiEntry`
  列表，无数据库、默认无 embedding，周期性从 vault 重建。
- **indexer**：解析 frontmatter + 正文（title/summary/wikilinks/checksum）；增量扫描（mtime 未变只 stat）；
  backlinks 图；`include_dirs` 限定只索引编译产物 wiki_dir。
- **检索**：CJK 字符 n-gram + 拉丁词分词；**多信号子串打分**（tag +0.4 / title +0.3 / summary +0.15，
  每字段每查询最多计一次）+ 新近度 + wikilink 图加成。**非经典 BM25**。
- **语义检索（可选）**：本地 `sentence-transformers` 编码 + LanceDB 磁盘缓存（`~/.ginno/vectorstore`），
  词法 + 余弦融合（`semantic_weight`）；完整降级阶梯（依赖缺失/失败自动回落纯词法）。
- **注入**：`build_wiki_context`（索引整 vault 但排除 raw_dir）→ 检索 → 包进 `<injected_wiki>`，
  作为**每轮易变 context 消息**（不进稳定 system prompt，保前缀缓存）。
- **编译**（`compiler.py`，确定性、默认零 LLM）：Raw → 概念页 + 汇总页 + INDEX；auto-associate
  （≥0.7 写 `## Related`）；关联扫描只限 wiki_dir，绝不改写用户 raw 文档。
- **关联图**（`association.py`，无 embedding）：TF-IDF 余弦 0.35 + tag Jaccard 0.25 + 共被引 0.20
  + 时间 0.10 + 层级 0.10；discover 提供 strong/isolated/orphan_bridges/merge_candidates。
- **引用与用量台账**（`citations.py / usage.py / web_usage.py`）：注入计数 + 回复引用校验
  （verified/index_only/unverified）落 `knowledge/usage.json` 与 `web_usage.json`，供检索加权
  （P2）、Discover 分区与 Settings 展示。全链路设计见 `citations-design.md`。

---

## 10. TODO 与外部平台同步（`todos/`）

- **store**：全局单文件 `todos.json`；item 含 `priority/category/due/done/emoji/tags/session_ids/
  artifact_ids/links/ext`。`ext` 是松散外部平台引用列表，去重键 `(provider,id)`。
- **工具**：`todo_list/create/update/done/delete/link`，按 Agent `tools_allow` 门控。
- **provider 抽象**：provider 是**自由字符串 id**（如 DingTalk），能力来自 SKILL.md
  `todo_provider` 声明 + `settings.todo_providers`。**同步本身是 LLM 驱动的**（注入 provider 的 skill
  + MCP 工具），新增平台**零适配器代码**。
- **双向同步 workflow**：内置 system workflow `todo-pull`（拉取平台未完成镜像到本地）与
  `todo-push`（本地完成态回写平台），是通用模板（`{{skill}}/{{provider}}/{{mcp}}` 经 context_override 注入）。
  PATCH todo `done` 置真自动触发 auto_push 的 push。
- **sync_ledger**（`todo_sync.json`）：append-only 事件台账（谁/何时/哪个 run/什么结果），
  与 todo 上的 `ext`（关系）分工明确，供面板显示状态与重试。

---

## 11. Artifacts 与 Files

- **Artifacts**（`artifacts/`）：`projects/<slug>/artifacts.json`；`{id, kind, name, ref, session_id}`；
  按 `(kind, ref or name)` 去重；登记来自 `artifact_register`/`attach_ref`（WS 层真正写库）。
  右栏只读展示 + metadata inspector（文件丢失时尝试在 vault 里 heal）。
- **Files**（`files/`）：
  - **extractors**：支持 xlsx/xlsm/xls、csv/tsv、docx、pptx、pdf、json/xml、txt/md；重依赖**懒加载**
    （`--extra docs`）。`schema_summary` 产表格紧凑 schema 供 prompt 注入。
  - **preview**：表格→分页 grid JSON，文档→markdown。
  - **registry**：`projects/<slug>/files.json` 文件身份台账；`touch()` 反应式通知（WS preview.invalidate）。
  - 会话文件目录 `sessions/<sid>/{uploads,results}/`；**会话删除后保留**，仅 orphaned 可经 session-files 端点清理。

---

## 12. Web 前端（`apps/web`）

- **静态导出**（`output:"export"`）；路由 `/`（工作区）、`/kb`、`/workflows`、`/settings/[tab]`
  （14 tab：model-api/skills/mcp/agents/workflows/knowledge/web/permissions/hooks/session-files/
  usage/general/notifications/tool-labels）。
  `/` 页面返回 `null`——工作区由 `AppShell` **常驻渲染**，路由切换用 `hidden` 隐藏而非卸载，
  保住 WS 连接与内存消息。
- **状态管理**：单一 React Context（`store.tsx` 的 `GinnoProvider`，无 Zustand）。全局态
  （agents/skills/sessions/todos/workflows/artifacts/providers/goalBySession/…）+ 乐观更新+失败回滚；
  **聊天消息态不在 Context**，而在 `ChatStream` 的 per-session refs（后台会话续流、切回即见）。
- **API 客户端**（`runtime.ts`，~70 函数）：BASE 取**同源** `window.location.origin + "/api"`，
  绝不写死端口；WS `openSessionSocket(sid)`。
- **`ChatStream`（聊天核心）**：per-session refs 存消息/socket/权限弹窗/草稿/绑定 runs；WS 每 20s ping、
  45s watchdog、close 后 3s 自动重连、重连后 `turn_state` 探测；`handle()` 消费约 25 种 WS 事件驱动 UI；
  发送走 `{type:"invoke", …}`，未送达标红可重试（复用 turn_id 让 add_messages 去重）。
- **组件**：blocks（10 种块——含 `sources` 引用来源块；d3 只做数学、SVG 由 React 渲染；流式文本里的
  `<ginno_citations>` 块渲染时解析/遮罩并折叠为 `SourcesBlock`，web 条目经 `POST /open-external`
  开系统浏览器）、RunBlocks（对话内 workflow 实时块）、
  commandMenu（`/` 与 `@` 补全）、SheetViewer（全屏文件预览）、right/（Artifacts/TODO/Workflow/Memory 面板；可收起为右缘悬停 Dock，宽度可拖拽 280–560px，开合/宽度持久化于 `ginno-right-panel`，`⌘\` 切换；见 design/right-panel-redesign.md）、
  workflow/（**自绘 SVG DAG**，无 reactflow/d3；Inspector/ContextEditor/DiffView/LogTimeline）、
  kb/（**手写力导向图谱**于 SVG；PageViewer 三态 read/edit/create）、settings/（逐 tab 接 API；
  含 WebSearchSettings：引擎配置 + 测试搜索 + 引擎被引率遥测）。

---

## 13. Desktop 壳（`apps/desktop`）

- **零 `#[tauri::command]`、零 plugin**；前端一切能力走 sidecar HTTP/WS。
- `lib.rs` 职责：release 拉起 sidecar（先 `kill_stale_sidecar` 仅杀占用 8787 的 `ginno-runtime`）；
  日志重定向；三种加载路径（dev 直接 devUrl / release 即时就绪 / release 未就绪→data: splash 轮询
  `/api/health` 就绪后 `navigate`）；唯一 Rust→JS 桥 = 原生拖放 `__ginnoFileDrop`。
- `tauri.conf.json`：`frontendDist = http://127.0.0.1:8787`；`dragDropEnabled:true`；`visible:false`
  防白屏；**sidecar 以 Tauri resource（`resources/runtime`）打包进 Contents/Resources**，非 externalBin。
- **macOS 签名**：`signingIdentity` 必须产生真实（非 linker-only）签名，否则 WKWebView 网络助手续
  校验失败 → **白屏**；`make app` 有回归守卫。hardened runtime + entitlements 允许 PyInstaller dlopen。

---

## 14. `~/.ginno` 目录布局

```
~/.ginno/                        # GINNO_HOME 可覆盖
├── settings.json                # providers/default_provider/permissions/hooks/context/knowledge/web/bypass_permissions
├── config.json                  # UI 配置（theme 等）
├── MEMORY.md                    # 全局长期记忆索引
├── todos.json                   # 全局 TODO 列表
├── todo_sync.json               # TODO 外部平台同步台账（append-only，上限 200 条）
├── agents/<id>.json + <id>/     # Agent 定义 + 私有记忆（MEMORY.md + memory/）
├── skills/<name>/SKILL.md       # 全局技能（内置 < 全局 < 项目）
├── mcp/mcp.json                 # MCP 注册表（默认含 playwright）
├── hooks/                       # 用户 hook 脚本
├── memory/pool/*.jsonl          # 每 turn 捕获的助手文本（summarize 原料）
├── knowledge/                   # usage.json（wiki 引用台账）+ web_usage.json（web 遥测）
├── usage/requests-YYYY-MM-DD.jsonl  # 持久 token 用量日志（D2；保留期裁剪）
├── vectorstore/  cache/         # 语义缓存 / 通用缓存
├── logs/sidecar.log             # RotatingFileHandler 5MB×3
├── workflows/<wf_id>/{meta.json, versions/N.json}   # 工作流版本化定义
├── workflow_runs/<run_id>.json + <run_id>.events.jsonl  # run 状态与事件（全局）
├── browser/                     # 内嵌浏览器（§6.14）
│   ├── spaces.json              # Space 注册表（id/name/profileDir/owner/state）
│   ├── profiles/<space_id>/     # 每 Space 独立 Chrome profile（cookies/localStorage 隔离）
│   └── extracts/<space_id>/     # browser_eval 提取的结构化数据（可选持久化）
└── projects/<slug>/
    ├── files.json  artifacts.json  goals.json  skills/
    └── sessions/
        ├── _index.json          # session meta
        ├── <sid>.json           # FileCheckpointer 检查点
        ├── <sid>.world.json     # WorldState baseline
        └── <sid>/{uploads,results}/   # 会话文件（删会话后保留）
```

---

## 15. 打包（`make app`）

```
web（next build → apps/web/out）
  → runtime（uv run --extra docs pyinstaller --onedir --add-data "out:web_out" bin/ginno-runtime.py
             --collect-all langchain/langgraph/mcp/pandas/docx/pptx/pypdf …）
  → sidecar（拷贝 dist/ginno-runtime/ 到 apps/desktop/resources/runtime/）
  → app（解锁签名 keychain + tauri build + linker-signed 回归守卫）→ .app / .dmg
```

- **onedir 而非 onefile**：onefile 每次启动重解 ~3000 文件被 EDR 扫描，冷启动 15–25s；onedir 在
  稳定签名路径只扫一次，重复启动 ~1–2s。
- `make e2e-ui`：打包 UI 的 Playwright 真浏览器 e2e。

---

## 16. 开发与测试

```bash
pnpm install && (cd packages/runtime && uv sync)   # 装依赖
pnpm dev            # 一键起 runtime(:8787) + web(:3000) + desktop
pnpm dev:runtime    # 仅 Python 运行时
pnpm build:web / build:runtime / build:desktop
pnpm test[:unit|:e2e]   # 委托 packages/runtime/scripts/test.sh [-m unit|api|e2e]
```

- **pytest markers**：`unit`（纯单测）/ `api`（FastAPI TestClient）/ `e2e`（完整 LangGraph + WebSocket）。
- **`testing/fake_model.py` 的 `ScriptedChatModel`**：确定性脚本化假 ChatModel，回放固定 AIMessage
  序列（文本 + tool calls），驱动**真实编译的 LangGraph**（工具执行、permission interrupt、checkpointer
  全走）——零网络、无 API key。经环境变量 `GINNO_FAKE_LLM` 接入 `build_model`。
- CI（`.github/workflows/ci.yml`）：hermetic 跑 `pytest -q`（无网络、无 key、空 mcp.json）。

---

## 17. 关键"反直觉"事实（写代码/文档前必读）

1. `bypass_permissions` **默认 ON**——开箱即特权模式，权限策略默认形同虚设，只有 hooks 始终生效。
2. 会话 `workspace` 是 `sessions/<sid>/` 会话目录，**非用户传入路径**。
3. 删除会话**不删文件**；文件目录只在 orphaned 后才能清理。
4. `PUT /api/providers` 会清空所有内存会话（下次 WS 连接重建）。
5. Workflow runs **全局**存放，不在项目目录下。
6. permission interrupt 的重连恢复、goal driver 的跨重启恢复都发生在 **WS 连接建立时**，非 REST。
7. compaction 摘要消息是 **HumanMessage**（前缀 `[conversation summary]`），非 SystemMessage。
8. 图片剥离是 **send-only**（落盘保留）；工具中段截断是**持久化的**——两者语义相反。
9. 没有 `render_chart` 工具，图表是 `render_widget(kind="chart")`；hooks 五事件仅 `PreToolUse` 接线。
10. 打包后 **sidecar 同源托管 UI/REST/WS**，Tauri 不托管任何前端文件、也无任何 tauri command。
11. 旧工具输出会被 **microcompact 静默清空**（保留窗口外、>500 字符 → `[old tool output cleared]`）；
    模型需要时得重新调用工具——这是设计行为不是 bug。
12. `web_fetch` / `POST /open-external` 都带**公网守卫**：主机只解析一次、混合应答整体拒绝、
    **连接钉死在已验证地址**（防 DNS-rebinding TOCTOU）、重定向每跳重验——引用里的 URL 不能当
    跳板探内网。
13. 回复末尾的 `<ginno_citations>` 块是**机器元数据**：历史端点剥离并折叠为 `sources` 块，
    memory pool 捕获也整块剥离——它不进记忆、不当正文显示。
14. server.py 只是 **app 壳**（347 行）：端点在 `api/` 各 router，进程级状态在 `server_shared.py`，
    底部大量 re-export 是历史兼容 facade，别在 server.py 里找业务逻辑。

---

## 18. 路线图（现状快照）

| 阶段 | 范围 | 状态 |
|---|---|---|
| P0–P2 | 骨架 + ReAct + MCP/Skill + Hooks/Permission | ✅ |
| P3 | PyInstaller + Tauri 打包 | ✅ |
| P4 | MEMORY.md + 语义检索（LanceDB） | ✅ |
| P5 | Sessions/Skills/MCP/Memory/Settings UI | ✅ |
| + | 多 Agent 路由、知识库编译/关联、Workflow DSL 引擎、TODO 外部同步 | ✅ |
| + | WorldState 上下文工程 + 失败重试加固 | ✅ |
| + | Goal 长程自主推进（P0/P1） | ✅ |
| + | 聊天内联图表 widget（render_widget chart） | ✅ |
| + | 上下文治理梯度（E2 截断 / E2.5 microcompact / E3 压缩 / E4 重申）+ 持久用量统计 | ✅ |
| + | 引用与来源体系（Wiki + WebSearch：契约/台账/SourcesBlock）+ 内置 web 搜索 | ✅ P0/P1（见 citations-design） |
| + | 内嵌浏览器 M1 + M2 协议层 + atrium 挖洞 + Frameworks + Helper | ✅ Helper.app + C 宿主进包；宿主 CDP 活着才切 native tile，否则 screencast |
| 🔮 | 引用检索加权生效、provider 原生搜索适配、web→Raw 沉淀、经验循环、多项目、真实桌面通知、账号体系 | 路线中 |

> 界面/功能的逐项完成度与已知限制，见 `docs/user-guide.md` 的图例标注。
