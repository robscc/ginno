# Goal 设计（长程目标驱动的自主执行）

> 状态：**已定稿**（评审决议全部合入：无轮数上限/交给自动压缩、移除预算功能、goal 不跟 agent、blocked 走纯提示词纪律、续轮 UI 可见紧凑 context 行）。目标：给 Ginno 引入 Codex 的 Goal 能力——用户为一个会话设定**长程目标**（objective），Agent **跨多轮自主推进**直到完成/受阻，全程有状态与用户控制。参考实现：Codex `ext/goal`（基于 2026-08-03 codex 源码的深度分析）。

## 0. TL;DR

- **Goal = 每会话一个 objective**（≤4000 字）+ 累计时长/轮数。**无预算功能**（评审已定：去掉 token 预算）。状态机 5 态：`active / paused / blocked / usage_limited / complete`。
- **自主推进（核心机制）**：一轮结束且 goal 仍 `active` → 服务端自动注入"continuation 消息"开启下一轮，无需用户在场。**续跑无轮数上限**，上下文大小完全交给现有自动压缩（`maybe_compact_history`，E3，阈值默认 500k tokens）；objective 每个续轮从 store 重新注入，天然不怕压缩丢失。用户消息永远优先；打断（stop）= 暂停 goal。
- **模型只有 3 个工具**：`goal_get / goal_create / goal_update`；模型只能标 `complete`（真完成）或 `blocked`（连续 3 个 goal 轮同一阻塞），pause/resume 由用户控制。
- **注入走消息而非 system prompt 主体**：continuation 用隐藏 user 消息模板（`<ginno_goal kind=…>`）；活跃 goal 的**存在性**进 WorldState 分节（ginno 原生增强，Codex 没有）。
- **Goal 不跟 Agent**：goal 快照设定时的 agent；切换 Agent 会自动暂停 goal（切回也不自动恢复，需手动 resume）。
- **安全**：grace 期（每轮间留 3s 给用户插话）+ 权限 interrupt 挂起时不续跑 + turn 出错自动置 blocked 防循环。
- **落点**：`goals.json`（每项目一个，按 session_id 索引，原子写）+ `tools/goal_tools.py` + server 侧 **goal driver**（每会话一个 asyncio task，不依赖 WS 连接）+ REST `/api/sessions/{sid}/goal` + WS `goal.updated/cleared` + TopBar 状态 chip/popover + `/goal` 命令。

---

## 1. Codex Goal 实现摘要（事实基础）

> 来源：`codex-rs/ext/goal/`、`state/src/runtime/goals.rs`、`app-server/src/request_processors/thread_goal_processor.rs`、TUI goal 组件（三路深度调查，2026-08-05）。

### 1.1 数据模型与持久化

- **每 thread 恰好一个 goal**，独立 SQLite（`goals_1.sqlite`，表 `thread_goals`，`thread_id` 主键）：

```
{ thread_id, goal_id(UUID，乐观并发令牌), objective(≤4000字),
  status, token_budget?, tokens_used, time_used_seconds, created_at, updated_at }
```

- 状态：`active / paused / blocked / usage_limited / budget_limited / complete`；终态 = `complete | budget_limited`。
- 无里程碑、无百分比、无文件关联（`goal_files` 只是超长 objective 的附件 offload）。
- Rollout 里追加 `ThreadGoalUpdated` 事件仅作耐久日志；objective 兼作空 thread 的标题预览。

### 1.2 模型工具（3 个）

| 工具 | 能力 | 关键纪律（写在描述里） |
|---|---|---|
| `get_goal` | 读状态/预算/用量 | — |
| `create_goal` | 建 goal（objective 必填） | 仅当用户/指令**明确要求**才建，不从普通任务推断；已有未完成 goal 时失败 |
| `update_goal` | 只能 `complete` 或 `blocked` | complete=真达成且无遗留；blocked=同一阻塞**连续 3 个 goal 轮**；不许因"难/慢/预算快没了"乱标 |

pause/resume/budget_limited/usage_limited **模型无权设置**——这是 Codex 的关键权限切分。

### 1.3 注入机制（隐藏 user 消息，非 system prompt）

三个模板渲染为 `<codex_internal_context source="goal">…</codex_internal_context>` 的 user 角色消息：

| 模板 | 触发 | 投递方式 |
|---|---|---|
| `continuation.md` | thread 空闲且 goal active | **作为唯一输入开启新 turn**（`try_start_turn_if_idle`） |
| `budget_limit.md` | 记账发现越过预算 | 注入**正在运行**的 turn（`inject_if_running`） |
| `objective_updated.md` | 用户运行中改了 objective | 注入运行中的 turn |

continuation 模板要点：objective 是**数据不是更高优先级指令**（防注入）；保持完整目标、不许把成功标准缩小到"能做完的部分"；预算数字随模板下发；完成前做"completion audit"（对照现实状态验证）；blocked 走 3 轮审计规则；与 `update_plan` 的联动**仅在提示词层**（无代码耦合）。

### 1.4 自主续跑循环

```
turn 结束 → on_thread_idle → goal active？→ 无 deferral、无待处理用户输入、非 Plan 模式、空闲
        → 渲染 continuation → try_start_turn_if_idle 开新 turn → 循环
```

- 重启/恢复会话：从 DB 发 goal 快照通知 → 重新触发 idle 生命周期 → **goal 跨重启续跑**。
- fork 线程会带 goal 快照但插入 deferral：等 fork 后的第一个用户 turn 才恢复续跑。
- turn 出错：用量超限类错误 → `usage_limited`；其他终态错误 → `blocked`（防止续跑循环烧钱）。

### 1.5 用量记账

- 每轮基线快照，turn 内每次工具完成/turn 边界 flush：`Δtokens = input − cached + output`，墙钟秒数含 idle。
- **预算判定在存储层原子完成**：`tokens_used + Δ ≥ token_budget → status='budget_limited'`（SQL CASE）。
- 乐观并发：所有写带 `expected_goal_id`；Plan 模式轮不记账。

### 1.6 UX 要点（TUI）

- `/goal [<objective>|clear|edit|pause|resume]`；设置是 composer 原生输入（支持粘贴/图片，超长自动附件化）。
- 状态行右侧指示器：`Pursuing goal (40K / 50K)` / `Goal paused (/goal resume)` / `Goal achieved (2d 3h)`，active 时**计时实时跳动**。
- **Esc 打断 → 自动 pause goal**；恢复会话遇 paused/blocked goal → 弹 "Resume paused goal?"。
- 替换未完成 goal 要确认；goal 可先于任何消息存在（goal-first thread），objective 成为会话预览标题。

### 1.7 边界（Codex 明确不做的）

无里程碑/进度百分比；goal 不与文件/todo/cloud 任务耦合；MCP 面不暴露 goal；Plan 模式与 goal 续跑互斥。

---

## 2. Ginno 现状摘要（可嫁接原语）

| 维度 | 现状 | 文件 |
|---|---|---|
| 状态存储 | 无 DB，全文件：`todos.json` / workflows / artifacts 各 JSON + 原子写范式 | `todos/store.py`、`workflows/store.py` |
| 会话 | LangGraph checkpoint（`sessions/<id>.json`）+ 元数据；WS 每会话；turn 由客户端 invoke 触发 | `server.py` |
| turn 生命周期 | `turn_start/turn_done` 日志、`_RUNNING_TURNS` 注册表、`turn_state` 探测、15s keepalive、**无 turn 级 stop** | `server.py` |
| 多 socket 广播 | turn 事件已广播到会话**所有** socket（`_SESSION_WS` + safe_send 遍历） | `server.py:222-233, 3408-3423` |
| 用量 | 每次 LLM 调用抽 usage（input/output），`_USAGE_BY_SESSION` 累计，`usage` WS 事件 + `/usage` 端点 | `server.py:3567-3583` |
| WorldState | 分节快照-diff：渲染进 system prompt + `context.updated` 通知 + 更新消息 | `world_state.py` |
| 命令 | 服务端 slash 注册表（`/help`、skill 命令），`notice` 事件直回 | `commands/registry.py` |
| 右栏 | TODO / Workflow / Artifacts / Memory 面板；TopBar 有会话级状态区 | `components/right/`、`TopBar.tsx` |
| 先例迁移模式 | `ensure_todo_tools()` 幂等合并 tools_allow；`ensure_research_discipline()` 条件升级 prompt | `agents/registry.py` |

**结论**：存储/工具/注入/UI 原语齐备；需要新建的是 **goal store、goal 工具、goal driver（服务端自主续跑）、REST/WS 面、TopBar 状态 UI**。最大的结构性工作：turn 执行目前绑定单个 WS handler（`_run_turn(ws, …)`），自主续跑需要**无客户端 socket 也能跑 turn** 的 headless 路径。

---

## 3. 产品方案

### 3.1 定位：Goal 与 Workflow / TODO 的三分

| | TODO | Workflow | **Goal** |
|---|---|---|---|
| 回答 | 要做什么（清单） | 怎么做（**已知**流程） | 达成什么（**开放**目标） |
| 执行 | 人/Agent 逐项处理 | DSL 编译成图，确定性执行 | Agent 自主多轮推进，路径未知 |
| 典型 | "更新 API 文档" | "深挖主题→评审→发布" | "把 X 调研清楚并产出报告"、"让这个仓库的测试全绿" |

Goal 是**会话内**的（一个会话至多一个活跃 goal），不跨会话；与 TODO/Workflow 不做强耦合（Codex 教训：prompt 层联动即可，代码耦合留白）。

### 3.2 用户旅程

**A. 设定目标**
1. 会话内输入 `/goal <objective>`（或会话头 "设定目标" 按钮 → 编辑器弹窗，支持粘贴长文）；
2. 已有未完成 goal → 确认卡"替换目标？"（展示旧 objective 摘要）；
3. 设定成功 → TopBar 出现 goal chip：`🎯 推进中 · 0m`；该轮若空闲，**立即开始自主推进**。

**B. Goal-first 会话**（P2）
- 侧栏"新建 Goal 会话"：先填 objective 再开会话，objective 自动成为会话标题（Codex goal-first thread 对齐）。

**C. 自主推进中**
- 每轮结束短暂 grace（默认 3s）后自动开下一轮；对话流里每个续轮有一条 context 行"目标推进 #N"（可折叠，不干扰阅读）；
- 用户**随时可以发消息**——用户消息优先，续跑让位；
- TopBar chip 实时显示：状态 + 用时；点击展开 popover：objective 全文、用时、轮数（"自主推进 #N"）、操作按钮。

**D. 控制操作**（popover / `/goal` 子命令）
| 操作 | 效果 |
|---|---|
| 暂停 | 当前轮跑完后不再续轮，status=paused |
| 恢复 | status=active，空闲则立即续跑 |
| 编辑 | 弹窗改 objective（运行中修改 → 注入"目标已更新"转向消息） |
| 清除 | 删除 goal（含用量记录） |
| Stop 按钮 | goal active 时按 stop = **暂停 goal** + 停止续跑（对齐 Codex Esc→pause） |

**E. 结束形态**
- `complete`：chip 变 `🎯 已达成 · 2m`，最后一条回复含模型对达成情况的总结；
- `blocked`：chip `🎯 受阻`，模型说明阻塞原因，等用户介入（用户回复后按"恢复"续跑，blocked 计数重置——Codex fresh-audit 规则）。

**F. 跨重启**
- 关闭 App 再打开：goal 持久化，打开会话时 chip 恢复；active goal 在会话加载后自动续跑；paused/blocked 出现恢复横幅（"目标已暂停，是否恢复？"）。

### 3.3 显示与文案

- 状态标签：`推进中 / 已暂停 / 受阻 / 用量受限 / 已达成`（对齐 Codex goal_status_label，blocked 显示"受阻"）。
- chip 颜色：active=主题橙、paused=灰、blocked=红、complete=绿。
- 续轮 context 行：`目标推进 #3 · 已用 2m`（居中 chip 样式，复用 world-state context row）。

---

## 4. 技术方案

### 4.1 数据模型与存储

`~/.ginno/projects/<slug>/goals.json`：按 session_id 索引的 map，原子写（tmp+rename，todos 范式）。

```python
{
  "<session_id>": {
    "goal_id": "a1b2c3…",          # uuid hex，乐观并发令牌
    "objective": "…",              # ≤4000 字
    "status": "active",            # active|paused|blocked|usage_limited|complete
    "time_used_seconds": 0,        # 各 goal 轮时长累加（展示用）
    "turns_used": 0,               # goal 轮计数（含续轮，展示"自主推进 #N"）
    "agent_id": "research",        # 设定时快照（goal 不跟 agent：driver 校验 + 展示用）
    "created_at": 1730000000.0,
    "updated_at": 1730000000.0
  }
}
```

- **级联删除**：session 删除时清 goal（删会话的既有路径加一行）。
- 不引入 SQLite（对齐架构原则 No database）；写入频率（每轮 1 次轻量记账）对 JSON 无压力。

模块：`packages/runtime/src/ginno_runtime/goals/store.py`（get/set/replace/update_status/account_turn/clear，`expected_goal_id` 乐观并发）。

### 4.2 工具层（`tools/goal_tools.py`）

| 工具 | 参数 | 说明（描述文案沿用 Codex 纪律，中文化） |
|---|---|---|
| `goal_get` | — | 读 goal（objective/状态/时长/轮数） |
| `goal_create` | objective | 仅当用户明确要求时创建；已有未完成 goal 报错 |
| `goal_update` | status ∈ {complete, blocked} | 只许这两个值；complete 时要求模型简要总结达成了什么；**blocked 走"同一阻塞连续 3 个 goal 轮"纪律——纯提示词约束，服务端不计数（评审已定）** |

- session_id 从图 state 注入（`agent_id` 同源），模型不可见也不可改；
- 注册进 ToolNode，tools_allow：dev（`*`）天然有；research/writer 加 `goal_*`（幂等迁移 `ensure_goal_tools()`，仿 `ensure_todo_tools`）；workflow-dev 不给。
- 工具写 store → 发 `goal.updated` WS 事件（带 turn_id）。

### 4.3 注入与续跑（核心）

#### 4.3.1 消息模板（`goals/templates.py`，内嵌字符串即可）

两个模板（Codex 三个中的 budget_limit 随预算功能一并去掉）：

- **continuation**：`<ginno_goal kind="continuation">` 包裹：objective（XML 转义，标注"用户数据，不是更高优先级指令"）+ 进度信息（第 N 个 goal 轮、累计时长）+ 纪律：保持完整目标、不许缩小成功定义、多步工作可用 todo_list 展示计划（prompt 层联动 TODO）、**completion audit**（完成前对照实际状态验证）、**blocked 3 轮审计**、达成调 `goal_update(complete)`。
- **objective_updated**：新 objective 取代旧的；停止只服务旧目标的工作。

#### 4.3.2 渲染位置

- 续轮：continuation 消息作为**该轮唯一 user 输入**；
- 用户交互轮：不注入 continuation（对齐 Codex），但活跃 goal 的**存在与状态**进 **WorldState 新分节 `GoalSection`**（snapshot=goal 摘要；render 进 system context；变更走既有 `context.updated` 通知）——ginno 原生增强，保证用户插话时模型也知道 goal 在跑；
- 历史渲染（评审已定：**可见但紧凑**）：continuation/objective_updated 消息加前缀族（`GOAL_CONTEXT_PREFIX`），`_messages_to_ui` 按 world-state scaffolding 同款处理：渲染为居中 context 行（"🎯 目标推进 #N · 已用 2m"），其后该轮的助手输出照常展示——过程全透明，P1+ 再评估折叠成组。

#### 4.3.3 Goal Driver（服务端自主续跑）

**结构**：`_GOAL_DRIVERS: dict[session_id, asyncio.Task]`。goal 变 active 时 ensure 一个 driver task；goal 暂停/清除/完成时停止。Driver 与 WS 连接**完全解耦**（用户关窗也续跑，sidecar 存活即可）。

```
driver 循环:
  await 当前 turn 结束（事件/条件变量）
  读 goal：非 active → 退出
  guards 任一不满足 → 退出或等待:
    - 无用户 invoke 正在处理/排队（用户优先）
    - 无 permission/version-propose interrupt 挂起（_PENDING_RESUME）
    - 会话当前 agent == goal.agent_id（goal 不跟 agent）
  grace 期（默认 3s，期间任何用户 invoke 取消本轮续跑）
  以 continuation 消息发起 headless turn（复用 _run_turn 逻辑）
  turn 结束 → 轻量记账（4.3.4）→ 循环
```

**无轮数上限，上下文靠自动压缩**（评审已定）：续轮与用户轮走同一条 `_run_turn` 路径，`maybe_compact_history`（E3，每轮开始前检查，阈值 `settings.context.compact_threshold_tokens`，默认 500k）自动生效，无需 goal 专属处理。**objective 不依赖历史存活**——每个续轮的 continuation 模板都从 store 重新渲染完整 objective，压缩摘要里有没有它都不影响续跑。

**Goal 不跟 Agent**（评审已定）：goal 记录设定时的 `agent_id` 快照。会话切换 Agent 时（PATCH session / @agent 路由覆盖）若存在 active goal 且新 agent ≠ goal.agent_id → goal 自动置 `paused`，发 `goal.updated` + context 行"目标属于 <agent 名>，已因切换 Agent 暂停"。切回原 agent **不自动恢复**，需用户手动 resume（可预期、不意外）。

**前置重构**（本方案最大改动）：`_run_turn`/`_stream_graph` 目前闭包绑定单个 `ws` 的 `safe_send`。抽出 **`SessionTurnRunner`**：发送目标 = 该会话 `_SESSION_WS` 全集（现有广播语义不变），无 socket 时丢弃帧但 turn 照常执行/持久化。invoke 路径与 driver 路径共用此 runner。

**用户优先的竞态处理**：grace 期与 driver 等待期都检查 invoke 到达（asyncio.Event）；用户 turn 结束时 driver 再评估。对齐 Codex `try_start_turn_if_idle` 拒绝 `PendingTriggerTurn` 的语义。

**跨重启**：App 重开 → 会话首次加载（WS 连接/历史请求）时检查 goal：active → 重启 driver（等价 Codex resume 快照 + idle 重触发）。

#### 4.3.4 轻量记账（每轮边界）

turn_done 时若该轮 goal-active：
- `Δtime = turn_done_ts − turn_start_ts` → `time_used_seconds += Δtime`；`turns_used += 1`；
- `store.account_turn(session_id, Δtime, expected_goal_id)` 一次原子写。
- **不做 token 记账**（预算功能已移除；会话级用量仍走既有 `usage` 事件/`/usage` 端点，与 goal 无关）。

### 4.4 API / WS 协议

REST（对齐 ginno 端点风格）：

| 方法 | 路径 | 语义 |
|---|---|---|
| GET | `/api/sessions/{sid}/goal` | `{goal: … | null}` |
| PUT | `/api/sessions/{sid}/goal` | body `{objective?, status?}`：设/换 objective、改状态（pause/resume）；返回 `{goal}`；替换未完成 goal 需 `confirm=true`（否则 409，前端弹确认） |
| DELETE | `/api/sessions/{sid}/goal` | 清除 |

WS 事件（session 通道广播）：`goal.updated {goal, turn_id?}`、`goal.cleared {}`。前端 `store.tsx` 挂 `goalBySession`。

`/goal` 命令（`commands/registry.py`）：
- `/goal`（裸）→ notice 展示摘要（状态/objective/用量 + 可用子命令提示，对齐 Codex goal_summary）；
- `/goal pause|resume|clear` → 对应状态操作 + notice 回执；
- `/goal <text>` → 设定（未完成 goal 存在时返回需确认的 notice，前端接管弹确认）；
- `/goal edit` → notice 携带 `{action:"open_editor", objective}`，前端开编辑器。

### 4.5 前端

| 组件 | 内容 |
|---|---|
| TopBar goal chip | 状态色点 + 标签 + 计时（active 每秒走）；点击开 popover |
| GoalPopover | objective 全文、进度（用时、轮数"自主推进 #N"）、操作：暂停/恢复/编辑/清除 |
| GoalEditorModal | 设定/编辑 objective（多行，粘贴长文） |
| 替换确认卡 | 复用 ConfirmModal（permission 确认交互范式） |
| 恢复横幅 | 打开 paused/blocked goal 会话 → 横幅"恢复目标？" |
| Stop 按钮语义 | goal active 时：stop = 暂停 goal + 停续跑（当前轮跑完）；toast"目标已暂停" |
| 续轮 context 行 | 复用 system context row 渲染 |

### 4.6 安全与边界

1. **续跑无轮数上限**（评审已定）：护栏改为——上下文交给自动压缩（阈值默认 500k）；turn 出错自动置 `blocked` 防错误循环；用户随时可暂停/清除；
2. grace 期默认 3s；
3. 权限/版本确认 interrupt 挂起时不续跑；
4. objective 注入模板时 XML 转义 + 明确"用户数据"定位（防 prompt 注入升级）；
5. settings 全局开关 `goals.enabled`（默认 true）；
6. 用量受限：provider 限流/额度错误 → `usage_limited`（P2，先归 blocked）。

### 4.7 与既有系统的关系

- **WorldState**：新增 `GoalSection`（4.3.2）；
- **TODO**：continuation 模板提示"多步工作可用 todo_list 列计划"——仅 prompt 层（Codex 同款边界）；
- **Workflow**：不耦合。定位互补（§3.1）；goal 轮里模型可以触发 workflow，但那是普通工具调用；
- **Agents**：**goal 不跟 agent**（评审已定）——goal 快照设定时的 agent_id，切走即自动暂停，切回不自动恢复（4.3.3）；research agent 是 goal 的头号场景（结合 KB Research 目录产出报告）；
- **Compaction**：续轮走既有 turn 路径，压缩自动生效且对 goal 透明（objective 每续轮重注入）；
- **Memory/KB**：无特殊集成。

---

## 5. 分期

> 实现状态（2026-08-06）：**P0、P1 已完成并验证**（单测 + e2e + 打包截图）。P2 未做。

**P0（MVP）— 已完成**
1. `goals/store.py` + session 删除级联；
2. `goal_*` 三工具 + `ensure_goal_tools` 迁移；
3. SessionTurnRunner 重构（headless turn）；
4. goal driver（续跑循环 + guards + grace + agent 校验）+ continuation 模板；
5. REST 三端点 + WS `goal.updated/cleared`；
6. 前端：TopBar chip（状态展示）+ `/goal <text>` 设定 + 续轮 context 行渲染。

**P1 — 已完成**
7. 暂停/恢复/编辑/清除全量操作 + popover + 编辑器弹窗 + 替换确认；
8. stop=暂停 goal（composer 运行中且 goal active 时出现停止键）；切 Agent 自动暂停；恢复横幅；
9. WorldState `GoalSection`（交互轮也感知 goal）；objective 变更经 `update_text` 走 context.updated 通告。

**P2 — 未做**
10. Goal-first 会话入口（objective 即标题）；
11. turn 级真正打断（graph 中断）；usage_limited 映射；
12. goal→TODO 联动探索（完成时建议归档为 TODO 等）。

---

## 6. 评审决议记录（全部已定）

1. **续跑无轮数上限**：上下文大小交给现有自动压缩（`compact_threshold_tokens` 默认已从 100k 改为 **500k**）；objective 每续轮重注入，不受压缩影响；
2. **移除预算功能**：token_budget / budget_limited / token 记账 / budget_limit 模板全部不做；仅保留时长/轮数展示；
3. **blocked 3 轮审计 = 纯提示词纪律**（选 A，Codex 同款）：规则写在 `goal_update` 描述与 continuation 模板里，服务端不计数；若日后观察到模型过早摆烂，再加一行轮数兜底；
4. **续轮 UI = 可见但紧凑**（选 A）：居中 context 行 `🎯 目标推进 #N` + 该轮输出照常展示，过程透明；折叠成组留待 P1+ 按需评估；
5. **goal 不跟 agent**：设定时快照 agent_id；切走自动暂停并提示；切回不自动恢复，手动 resume。
