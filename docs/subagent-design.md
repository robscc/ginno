# Ginno Subagent 系统设计

> 状态：产品方案（待评审）
> 日期：2026-09-30
> 前置调研：Ginno 现有架构（stream/graph/steering/checkpointer）；Claude Code subagent 官方文档（sub-agents / tools-reference / workflows）

## 0. 一句话

**Subagent = 子 session。** 主对话把任务委派给一个拥有独立上下文、独立对话流、独立生命周期的子 agent；用户在侧栏和主对话卡片里全程可见、可介入（steering）、可验收；结果以摘要回传主对话、以完整对话落盘备查。

## 1. 需求

1. Subagent 有明确的目标（goal）与约束；对话像 chat 页一样完整可见。
2. 主对话知道有多少 subagent 在跑；subagent 结束后做检查、确认、总结；主对话执行中结果走 steering 通道注入。
3. 两种发起方式：`/subagent` 用户主动发起；主 agent 自主判断拆分发起（可多个）。
4. 任务拆分由提示词模板或大模型指定。

## 2. 核心决策：subagent 是子 session

现有 session 机制已具备 subagent 需要的一切，复用率极高：

| 需求 | 复用的现有机制 |
|---|---|
| 独立完整对话流 | 独立 checkpoint 文件（`sessions/<id>.json`）、独立 WS 端点、`getSessionHistory` |
| 完整对话查看 | ChatStream 按 session id 打开，零新渲染路径（PinStream 已证明协议可 100% 复用） |
| 并发 | 每个 session 有自己的 `_turn_lock` / `_TURN_TASKS`，天然绕开单会话单 turn 串行 |
| 中途介入 | steering 原语 per-session（`steer_enqueue` / 超步边界 drain） |
| checkpoint 隔离 | per-session 单文件，无需触碰「新铸 message id」纪律 |

**新增的只有**：父子关系元数据、spawn 工具、subagent 事件命名空间、侧栏树渲染、结果回传调度、级联删除。

## 3. 从 Claude Code 借鉴什么

| Claude Code 模式 | Ginno 采用方式 |
|---|---|
| 后台 subagent + 完成通知（结果在后续 turn 到达） | 结果回传 = steering 注入（父 running 时 `steer_enqueue`）或 idle 时开新 turn —— 与 steering 设计 §3.3 完全同构 |
| 双通道回传：摘要进主对话、全量 transcript 落盘 | 摘要卡片进主对话；完整对话就是子 session checkpoint，天然落盘 |
| 防泛滥三件套：硬上限 + 软引导 + 用户 opt-in | 并发上限（默认 5，硬限）+ 深度上限（3 层，硬限）+ 多 agent 批量拆分需用户确认 |
| 权限「继承 + 单向收紧」 | subagent 继承父 session 的 permission 策略，只可收紧 |
| 失败三分法：用户杀 / 主 agent 杀 / 自己失败 | 用户杀不可 resume；其余结束态都允许「继续这个 subagent」（开新 turn） |
| fork（继承全上下文）vs fresh（全新上下文） | V1 只做 fresh（standalone brief，同 delegate_agent 语义）；fork 列入 V2 |
| description 驱动自动委派 | subagent 类型注册表（见 §5.2），description 是主 agent 路由依据 |

**刻意不抄的**：Claude Code 终端形态下用户基本看不到 subagent 全文（只有 panel + `/tasks`）；Ginno 是桌面应用，子 session 树 + 完整对话视图是一等公民——这是差异化优势。

## 4. 数据模型

### 4.1 SessionMeta 扩展（`types.ts` + runtime session meta）

```ts
interface SessionMeta {
  // ...现有字段
  type?: "quick" | "subagent";          // 已有 "quick" 先例
  parent_session_id?: string;           // 父 session（主对话或上层 subagent）
  depth?: number;                       // 0 = 主对话直接子代；上限 2（共 3 层）
  subagent?: {
    goal: string;                       // 明确目标（必填）
    constraints?: string;               // 约束（只读、不改 X 文件、时间预算…）
    acceptance?: string;                // 验收标准（可选，供主 agent 检查用）
    origin: "user" | "agent";           // /subagent 发起 or 主 agent 自主发起
    status: "running" | "waiting" | "done" | "failed" | "stopped";
    // waiting = 自己的 turn 已结束，但还有运行中的子代（见 §5.6 等待机制）
    result_summary?: string;            // 完成后回填的摘要
  };
}
```

### 4.2 深度语义

主对话 depth = -1（或无 subagent 字段）。`depth = 0,1,2` 为三层 subagent；depth = 2 的 session 内不注册 spawn 工具（结构性封顶，而非运行时报错）。系统提示告知 subagent 当前剩余深度，让它自己决定不下放。

## 5. 生命周期

### 5.1 发起（两种入口）

**入口 A：`/subagent`（用户主动）**

```
/subagent 调研三个 OAuth 库的 token 刷新处理，给出选型建议
```

- 走现有 `commands/resolver.py` 管线注册为内置命令。
- 两种形态：
  - **直接型**：`/subagent <goal>` → 立即创建 1 个 subagent（约束/验收留空，可在子 session 里用 steering 补）。
  - **拆分型**：`/subagent 拆分 <复杂任务描述>`（或 goal 中含「拆分/并行/多个」意图时）→ 先调 LLM 做任务分解（见 §5.2）→ 展示拆分方案卡片（每个子任务一行：goal / 约束 / 建议并发）→ 用户确认或编辑 → 批量 spawn。

**入口 B：主 agent 自主 spawn（工具）**

新增工具 `spawn_subagent(goal, constraints?, acceptance?)`（`tools/builtin.py` 注册，随 `build_all_tools` 拼装）：

- 工具描述里写清使用判据（何时该拆、何时不该），模仿 Claude Code 的 description 驱动路由；
- 单 turn 内多次调用 = 并行多个 subagent（同一消息多个 tool_use 的既有并发语义——langgraph
  并发派发，见 architecture §17.15；注意并行 subagent 若与父 agent 改**同一文件**，进程级写锁只保证
  不互相踩踏，不保证语义上的合并，需在 brief 里靠"分文件"约束规避）；
- **不进 permission 豁免集**（同 delegate_agent 的先例）：默认 `ask`，用户看到 goal/约束后批准——这是防泛滥的第一道闸。用户批准时可勾选「本会话不再询问」；
- 返回值是即时回执（subagent_session_id + 标题），**不阻塞主对话**——后台语义，结果靠完成通知（§5.5）。

配套只读工具 `list_subagents()`：返回在跑/已结束的 subagent 清单（id、goal、状态、耗时）——主对话感知「有多少在跑」的模型侧通道。

### 5.2 任务拆分（提示词 or 模型）

两条路径，同一产物（`SubtaskPlan[]`）：

1. **提示词模板**：预置拆分模板（调研型 / 实现型 / 评审型，见附录 A.2 / A.3），`/subagent 拆分` 且匹配模板时直接套用，不额外调 LLM；
2. **模型拆分**：不匹配模板时用当前会话模型跑一次 decompose 调用（复用 `/compact` 的 async_handler 模式：builtin_async 命令 + busy-gated 后台任务），输入 = 用户任务 + 当前对话上下文摘要，输出 = 结构化子任务列表（JSON schema 约束）。

主 agent 自主拆分不需要这条管线——它自己就在做拆分决策，直接多次调 `spawn_subagent`。

### 5.3 运行与并发控制

- subagent 的 turn 执行**完全复用** `_run_stream` / graph：独立 session、独立 `_turn_lock`，与主对话并行。
- spawn 时由 runtime 侧组装 standalone brief（首条 user 消息）：
  ```
  <ginno_subagent_brief>
  目标：{goal}
  约束：{constraints}
  验收标准：{acceptance}
  工作目录与上下文目录：继承父 session
  你是第 {depth+1} 层 subagent，{还可以/不可以}再委派子任务。
  完成后输出最终报告：结论 + 关键产出物路径 + 与验收标准的对照（证据化格式见附录 A.4，输出纪律见附录 A.6）。
  </ginno_subagent_brief>
  ```
- persona 默认继承父 session 的 agent_id；subagent 类型注册表（V2）可指定不同 persona/工具白名单。
- **并发硬上限 5**（`settings` 可调）：超限时 spawn 工具返回 `[error] 并发 subagent 已达上限，勿立即重试`，并附当前在跑清单（模仿 Claude Code 的明确不重试提示）。

### 5.4 人类介入（steering 模式）

用户在子 session 视图里正常输入：

- 子 agent **running**：走 `steer` 帧 → 子 session 的 stash → 下个超步边界吸收。与主对话 steering 行为逐字节相同。
- 子 agent **idle（已结束）**：退化为普通 `invoke` 开新 turn——「继续这个 subagent」即 resume 语义（上下文全在 checkpoint 里）。用户杀掉的（侧栏 × 或 stop）不提供该入口；主对话 stop 后的同样规则。

**父 agent 对子 session 的注入（agent-to-agent steering）V1 不做**——介入是人类特权，避免 agent 间消息绕过权限模型（Claude Code 硬规则：agent 消息不构成任何批准）。

### 5.5 结束、结果回传与验收

subagent 的 turn 自然结束后（`message.end` 且无后续 pending），runtime 侧 subagent 调度器：

1. **生成摘要**：取最终报告（最后一条 assistant 消息），若过长（> 约 500 字）用当前模型压一版摘要（复用 session_title 的生成链路思路）；
2. **回填 meta**：`result_summary` + `status` 写入子 session meta，发 `agents.changed` 类事件刷新 UI；
3. **注入主对话**（与 steering 设计 §3.3 同构）：
   - 父 session **running** → `steer_enqueue`，消息形如 `<ginno_subagent_result session="…" goal="…">摘要</ginno_subagent_result>`，主 agent 下个超步边界吸收并转述/整合；
   - 父 session **idle** → 直接开新 turn（invoke 一条 system 侧 user 消息），主 agent 收到后向用户汇报；
4. **主对话卡片更新**：运行中卡片 → 结果卡片（摘要 + 「查看完整对话」+ 验收操作）。

**检查 / 确认 / 总结**（需求 2）：

- **自动检查**：subagent 带 `acceptance` 时，主 agent 收到结果后在同一 turn 内对照验收标准给出一行判定（通过 / 有缺口 + 缺口说明）——不需要新机制，就是注入消息里要求它做；
- **人工确认**：结果卡片上「✅ 确认 / ↩ 有问题」。确认 = 卡片定稿归档；有问题 = 用户可选择直接在子 session 里 steering 纠偏（继续跑），或在主对话里让主 agent 重新 spawn；
- **总结**：多个 subagent 都完成后，主 agent 做一次汇总（注入的最后一条结果消息触发，或用户点卡片上「汇总」）。

### 5.6 嵌套等待机制（subagent 有自己的 subagent 时）

**问题**：父 subagent spawn 了子代后，若自己的 turn 先结束，调度器不能把它判定为「完成」——它的最终报告必须包含子代结果，提前汇报会产生空洞的结论向上传染。

**两层机制**：

1. **结构性完成门（调度器侧，硬保证）**：subagent 的完成判定 = **自己 turn 结束 ∧ 无运行中子代**。turn 结束但子代仍在跑 → status 转为 `waiting`（侧栏 ⏳），不触发摘要/上报；最后一个子代完成时，其结果照 §5.5 注入父 subagent（此时它 idle → 开新 turn），父 subagent 整合子代结果后再次到达 turn 边界——此时无运行中子代，才真正 `done` 并向上汇报。**该门对任意深度都成立**：中间层只要还有活的后代就停在 `waiting`。
2. **显式等待工具（模型侧，主动汇聚）**：`list_subagents(wait=true, ids?, timeout_s?)`——阻塞当前 turn 直到指定的（默认全部）直属子代结束，子代摘要作为工具结果一次性返回。这是主 agent/subagent 收敛结果的惯用姿势：spawn 若干 → `list_subagents(wait=true)` → 对照验收写最终报告。带 `timeout_s` 到点返回各子代当前状态（partial 语义，模仿 Claude Code 的 partial 标注）；用户 stop 可以打断等待（协作式，复用 `_TURN_STOP`）。（2026-10-08 前该能力是独立工具 `wait_subagents`，为省每请求 schema 开销并入 `list_subagents`，语义不变。）

**配套规则**：

- **stop 级联**：停止一个 `running`/`waiting` 的 subagent 时，其全部运行中后代一并协作式停止（与级联删除同一套遍历）；被级联停止的子代 status = "stopped"，不向已停止的父注入结果；
- **完成上报只发一次**：`waiting → running`（被注入唤醒）→ `done` 的链路里，摘要生成与向上注入仅发生在最终 `done` 时刻；
- **防饿死**：`waiting` 期间若所有子代都被用户杀掉（无人再注入唤醒），调度器注入一条「你的子任务均已被用户停止」消息唤醒父 subagent 收尾（它自行决定汇报什么）；
- 死锁不可能：父子关系是树（无环），深度有硬上限。

### 5.7 失败 / 停止 / 超时

- **瞬态 API 错误**：复用现有 `AUTO_RETRY_MAX=2` 自动重试；重试耗尽 → `status: "failed"`，失败通知注入主对话（错误原文不混入正常结果，模仿 Claude Code 的失败显式上报）；
- **用户 stop**（子 session 视图里的停止按钮 / 侧栏 ×）：复用 `_TURN_STOP` 协作式停止 + `_heal_interrupted_turn`；status = "stopped"，**不注入主对话**（用户亲手杀的，主 agent 只在下次调 `list_subagents` 时看到）；侧栏可清行；
- **无超时击杀**（与 delegate_agent 不同）：subagent 是进程内会话不是子进程，用户全程可见可停，不需要隐藏的超时语义。

### 5.8 清理（级联删除）

- 删除父 session 时，沿 `parent_session_id` 反向索引找到全部后代：
  - 运行中的先协作式停止、等 turn task 收尾，再删 checkpoint 文件（避免删后回写）；
  - 确认对话框明示「将同时删除 N 个子 agent 会话」；
- 单独删除一个子 session 不影响父（父卡片标记「已删除」，结果摘要仍留在主对话历史里）。

### 5.9 标题

subagent 首个 turn 结束后，用当前模型对 goal + 首轮产物生成动词短语短标题（复用 `session_title` 事件链路），发 `session_title` 刷新侧栏。

## 6. UI 设计

### 6.1 侧栏 session 树（AppShell）

```
今天
├─ 重构认证模块                    ● 2 agents
│   ├─ 🟢 调研 OAuth 库选型
│   ├─ 🟢 编写集成测试骨架
│   └─ ✅ 梳理现有 session 逻辑     （已总结回主对话）
├─ 日报整理
```

- `sessionGroups` 渲染前先按 `parent_session_id` 建树：有子的主 session 行展示徽标（运行数）；子行缩进、状态点（🟢 running / ⏳ waiting / ✅ done / ⚠ failed / ⛔ stopped）+ 标题；
- 子树默认展开运行中的、折叠已完成的（完成行淡化，可点开）；点击子行 = 切换到该子 session 的完整对话视图；
- 子行 hover 出 × （停止单个 / 清除已完成行）；
- 嵌套子 subagent 再缩进一级，行尾标 `+N` 表示还有后代。

### 6.2 子 session 视图

- **就是 ChatStream**，按 session id 打开，套一个 subagent 顶栏：goal / 约束 / 验收标准 / 状态 / 「返回主对话」；
- 输入框语义：running 时输入 = steering（复用 `useSteerQueue`，UI 上标「将在下个工具边界注入」）；idle 时 = 继续 subagent；
- permission / ask_user 卡片正常出现在子 session 视图内，用户直接代答（多 socket 广播已支持）。

### 6.3 主对话卡片（blocks.tsx 新增 block 类型）

- **发起卡片**：goal / 约束 / 拆分理由（主 agent 自主发起时）/ 「查看对话」入口；等待 permission 批准时就是 permission 卡的上下文；
- **运行中**：脉搏指示 + 已运行时长 + 实时 token 计数（usage 事件已有）；
- **结果卡片**：摘要 + 状态 + 验收判定 + 「查看完整对话 / 确认 / 有问题」操作。

## 7. Runtime 实现要点（文件级）

| 改动 | 位置 |
|---|---|
| `spawn_subagent` / `list_subagents`（含 `wait=true` 阻塞语义，原 `wait_subagents` 已并入）工具 | `tools/builtin.py`（或新 `tools/subagent.py`），`graph.build_all_tools` 拼装，depth≥2 时不注册 spawn；完成门（turn 结束 ∧ 无运行中子代）与 waiting 状态机在调度器 |
| subagent 调度器（完成监听 → 摘要 → 注入父对话） | `api/stream.py` 旁路任务，模式参照 `_WF_RUN_TASKS` / `spawn_bg`；注入复用 `steer_enqueue`（running）与 invoke（idle） |
| `/subagent` 命令（含拆分型 builtin_async） | `commands/registry.py` + `resolver.py` |
| SessionMeta 扩展 + 反向索引 | `session_meta.py` / `server_shared.py`（`_SESSIONS` 内存表加 `children` 索引） |
| WS 事件 | 新增 `subagent.spawned` / `subagent.status`；结果注入复用 `steer.accepted` / `message.end` 既有事件，不新造 |
| 级联删除 | sessions 删除 API 处（先停运行中 turn → 删 checkpoint） |
| 侧栏树 / 卡片 / 子 session 顶栏 | `AppShell.tsx`（sessionGroups 建树）、`blocks.tsx`（subagent block）、`store.tsx` |

## 8. 权限与安全

- **继承 + 单向收紧**：subagent 继承父 session 的 permission 策略与 tools_allow；可再收紧（类型注册表指定更小工具集），不可放宽；
- `spawn_subagent` 不进豁免集，默认 `ask`（用户批准的是 goal 本身）；
- subagent 工具调用照常走 `permission_node`；ask_user / permission 卡片出现在子 session 视图，由用户代答；
- agent 间与 agent 内消息一律不构成权限批准（硬规则，写进系统提示）；
- subagent 产出的文件改动照常触发 `code.changed` / `preview.invalidate`，主窗代码面板无感兼容。

## 9. 明确不做（V1）

- fork（继承父全上下文的并行分支）——V2，需解决 prompt cache 共享与上下文拷贝成本；
- agent-to-agent steering（父 agent 直接注入子 session）；
- subagent 类型注册表（.md 定义多 persona/工具白名单）——先用父 persona + 默认工具集；
- Workflow 式脚本编排（几十上百 agent）——现有 workflow 引擎已覆盖该场景，subagent 面向「少量、可见、人机协同」。

## 10. 分期

- **P1（核心闭环）**：SessionMeta 扩展 + spawn 工具 + 完成注入回传 + 侧栏树 + 子 session 视图（顶栏简化版）+ 结果卡片。跑通「主 agent 拆 2 个并行任务 → 完成回传 → 汇总」。
- **P2（发起与验收完整化）**：`/subagent` 命令两形态 + LLM 拆分 + 验收判定 + 确认/纠偏操作 + 级联删除 + LLM 标题。
- **P3（进阶）**：subagent 类型注册表、fork、并发上限设置项、token 预算警告。

## 11. 开放问题（评审时定）

1. 摘要生成的成本控制：每个 subagent 结束都多一次 LLM 调用，是否只在最终报告 > 阈值时触发（当前方案：是）？
2. 主对话 idle 时结果注入开新 turn，会话列表 `updated` 被子 session 反复顶起——是否需要「子 session 活动不顶起父会话排序」的豁免？
3. 多个 subagent 同时完成时的注入合并：连续多条 steer 消息 vs 合并一条（当前方案：不合并，逐条注入，模型侧整合）。

## 附录 A：Prompt 借鉴库（源自 langgptai/awesome-claude-prompts）

以下 prompt 从该仓库筛选，均与 subagent 机制直接相关。每条给出：来源 → 原文精髓 → **Ginno 适配版**（可直接进系统提示/工具描述/brief 模板）→ 刻意修改点。

### A.1 dispatch_agent 工具描述（Claude Code 系统 prompt 节选）

来源：README「system prompt and tools from claude code」。

原文精髓——五条 usage notes，其中三条直接可用：

> 1. Launch multiple agents concurrently whenever possible… use a single message with multiple tool uses
> 2. The result returned by the agent is not visible to the user… you should send a text message back to the user with a concise summary
> 3. Each agent invocation is stateless… your prompt should contain a highly detailed task description… and specify exactly what information the agent should return

**Ginno 适配**（`spawn_subagent` 工具描述的骨架）：

```
spawn_subagent(goal, constraints?, acceptance?)：创建一个拥有独立上下文和独立对话的子代理。

使用判据：
- 任务可独立描述清楚、产出可单独验收时才委派；需要当前对话细节的任务不要委派（子代理看不到本对话）
- 多个互不依赖的子任务，在同一条消息里并行发起多次调用
- goal 必须自足：子代理只能看到你写的 goal/constraints/acceptance，看不到此对话
- 在 goal 里明确要求最终报告返回什么（结论、产出物路径、与验收标准的对照）
- 结果以摘要注入本对话；完整对话用户随时可查，但你的转述是用户的第一信源——只转述关键信息

不委派的情况：
- 一步就能完成的查证（自己查更快）
- 需要修改当前正在编辑的文件（子代理并发写会冲突）
```

**刻意修改两点**：
- 原文「The agent's outputs should generally be trusted」**删掉**——Ginno 沿用 delegate_agent 的 UNTRUSTED 先例，subagent 报告（尤其涉及路径、代码引用）在注入消息中按不可信数据处理，主 agent 引用其产出前应自行核验关键事实；
- 原文「agent 不能用 Bash/Edit」的工具白名单思路保留在权限模型（§8 单向收紧），不写死在工具描述里。

### A.2 AutoGPT（任务分解步进执行）→ 「实现型」拆分模板

来源：README「AutoGPT」。

原文精髓：小问题直接答；大项目走「一次关键分析 → 项目结构 → 一次一小步 → 自动继续」。

**Ginno 适配**（§5.2 提示词模板之「实现型」，decompose 调用的系统提示）：

```
你是任务拆解器。输入一个工程任务，输出并行子任务方案。

流程：
1. 关键分析（只做一次）：用多级列表拆解任务的关键方面与风险
2. 子任务划分：每个子任务必须是——
   - 可独立完成、可单独验收（有明确的完成判据）
   - 相互之间文件/资源不冲突（并发执行安全），冲突的必须串成依赖说明
   - 粒度适中：单个子任务预期 5-30 分钟工作量；更小的合并、更大的再拆
3. 为每个子任务写：goal（自足的完整描述）、constraints（不可碰的边界）、acceptance（验收标准）

输出 JSON 数组，不要任何前言或解释。
```

**刻意修改**：原文「Perform all tasks directly and automatically without asking」反着改——拆分结果必须经用户确认卡片（§5.1 入口 A）才批量 spawn，这是防泛滥闸门。

### A.3 Get multiple perspectives / CEO panel（多视角评审）→ 「评审型」拆分模板

来源：README「Get multiple perspectives for your problem」「Get solutions from a CEO to your problems」。

原文精髓：给同一问题生成 N 个不同立场/身份的独立评估再对比。

**Ginno 适配**（§5.2「评审型」模板——这是 subagent 的天然场景：N 个独立上下文互不污染）：

```
对 {决策/方案} 做多视角评审。spawn {N} 个评审 subagent，各自立场：

- 立场₁（如：架构保守派）——关注长期维护成本、迁移风险
- 立场₂（如：激进效率派）——关注交付速度、机会成本
- 立场₃（如：用户代言人）——关注实际使用体验、边界场景

每个 subagent 的 goal 中写明：只从本立场出发论证，不要折中；
最终各自给出：结论 + 论据 + 本立场下的最大风险。
汇总时由主对话对比，不要求 subagent 之间达成一致。
```

要点：**刻意不折中**——每个独立上下文保持立场纯粹，折中留给主 agent 汇总时做。这比单对话里「扮演多个角色」的质量高（无交叉污染），是 subagent 架构的独有优势。

### A.4 Cite your sources（引用-后答）→ subagent 最终报告格式

来源：README「Cite your sources」。

原文精髓：先列原文引用编号，答案句尾挂引用号，无法回答就明说。

**Ginno 适配**（写进 standalone brief 的最终报告要求，§5.3）：

```
完成后输出最终报告，格式：
证据：先列出支撑结论的依据，编号排列——文件路径:行号、命令输出摘录、
      测试结果。没有可靠依据的条目写「无依据，系推断」。
结论：逐条陈述，句尾以 [n] 标注所依据的证据编号。
验收对照：逐条列出 acceptance 标准与实际达成情况（达成/部分/未达成 + 说明）。
若目标无法完成：明确说明卡在哪一步、已尝试什么、还需要什么输入。
```

价值：subagent 报告是向上传染的唯一通道，证据化格式让主 agent 的验收判定（§5.5）有据可依，也大幅降低幻觉随摘要进入主对话。

### A.5 MetaPrompt 官方示例（写指令的元任务）→ brief 写作规范

来源：README「MetaPrompt (official example)」。

原文精髓：给「热心但缺乏背景的助手」写指令——先立规则边界，再 BEGIN DIALOGUE 进入角色。

**Ginno 适配**：主 agent 写 goal 就是在做 MetaPrompt 的事。规范写进系统提示：

```
给 subagent 写 goal 时，把它当作一个热心但完全不了解上下文的新同事：
- 先说清要做什么、为什么（一句话背景即可）
- 再列边界规则（什么不要做、什么不要改）
- 最后明确交付物：报告里必须包含什么
- 不要复述大段本对话内容——需要背景说明该任务不适合委派
```

### A.6 Custom AI Operating System（operator 输出纪律）→ brief 的输出纪律段

来源：README「Custom AI Operating System for a Small Business」。

原文精髓：profile 静默使用不回问；输出「立即可用」；缺关键信息只问一个问题然后继续；无客套前言。

**Ginno 适配**（standalone brief 末尾的通用纪律段，所有 subagent 共用）：

```
输出纪律：
- workspace 与上下文目录已继承自主对话，直接使用，不要重新询问
- 产出必须立即可用：代码可直接落地、结论可直接引用，不留「待补充」占位
- 缺一个关键信息时：先完成其余部分，最后集中提出（最多 2 个）具体问题，
  不要中途停下来问
- 报告直入主题，不写「我将…」式的开场白
```

**刻意修改**：原文「ask ONE concise clarifying question, then proceed」放宽为「最多 2 个、收尾时集中问」——subagent 中途挂起等人回答会拖垮并行批次（ Ginno 没有 agent-to-agent 问答通道，问题只能经最终报告回流）。

### A.7 Assign tasks to the right skilled employee（能力-任务匹配）

来源：README「Assign tasks to the right skilled employee」。

原文精髓：按成员技能分派并**说明理由**。

**Ginno 适配**：拆分方案卡片（§5.1）中每个子任务加一行「委派理由」——主 agent 自主 spawn 时同样在发起卡片上展示。用于用户快速判断拆分是否合理（这是确认环节的主要审阅物）：

```
每个子任务卡片：goal 摘要 / 约束 / 验收 / 委派理由
委派理由示例：「纯只读调研，独立上下文即可完成，无需主对话细节」
反面示例（应阻止）：「需要知道用户之前说过什么」→ 说明不该委派
```

### A.8 Control output format（JSON mode）→ decompose 输出控制

来源：README「Control output format (JSON mode)」（引 Anthropic 文档）。

原文精髓：prefill 开括号 + 「Do not output preamble or explanations」。

**Ginno 适配**：§5.2 的 LLM 拆分调用直接采用——assistant 侧 prefill `[`，系统提示末尾加「输出 JSON 数组，不要任何前言或解释」。配合 A.2 模板使用。

### 汇总：prompt 与机制的对应

| Prompt | 落点 |
|---|---|
| A.1 dispatch_agent | `spawn_subagent` 工具描述 + 主 agent 系统提示的委派判据 |
| A.2 AutoGPT | 「实现型」拆分模板（decompose 系统提示） |
| A.3 多视角/CEO panel | 「评审型」拆分模板 |
| A.4 Cite your sources | standalone brief 的最终报告格式要求 |
| A.5 MetaPrompt | 主 agent 写 goal 的规范（系统提示） |
| A.6 operator 纪律 | standalone brief 的通用输出纪律段 |
| A.7 能力匹配 | 拆分方案卡片 / 发起卡片的「委派理由」字段 |
| A.8 JSON mode | decompose 调用的输出控制 |


1. 摘要生成的成本控制：每个 subagent 结束都多一次 LLM 调用，是否只在最终报告 > 阈值时触发（当前方案：是）？
2. 主对话 idle 时结果注入开新 turn，会话列表 `updated` 被子 session 反复顶起——是否需要「子 session 活动不顶起父会话排序」的豁免？
3. 多个 subagent 同时完成时的注入合并：连续多条 steer 消息 vs 合并一条（当前方案：不合并，逐条注入，模型侧整合）。
