# Workflow DSL 设计（LangGraph 图驱动）

> 状态：设计稿（待评审）。目标：把 workflow 从「LLM 自报进度的标题清单」升级为「由沉淀的 DSL 编译成 LangGraph 图、可重跑、可观测、可对话式版本化编辑」的一等公民。

## 0. TL;DR

- **DSL** 是单一事实源：`nodes`（step/branch/loop/human/subflow）+ `edges`（含条件）+ `context`（schema+初始值）+ `entry`，直接 1:1 编译成 LangGraph `StateGraph`。
- **总结**：复用现有 `memory/summarize` 的「LLM 提炼」范式，换 prompt + 结构化输出，从**整段 session 历史**（checkpointer 的 messages）提炼出 DSL 草稿。
- **执行**：每次 run **fork 一个 agent 副本**（复制 prompt/tools、干净记忆），用**独立 thread** 跑编译出的图；step 在该 thread 内调 model+tools，历史天然干净。
- **上下文**：一等公民 `WorkflowContext`，可携带（总结时带入）、可查看、可编辑（运行前改 inputs、运行中暂停改 current）。
- **日志**：图执行的事件流（node/tool/token/error）append-only 落盘成 `events.jsonl`，SSE/WS 实时推，详情页可看。
- **版本化**：def 改为 `current` 指针 + `versions/<n>.json` 全量快照；每次改动记 version。
- **对话式编辑**：内置 `workflow-dev` Agent，绑定到某 workflow 的专属 session；它产出的修改以 **diff 卡片**（复用 permission 确认交互）让用户 Apply/Reject，Apply 才落新版本。
- **Supervisor**：图里的可选监控节点，auto/human 两态，本期占位、接口留好。

---

## 1. 现状摘要（事实基础）

| 维度 | 现状 | 文件 |
|---|---|---|
| 执行模型 | **非图**：LLM 调 `workflow_run` 后在普通对话轮里自调 `workflow_step(..,'done')` 自报进度；step 工作发生在同一轮 | `tools/workflow_tools.py`、`graph.py:95-101` |
| DSL/数据 | steps = `{id,title,agent_id?}`，无边/分支/输入输出；`update_def` 原地覆盖，**无版本** | `workflows/store.py`、`lib/types.ts` |
| 持久化 | def/run 各一 JSON，**非原子**写、无锁；run 与 session **未关联**（`session_id` 参数存在但没传） | `workflows/store.py` |
| 可复用资产 | checkpointer 全快照链+parent（可 time-travel/fork）；agent fork≈10 行；`summarize_pool` LLM 提炼范式；bypass 自主运行；前端 hand-rolled SVG 图（`kb/GraphView.tsx`，无三方库）；KB 两栏+sticky inspector（`PageViewer` read/edit/create）；permission 确认卡 = 现成「diff 确认」交互；`newSession(agent_id)` + composer `target` | 见各处 |
| 日志 | WS `token.delta/tool.*/workflow.emit` **转瞬即逝**；run 仅每步一个 `output` 字符串；memory pool 仅记 assistant 文本 | `server.py`、`memory/pool.py` |

**结论**：执行引擎、DSL schema、版本、日志、run↔session 关联、详情页——均需新建；但每一项都有可嫁接的现成原语。

---

## 2. 设计原则

1. **DSL 即图**：DSL 不另造执行语义，编译产物就是 LangGraph 图，复用其 checkpoint/interrupt/stream。
2. **复用而非平行**：fork agent、复用 provider/model/tools；不重写 agent loop，step 内部直接复用 `agent_node`/`ToolNode` 逻辑。
3. **可观测优先**：执行的一切（节点进出、工具、token、错误、上下文变更）都是事件流，落盘 + 实时推。
4. **人在环上而非环内**：默认自主跑（bypass/独立 thread）；需要人时通过 interrupt（human 节点 / supervisor=human / diff 确认）显式暂停。
5. **版本不可变**：run 钉死 `dsl_version`；编辑只产生新版本，旧 run 永远可复现。

---

## 3. DSL Schema

```jsonc
{
  "dsl_version": "1",                 // DSL 语言版本（schema 演进用），与内容版本 workflow.version 区分
  "name": "PR Triage",
  "description": "拉取待审 PR → 分类 → 起草评审意见 → 高优先级通知",
  "entry": "fetch",

  "context": {                        // 一等公民：可携带/可查看/可编辑
    "schema": {                       // JSON Schema，驱动 UI 表单 + 校验
      "type": "object",
      "properties": {
        "repo":   { "type": "string" },
        "prs":    { "type": "array",  "items": { "type": "object" } },
        "drafts": { "type": "array",  "items": { "type": "string" } }
      },
      "required": ["repo"]
    },
    "initial": { "repo": "robscc/ginno" }   // 重跑时的默认输入（UI 可改）
  },

  "nodes": [
    { "id": "fetch",   "type": "step",   "agent": "research",
      "goal": "用工具列出 {{context.repo}} 的待审 PR，写入 context.prs",
      "tools_allow": ["mcp_*", "read_*"], "writes": ["prs"] },

    { "id": "each",    "type": "loop",   "over": "context.prs", "as": "pr",
      "body": "review", "max_iters": 50, "parallel": false },

    { "id": "review",  "type": "step",   "agent": "dev",
      "goal": "审查 {{pr}}，产出评审意见字符串，追加到 context.drafts",
      "writes": ["drafts"] },

    { "id": "gate",    "type": "branch",
      "cases": [
        { "when": "len(context.drafts) > 0 and any_high_priority(context.prs)", "then": "notify" }
      ],
      "default": "done" },

    { "id": "notify",  "type": "step",   "agent": "writer",
      "goal": "把 context.drafts 汇总成一条通知发出去" },

    { "id": "done",    "type": "step",   "agent": "dev", "goal": "输出一行总结" }
  ],

  "edges": [
    { "from": "fetch",  "to": "each" },
    { "from": "each",   "to": "gate" },
    { "from": "notify", "to": "done" }
    // branch 的出边由 cases/default 描述，不在此重复
  ],

  "supervisor": { "enabled": false, "mode": "human" }   // 占位，见 §9
}
```

**节点类型**（v1 实装前四种，`subflow` 延后）：

| type | 语义 | 编译为 |
|---|---|---|
| `step` | 让指定 agent 达成 `goal`（可多轮 tool 调用），结果写回 `context[writes]` | 复用 agent loop 的子图/子 thread |
| `branch` | 对 `context` 求值 `cases[].when`，首个命中走 `then`，否则 `default` | 条件边 |
| `loop` | 遍历 `over`（列表/范围/`while` 条件），每轮跑 `body` 节点；`parallel` 用 LangGraph `Send` 扇出 | 回边 / Send |
| `human` | `interrupt`，暂停并把控制权交给 UI（可编辑 context 后 resume） | `interrupt()` |
| `subflow` | 嵌套引用另一 workflow（带版本） | 子图（**v2，已决 Q1 延后**） |

> **v1 范围（已决 Q1）**：实装 `step` + `branch` + `loop` + `human`；`subflow` 与 `parallel` 默认关闭、延后到 v2。`loop.parallel` 字段保留但 v1 忽略。

**表达式**：`when`/`over`/`goal` 里的 `{{...}}` 与条件用**受限表达式**（沙箱求值，仅可访问 `context` + 白名单函数如 `len/any/all/min/max`，**禁止任意代码**）。v1 可先用 Jinja2 沙箱（`goal` 模板）+ 简单 AST 求值器（`when`/`over`），避免引入 exec。

**校验**：`entry` 存在；图连通且无悬空边；`writes` 字段都在 `context.schema` 内；loop 有 `max_iters` 兜底；branch 至少 default 或全覆盖。校验在保存/编译前跑，错误精确到 node id。

---

## 4. Session → DSL 总结

复用 `memory/summarize.py` 的范式（`build_model` + `ainvoke([System, Human])` + 结构化输出），差异在**输入**与**输出**：

- **输入**：不是 assistant-only 的 memory pool，而是**整段 session 历史**——从 checkpointer 取 `channel_values.messages`（已有 `_messages_to_ui` 的同源数据），压成「用户意图 + 每步用了什么工具/产出了什么 + 分支/循环线索」的精简轨迹（token 预算内截断/摘要）。
- **输出**：用 LangChain `with_structured_output(DSLSchema)`（或 prompt 约束 + JSON 校验 + 一次自修）产出**带 `context` 的 DSL 草稿**：把对话里出现的数据/参数抽成 `context.schema`+`initial`，把反复出现的「对列表每一项做 X」识别成 `loop`，把「如果…则…」识别成 `branch`。
- **上下文带入**：总结时同时产出一份「执行上下文快照」（对话中已知的 repo/PR 列表/偏好等）作为 `context.initial` 的**建议值**，用户可在重跑前改。
- 落点：`POST /workflows/{id}/summarize-from-session { session_id }` → 返回 DSL 草稿（**不直接覆盖**），走 §8 的 diff 确认后才成为 version 1。

> 这一步产出的 DSL 必然是「建议」，必须经人确认——与目标 5 的 diff 确认共用同一交互。

---

## 5. 执行模型

### 5.1 编译：DSL → LangGraph

`compiler.py`：`StateGraph(WorkflowState)`，每个 node 按 type 加节点，edges/branch/loop 加边，`entry` 为入口，`END` 为终止。`WorkflowState`：

```python
class WorkflowState(TypedDict):
    context: dict            # 当前 WorkflowContext.values（可变，受 schema 约束）
    context_meta: dict       # 每字段来源：initial|step:<id>|human|supervisor
    results: dict            # node_id -> 结构化结果
    events: Annotated[list, add]   # 事件流（也外置落盘，见 §7）
    loop_iters: dict         # loop_id -> 当前迭代
    pending: dict            # interrupt 载荷（human / supervisor=human / diff）
```

### 5.2 fork agent + 干净历史

- run 启动时 `fork_agent(source_agent_id, run_id)`：复制 `system_prompt/tools_allow/provider/model/icon/color`，新 id `wf-<run8>-<src>`，**新建空记忆**（`ensure_agent_memory` 自动建空目录，不复制 → 干净）。
- 该 fork agent 绑定一个**独立 thread**（`thread_id = run_id`，独立 checkpointer 文件）→ 与用户原 session 完全隔离，历史干净。
- provider/model 因复制字段而**自动与原 agent 相同**（`_resolve_provider_model` 链）。

### 5.3 step 节点执行

step 节点 = 在该 run 的 thread 内跑一次「带 goal 的 agent turn」：把 `goal`（模板渲染 `context`）+ 当前 `context` 视图作为指令，调 `model.bind_tools(allowed)` 循环直到模型不再调工具或产出写回值；工具调用走现有 `ToolNode`。step 的「写回」= 模型按 `writes` 产出结构化字段（用 structured output 或约定 JSON 块），合并进 `state.context` 并记 `context_meta`。

> 隔离粒度（**已决 Q2：单 thread 全图**）：每 step 在同 thread 内以一段带标记的 messages 执行，日志连贯、实现最简；step 间靠 `context` 显式传值而非共享对话历史。若将来需要 step 强隔离，可切「每 step 子 thread」，对外 API 不变。

### 5.4 运行入口

- `POST /workflow_runs { workflow_id, dsl_version?, context_override?, from_node? }` → 建 run（钉 `dsl_version`）、fork agent、`graph.astream(stream_mode=["updates","messages","custom"])` 驱动；`from_node` 支持「从某步重跑」（结合 checkpointer time-travel / 或重放 context 到该节点）。
- `POST /workflow_runs/{id}/cancel`、`/resume { context_patch? }`（human/supervisor 恢复）。
- run 记录新增 `dsl_version`、`fork_agent_id`、`thread_id`、`source_session_id`（总结来源，可空）。

---

## 6. 上下文模型

- **存储**：run 级，随 `WorkflowState.context` 在 checkpointer 里快照（每节点一次 → 天然可 time-travel 看「当时 context」）；另存一份 `runs/<id>/context.json` 便于直读。
- **可查看**：`GET /workflow_runs/{id}/context` 返回 `{ values, meta, schema }`；详情页「上下文」tab 按 `schema` 渲染表单 + 原始 JSON 双视图。
- **可编辑**：
  - 运行前：`context_override` 覆盖 `initial`（重跑时 UI 表单预填上次值）。
  - 运行中：仅当图处于 `interrupt`（human 节点 / supervisor=human）时，`POST .../resume { context_patch }` 合并并校验 schema 后继续；非暂停态不允许改（保证可复现）。
- **来源追踪**：`context_meta` 标每字段最后由谁写（initial / step:id / human / supervisor），详情页可见、便于排查「这个值哪来的」。

---

## 7. 执行日志

- **事件流**：图 `astream` 的 `updates/messages/custom` 统一转成事件，append 到 `runs/<id>/events.jsonl`（仿 `memory/pool.py` 的 append 模式）：
  `{ ts, run_id, node_id, node_type, kind, data }`，`kind ∈ {node_enter,node_exit,tool_call,tool_result,token,context_write,branch_decision,loop_iter,interrupt,resume,error}`。
- **实时推**：`GET /workflow_runs/{id}/events/stream`（SSE）+ WS 事件 `run.event { run_id, event }`、`run.status`。详情页/Supervisor 独立页**自带连接**（现状仅 ChatStream 监听 WS，新页面需自建 socket 或 SSE——见衔接表）。
- **过滤**：`GET /workflow_runs/{id}/events?node_id=&kind=`；详情页点节点 → 过滤该节点日志。
- **与 chat 的关系**：workflow run 的执行**不**塞进用户 chat 历史（隔离 thread）；chat 里只嵌一张 `WorkflowBlock` 卡（现状已有）+ 一个「查看详情」跳转。

---

## 8. 版本化 + 对话式编辑（Workflow 开发 Agent）

### 8.1 存储布局

```
~/.ginno/workflows/<id>/
  meta.json                 # { id, name, current: 3, versions: [1,2,3] }
  versions/1.json           # 全量 DSL 快照 + { commit, created, source }
  versions/2.json
  versions/3.json
  dev_session.json          # 绑定到本 workflow 的「开发 session」id（见下）
```

`update_def` 改为「写新版本 + 推进 current」（旧 `workflows/<id>.json` 单文件做一次性迁移）。run 钉 `dsl_version` → 旧 run 永远可复现。

### 8.2 API

- `GET /workflows/{id}/versions`、`GET .../versions/{n}`、`GET .../versions/diff?a=&b=`（后端算 unified diff，DSL 先 canonical-pretty 再 diff）、`POST .../rollback { to }`（= 用旧快照建新版本，不删历史）。

### 8.3 对话式编辑

- 内置 seed agent **`workflow-dev`**：`tools_allow` 限定为 workflow 读写 + diff 工具；`system_prompt` 内置「你正在编辑 workflow X 的 DSL，遵守 schema，改动必须经 propose_edit」。
- 每个 workflow 一个**专属开发 session**（`workflows/<id>/dev_session.json` 记录 id；首次「打开开发会话」时 `newSession(workflow-dev)` 并把 workflow id + 当前 DSL 注入为首条上下文）。详情页「开发」tab 的「打开开发会话」按钮 = `newSession('workflow-dev', {seed})` + 跳 `/`（复用现有 `newSession(agent_id)` + composer `target` 原语）。
- **修改 = 工具调用 + 独立 interrupt 确认（已决 Q5）**：dev agent 调 `propose_edit(workflow_id, new_dsl, rationale)` → 后端算 `diff(current, new_dsl)` + 校验 new_dsl → 发 WS `version.propose { workflow_id, diff, rationale, propose_id }` 并 `interrupt`。
  - **确认卡是独立的一等 interrupt kind（`version.propose`），不依赖 permission 子系统**：UI 复用 permission 卡片的交互形态（左右/unified diff + Apply/Reject），但走自己的 interrupt/resume 通道。原因：permission 计划在全局移除（默认 bypass、后续彻底去限制，见 Q9），diff 确认必须与之解耦，免得随 permission 一起被删。
  - 用户 Apply → `Command(resume={apply:true})` → 写新版本 + 发 `workflows.changed`；Reject → resume(apply:false)，dev agent 收到拒绝可继续对话调整。
- 这样「所有修改前后对比确认 + 版本化」由独立 interrupt + diff 卡天然保证，且不需要任何 DAG 拖拽编辑器，也不耦合即将废弃的 permission。

---

## 9. Supervisor 模式（占位，待深入）

DSL 的 `supervisor` 字段编译为图里的一个**监控节点**，挂在每个 step 之后（或指定检查点）：

- **路由**：step → `supervisor` →（继续下一步 | 重试本步 | 跳过 | 改 context 后继续 | 暂停 | 中止）。
- **`mode: auto`**：supervisor 节点是一次**独立 LLM 调用**（可指定 agent/model），输入 = 当前 context + 最近 events 摘要 + 可选策略文本，输出 = 上述控制指令（structured output）。适合「长流程自愈」。
- **`mode: human`**：supervisor 节点 = `interrupt`，把控制权 + 当前状态推给 Supervisor UI，人决策后 resume。适合「关键流程人盯」。
- **可调**：运行中可经 `resume` 改 supervisor 策略/阈值（context 的一部分或 run 级 config）。
- **本期交付**：schema 字段 + 图节点桩 + UI「Supervisor」tab 占位（显示当前模式 + 人工决策入口）；auto 的策略 prompt 与循环/重试上限在深入讨论后定。

---

## 10. 前端信息架构

新增**工作流页** `app/workflows/page.tsx`（静态导出，仿 KB 单路由 + 客户端选择态），两栏 + sticky inspector：

| Tab / 区 | 内容 | 复用 |
|---|---|---|
| 列表 | workflow 卡（name/版本/最近 run）+「最近运行」列表 | 现有 store/卡片样式 |
| 详情·图 | DSL 的 **DAG 视图**，节点按执行状态着色，运行中实时高亮当前节点；点节点 → inspector 显示该步 goal/context 快照/日志 | 改造 `kb/GraphView.tsx`：换入参为 `{nodes,edges}` + 拓扑分层 seed（保留其 SVG 渲染/拖拽/高亮） |
| 详情·上下文 | `context` 表单（按 schema）+ 原始 JSON；运行前可编辑 initial，暂停时可 patch | 仿 `PageViewer` edit 模式 |
| 详情·日志 | `events.jsonl` 时间线，可按节点/类型过滤、搜索、跟随实时流 | 新建 `WorkflowLogView` |
| 详情·版本 | 版本列表 + 选两版 **diff** + 回滚 | 新建 `DslDiffView`（行级 diff 高亮） |
| 详情·开发 | 「打开开发会话」按钮 → 绑定 `workflow-dev` 的专属 session | `newSession(agent_id)` + `target` |
| 详情·Supervisor | 当前模式 + 人工决策入口（mode=human 时） | 占位 |

- **右栏 `WorkflowPanel`** 增强：run 卡可点跳详情、显示取消/重试、进度；def 名可点跳详情。
- **chat 内 `WorkflowBlock`**：加「查看详情」跳转；diff 确认卡（`version.propose`）作为新 block/交互渲染在 chat 或详情顶部。
- **Settings → Workflows**：降级为「高级 / 原始 JSON」入口或直接跳转工作流页（不再作为主编辑面）。

---

## 11. 数据流（执行 + 编辑两条主线）

**执行**：
```
UI 重跑 ─POST /workflow_runs{wf, version, context_override}─► server
   ├─ fork_agent(src) → 独立 thread
   ├─ compiler.compile(dsl_version) → StateGraph
   ├─ graph.astream(...) ──► 事件 ──► events.jsonl + SSE/WS(run.event)
   │     step 节点: model+tools(复用) → 写 context + context_meta
   │     branch/loop/human/supervisor 见 §3/§9
   └─ 完成/中断 → run.status + WS(run.status) → UI 详情/右栏刷新
```

**对话式编辑**：
```
详情页「打开开发会话」─newSession(workflow-dev, seed=当前DSL)─► /
   用户对话 ─► dev agent ─propose_edit(new_dsl)─► server
       ├─ diff(current,new) + 校验 ─► WS(version.propose{diff,rationale}) + interrupt
       ├─ 前端 diff 卡 [Apply/Reject]
       └─ resume(apply) ─► 写 versions/N+1 + current++ + WS(workflows.changed)
```

---

## 12. 与现有模块的衔接

| 模块 | 改动 |
|---|---|
| `workflows/store.py` | 拆为版本化布局；写改原子（lift checkpointer 的 temp+rename）；新增版本/diff/rollback；非原子写 + 无锁的并发缺陷一并修 |
| 新增 `workflows/{dsl,compiler,engine,events,context}.py` | schema+校验 / DSL→图 / 执行器+stream / events.jsonl / context 校验 |
| `graph.py` | 抽出 `agent_node`/tool 循环为可被 step 节点复用的工厂（不改主 chat 图行为） |
| `agents/registry.py` | 加 `fork_agent(src_id, new_id, name)`；seed `workflow-dev` agent |
| `server.py` | 新增端点（versions/diff/rollback、runs 触发/cancel/resume、events stream、context get/patch、summarize-from-session、propose_edit 的 interrupt/resume）；新增 WS 事件 `run.event/run.status/version.propose/context.changed` |
| `checkpointer.py` | 实装 `list()`（用于 events/上下文 time-travel 与 from_node 重跑） |
| 前端 | 新增 `app/workflows/page.tsx` + `components/workflow/{WorkflowDag,DslDiffView,ContextEditor,WorkflowLogView}.tsx`；改造 `GraphView` 为通用 DAG；`WorkflowBlock`/`WorkflowPanel` 加跳转与操作；`runtime.ts` 补 `updateWorkflow`/versions/runs 单条/events stream/summarize/propose 响应 |
| 静态导出 | 走 KB 模式（单路由 + 客户端态），**不**用 `/workflows/[id]` 动态路由 |

---

## 13. 分阶段落地

- **P1 地基**：DSL schema+校验+版本存储+events 骨架+原子写；`update`/versions/diff/rollback 端点；前端原始 DSL 查看+版本列表+diff。**不接执行**，先让 DSL 可沉淀可版本化。
- **P2 执行+日志**：compiler（step+branch）+ engine 单 thread + fork agent + events.jsonl + SSE；`POST /workflow_runs` 触发；详情·图(静态着色)+日志时间线。
- **P3 上下文+loop**：context schema 表单+initial 覆盖+`context_meta`；loop（含 Send 并行）；详情·上下文 tab。
- **P4 详情页成体**：GraphView→DAG 拓扑布局+运行实时高亮+点节点过滤日志；右栏/`WorkflowBlock` 跳转与 cancel/retry。
- **P5 对话式编辑**：seed `workflow-dev` + 专属 session + `propose_edit` interrupt + diff 确认卡 + 自动建版本。
- **P6 总结**：`summarize-from-session` → DSL 草稿 → 走 P5 的 diff 确认落 version 1。
- **P7 Supervisor + human + subflow**：占位转实装（auto 策略 prompt、human interrupt UI、subflow 子图）。

---

## 14. 开放问题（待你拍板）

- **Q1 DSL v1 表达力**：✅ 已决——`step+branch+loop+human` 已够；`subflow`/并行延后 v2。
- **Q2 step 隔离粒度**：✅ 已决——单 thread 全图。
- **Q3 表达式沙箱**：Jinja2(模板) + 受限 AST 求值(条件) 是否可接受？还是引 CEL/简单 DSL？
- **Q4 版本布局**：`workflows/<id>/versions/*.json`（全量快照，简、diff 易）vs 单文件+eventsourcing？倾向全量快照。
- **Q5 diff 确认载体**：✅ 已决——独立 interrupt kind `version.propose`（UI 沿用确认卡形态，但不依赖 permission，见 Q9）。
- **Q6 开发 session 绑定**：每 workflow 一个常驻 dev session vs 每次新开？倾向常驻一个 + 可「清空重开」。
- **Q7 Supervisor**：⏸ 待深入讨论——本期仅占位接口（schema 字段 + 图节点桩 + UI tab）。
- **Q8 并发**：run 级锁 + store 原子写是否足够？多 run 同 workflow 是否允许并行（fork agent id 已含 run_id，天然不冲突）？
- **Q9 permission 子系统去留**：现状默认 `bypass=True`，方向是后续在全局彻底移除 permission 限制。需定：移除时间表，以及移除后 `permission_node`/`PreToolUse` hooks/`interrupt(permission_request)` 的归宿。**约束**：workflow 的 diff 确认（Q5）与 human 节点必须用独立 interrupt kind，不得依赖 permission，确保解耦后可独立存活。

---

## 附：与 5 个目标的对应

| 目标 | 落点 |
|---|---|
| 1 session→DSL + 重跑 + Supervisor | §4 总结、§5 执行、§9 Supervisor |
| 2 fork agent + 干净历史 + 可携带/可编辑上下文 | §5.2 fork、§6 context |
| 3 详细执行日志 | §7 events.jsonl + SSE/WS |
| 4 详情页（图+上下文+日志+清单） | §10 |
| 5 每 workflow 开发 session + Workflow-dev Agent + 版本化 diff 确认 | §8 |
