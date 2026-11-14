# AgentTeam 编排能力产品提案

> 日期：2026-10-14
> 基线：main `fdedcd2`（AgentTeam P0–P3+ 已上线）
> 配套：`docs/agent-team-backlog.md`（56 项缺陷/打磨清单）——**本文是能力扩展，不是缺陷修复**
> 工作树：`.claude/worktrees/agent-team-next`

---

## 0. 核心判断：Ginno 不缺编排引擎，缺的是把引擎接到团队上

AgentTeam 今天的形态是「**群聊广场 + 常驻工人**」：通信层完备（@投递 / 回帖 / steer / 状态上卷 / 三层记忆），
但**工作结构层缺失**——没有任务依赖、没有结构化交接、没有流程闸门、没有自动触发。
用户仍然要人肉一句句 @，本质是「多开几个聊天窗口」，不是「编排」。

但调研代码后发现：**Ginno 已经有三台现成的编排引擎，只是都没接到团队上。**

| 既有引擎 | 已具备的编排原语 | 证据 | 与团队的关系 |
|---|---|---|---|
| **workflows DSL** | `parallel`（`max_concurrency` 1–8）、`branch`、`loop`、`human`（interrupt 闸门）、`python`（确定性节点）、**每节点可指定 `agent`** | `workflows/dsl.py:22,75-100,356-414` | 节点只能指向 agent，**不能指向团队成员实例** |
| **subagent plan** | `depends_on` 任务图、并发调度、失败重试（amend）、进度回报 | `create_subagent_plan` / `amend_plan` 工具 | 只服务一次性 subagent，**不接常驻成员** |
| **worker_bus** | 跨 session 派活 / 订阅 / 回执（assign_worker / watch_worker） | `worker_bus/registry.py:26` | `ASSIGNABLE_TYPES` **不含 team-member** |

**外加一个你自己的资产**：**SOPFlow**（已开源）——SOP 执行引擎，
`task/task_confirm/confirm/gate/join` 五内核 + 打回例程 + 子流程 + 事件溯源 + epoch CAS + RBAC，
已有 MCP server 与 strands 桥。**Ginno 里目前零集成**（全仓 grep `sopflow` 无命中）。

> **结论**：下面的建议绝大多数是 **S–M 的「接线」工作，不是 L 的「新建引擎」**。
> 这是 Ginno 相对竞品的结构性优势——herdr 要自己造状态机，Vibe Kanban 要自己造任务板，
> 而 Ginno 的引擎已经在了，只差插头。

**前置条件**：`agent-team-backlog.md` 的 **P0-2（Lead 工具绑错会话 → P3 自主分派整条空转）必须先修**。
否则下面所有「让 Lead/流程自动分派」的能力都建立在一个不可达的工具集上。

---

## 1. A 层｜任务层：从「发消息」到「派任务」

### A1. 团队任务板（复用 subagent plan 的依赖图）⭐ 推荐

**问题**：@投递是 fire-and-forget 消息，没有「B 等 A 完成」。用户要自己盯着回帖再手动 @ 下一个人。

**建议**：把 `create_subagent_plan` 的任务图能力接到团队成员上——
`depends_on` 依赖、并发上限、失败重试、进度自动回报，**引擎全部现成**，
只需把执行目标从「新建 subagent」换成「投递给常驻成员实例 + wait 收敛」。

**价值**：真正的依赖编排。`研究 → 实现 → 评审 → 文档` 这种链不再靠人肉接力。
**与既有 subagent plan 的边界**：subagent = 一次性临时工（fresh context）；
团队任务板 = 常驻同事（累积上下文 + 记忆）。二者共用调度器，不共用语义。

**量 M**（不是 L——调度器已有）｜依赖 P0-2、P1-2（状态钩子）

### A2. 结构化交接契约：把「反糊弄」从劝变成验 ⭐ 差异化

**问题**：内阁模板已经写了很好的回报纪律——
> 「报完成必须附路径、行号、命令输出与『未验证清单』，没跑说没跑，不得删弱校验凑绿；
> 遇阻回报『已做、卡哪、需要什么』三件；同一失败最多重试 2 次」
> （`api/team_templates.py:24-28` `_REPORT_CONTRACT`）

但它是**提示词文本**，靠模型自觉。而且按 backlog **P0-6，instructions 根本到不了成员**——契约双重失效。

**建议**：升级为 **schema 级交接契约**。成员回帖必须带结构：
```
{ status: done|blocked|failed,
  artifacts: [路径/行号],
  evidence: [命令 + 输出摘要],
  unverified: [没验证的清单],
  blockers: [已做/卡哪/需要什么] }
```
- 回帖卡按 schema 渲染：**声称 done 但 evidence 为空 → 自动标红「未验证」**
- `unverified` 非空时不允许标记任务完成（可被人工 override）
- Lead 汇总时按 schema 聚合，而不是读自由文本

**价值**：这是 Ginno 相对竞品的**真差异化**。Claude Code Agent Teams / herdr 的回帖都是自由文本，
「agent 谎报完成」是多 agent 编排的头号信任问题。把契约从 prompt 提升到 schema + UI 校验，
是**可验证的诚实**，不是「提示它诚实」。

**量 M**｜依赖 P0-6（instructions 落地）、P0-1（回帖持久化）

### A3. 团队共享产物板（blackboard）

**问题**：成员**有意不共享对话上下文**（隔离是对的），只共享 workspace + TEAM.md。
但编排需要「A 产出规格 → B 消费规格」，现在只能靠广场文本传递，长产物会被 1200 字截断。

**建议**：团队级 artifact 板——成员产出登记为 team artifact（文件/片段/结论），
下游成员用 `@artifact` 引用（**mention 机制已有**），产物面板复用既有 Artifacts 面板。
上下文仍隔离，但**产物可寻址**。

**价值**：解决「隔离」与「协作」的张力——不共享上下文，但共享工作成果。
**量 M**｜复用 artifacts + @artifact mention

---

## 2. B 层｜编排层：从「人肉 @」到「流程自动跑」

### B1. workflow 节点可指向团队成员 ⭐⭐ 最高性价比，建议第一个做

**现状**：DSL 已有 `parallel`（并发 1–8）/ `branch` / `loop` / `human` / `python`，
且**每节点已能指定 `agent`**（`dsl.py:381,414`）。节点执行走 headless turn。

**建议**：让节点的 `agent` 字段能解析为**团队成员实例**，执行改为 `deliver + wait` 收敛。
**只加一个 target 类型，引擎一行不用改。**

立刻获得的能力（全部是 DSL 已有的）：
- **流水线**：`step(研究) → step(实现) → step(评审)`
- **fan-out / fan-in**：`loop(parallel: {max_concurrency: 4})` 派给多个成员
- **条件分支**：`branch` 按上游产出选路
- **人工闸门**：`human` 节点 = interrupt，天然就是「人批准才继续」
- **确定性步骤**：`python` 节点做机械计算，不烧 token
- **循环重试**：`loop` + 退出条件

**价值**：一次性把「编排模式」全部拿到，且**用户已经会用 workflow 页**（零学习成本）。
**量 S–M**｜依赖 P0-2、P1-2｜这是我最推荐的第一项

### B2. 编排模式库（把常见协作形态做成团队模板）

**现状**：只有「Lead 分派」和「无领队广播」两种形态。团队模板机制已有（含内阁）。

**建议**：有了 B1，常见模式就是**写 DSL**，存为团队模板：
| 模式 | DSL 形态 | 适用 |
|---|---|---|
| 流水线 | `step → step → step` | 研究→实现→文档 |
| 评审环 | `loop(worker → reviewer → branch)` | 代码/文案质量把关 |
| 对抗验证 | `parallel(builder, red-team) → join` | 安全/ correctness |
| Map-Reduce | `loop(parallel) → step(汇总)` | 批量调研/多文件改造 |
| escalation | `branch(失败 → 升级给更强成员)` | 成本/能力分层 |

**价值**：用户不必自己设计协作结构，选模式即得。**量 S**（B1 之后就是写 DSL + 模板）

### B3. 团队级人在环闸门

**现状**：permission / ask_user 是**成员级**的（某个成员撞卡）。
编排需要**任务边界级**的「A 完成后人批准才启动 B」。

**建议**：短期用 B1 的 `human` 节点（已是 interrupt，零新机制）；
中期评估接 **SOPFlow 的 `gate`/`confirm` 内核**——它还有「打回例程」（评审不通过退回重做），
这是 workflow DSL 目前没有的。

**量 S（短期）/ M（SOPFlow）**

### B4. 事件与定时驱动：让团队自主运转

**现状**：团队只能被用户戳。`schedule_store.py:36` 的 `TARGET_TYPES=("prompt","workflow")` 不含 team；
渠道（钉钉/企微）消息走 headless 直跑，不经团队路由。

**建议**：
- `schedule target=team`：「每天 9 点团队站会 / 每晚巡检 CI」
- 渠道消息 → 团队：钉钉里 @ 团队即触发，回帖回渠道
- 依赖 B1 后，定时任务可以直接跑一个团队 workflow

**价值**：从「多开聊天窗口」变成「一支自主运转的团队」。这是产品形态的质变。
**量 M**｜backlog P2-1 / P2-3 已立项

---

## 3. C 层｜成员层：异构与智能

### C1. 外部 CLI agent 当团队成员（混合团队）

**现状**：`delegate_agent` 已能托管 Claude Code / Codex / pi / dsh，但 delegation 会话**不入团队编制**。

**建议**：让 delegation 会话成为团队成员 → **混合团队**（内部 agent + 外部 CLI agent），
统一状态/记忆/回帖/预算。

**价值**：这正是 herdr 的定位（它是 CLI agent 的 runtime），
但 **Ginno 有统一 UI + 状态语义 + 记忆分层 + 可回放**，herdr 只能读终端屏幕猜状态。
**量 M–L**｜backlog 已列为「成员异构化」议题

### C2. worker_bus 放开 team-member（agent→agent 的现成底座）

**现状**：`ASSIGNABLE_TYPES` 不含 team/team-member，`worker_bus/__init__.py` 明示「独立于 AgentTeam」。

**建议**：放开白名单 + 沿用工具层 ask 权限。
**这改变了设计文档的成本等式**：原本把 agent→agent 直连列为 V2「要新建邮箱、怕绕权限」，
但 worker_bus **已经提供了派活/订阅/回执管道**，不需要新建。

**价值**：成员之间能直接协作，不必一切经广场（广场仍是可观测主干）。
**量 M（原评估 L）**｜需守「投递≠批准」

### C3. 成员能力画像与自动选员

**现状**：@ 谁靠人记住谁擅长什么；Lead 分派也没有推荐依据。

**建议**：成员有 agent 配置 + 历史产出 + 成功率 → Lead 分派时给出**推荐 + 理由**
（「这个任务建议给 dev-后端：过去 5 次同类任务 4 次一次通过」）。

**价值**：降低编排的认知负担。**量 M**｜可后置，依赖 A2 的结构化产出做统计

---

## 4. D 层｜可观测与治理

### D1. 编排视图（任务流图，不是成员状态卡）

**现状**：总览页回答「谁在干什么」（成员维度）。
编排需要回答「**流程走到哪、哪一步卡住、它解锁了什么**」（任务维度）。

**建议**：任务流图 + 关键路径 + **阻塞传播**可视化
（A 卡住 → 高亮所有依赖 A 的下游任务）。
herdr 的洞见是「谁卡住了」；编排的下一步是「**它卡住了谁**」。

**量 M**｜依赖 A1

### D2. 预算与熔断

**现状**：无成本可见（backlog P2-5：member_row 无 usage 字段，usage_source 把 team-member 记为 `"chat"`）。

**建议**：团队/任务级 token 预算，超限**自动暂停并请示**（不是静默烧钱）。
多 agent 并行成本是单 agent 数倍——Claude Code 官方文档明确警告 agent teams "use significantly more tokens"。

**价值**：这是多 agent 编排的**刚需护栏**，不是锦上添花。**量 S–M**

### D3. 编排回放与审计（Ginno 独有优势）

**现状**：Ginno 的 session 全量可回放（FileCheckpointer），但**没有团队级视角**。

**建议**：团队级协作时间线——谁在何时产出什么、谁卡住多久、人何时介入、token 花在哪。
**终端形态的竞品（herdr / Claude Squad）给不了这个**，因为它们不拥有 agent 的内部事件流。

**价值**：可审计 = 可信任 = 敢把真实工作交给团队。**量 M**

---

## 5.  standout：AgentTeam × SOPFlow = 有治理的多 agent 编排

这是我认为**最有战略价值**的一条，且用的是你自己的资产。

| | AgentTeam | SOPFlow |
|---|---|---|
| 解决 | **谁干**（常驻成员、身份、记忆、状态） | **按什么顺序干、在哪卡闸、怎么打回** |
| 已有 | 广场 + 投递 + 回帖 + 状态机 | task/gate/join/confirm 五内核 + 打回例程 + 子流程 |
| 治理 | permission（成员级） | 事件溯源 + epoch CAS + RBAC + deptScope |
| 接入 | — | **已有 MCP server + strands 桥 + CLI + REST** |

**组合后的定位**：市面上多 agent 产品要么「无治理的群聊」（Claude Code Agent Teams），
要么「无 agent 的流程引擎」（传统 BPM），要么「终端里的 pane 堆」（herdr）。
**「有状态机内核 + 事件溯源 + 权限治理 + 常驻 agent 成员 + 桌面可回放」这个组合是空位。**

而且 SOPFlow 已经过对抗验证（52 真实 SOP 语料 / 616 节点 / 31 缺陷全修 / 复验 52/52 COMPLETED），
不是纸面设计。接入成本可控（MCP server 已在）。

**建议路径**：先做 B1（workflow 接团队）验证需求，
若用户确实需要「打回 / 闸门 / 子流程 / 审计」这类强治理，再引入 SOPFlow 作为编排内核，
而不是自己在 workflow DSL 上重造一遍。

---

## 6. 建议路线图

| 阶段 | 内容 | 量 | 为什么这个顺序 |
|---|---|---|---|
| **0 前置** | 修 backlog **P0-2**（Lead 工具不可达）+ P0-1/P0-3/P0-6 | S–M | 不修则一切自动分派建立在不可达工具上 |
| **1 接线** | **B1** workflow 节点指向团队成员 | S–M | 最高性价比：一次拿到流水线/并行/分支/闸门/循环 |
| **2 模式** | **B2** 编排模式库（模板） | S | B1 之后就是写 DSL，用户零学习成本 |
| **3 信任** | **A2** 结构化交接契约 + **D2** 预算熔断 | M | 解决「谎报完成」与「烧钱」两大信任问题 |
| **4 依赖** | **A1** 团队任务板 + **D1** 编排视图 | M | 从流程编排升级到依赖编排 |
| **5 自主** | **B4** 定时/渠道驱动 + **C2** worker_bus | M | 团队从「被戳」到「自主运转」 |
| **6 异构** | **C1** 外部 CLI 成员 + **A3** 产物板 | M–L | 扩大成员能力边界 |
| **7 治理** | **SOPFlow 集成**（按需） | M–L | 强治理需求出现后再引入，避免过早重造 |

**如果只做一件事**：**B1**。它用最小改动把 Ginno 已有的整套编排原语接到团队上，
且用户已经会用 workflow 页。

---

## 7. 明确不建议做的

| 项 | 理由 |
|---|---|
| 自研任务图调度器 | `create_subagent_plan` 已有 depends_on/并发/重试/amend，重造是浪费 |
| 自研流程引擎（闸门/打回/子流程） | workflow DSL 已有 parallel/branch/loop/human；强治理走 SOPFlow |
| 自研 agent→agent 邮箱 | worker_bus 已是现成管道（C2） |
| 多机/云端网格 | 本地桌面定位，是另一个产品（设计 §1 非目标） |
| 成员共享对话上下文 | 隔离是有意设计且是对的；用 A3 产物板解决协作，不要破坏隔离 |
| 总览页 live 多列对话 | 设计 §9.5 已决定等用户反馈；多路 WS 性能代价高 |

---

## 8. 待决问题

1. **B1 的节点语义**：workflow 节点指向成员时，是「投递并等回帖」（同步收敛）还是「投递即返回」（异步）？
   推荐同步（`wait` 原语已有），否则 DSL 的上下游数据流断了。
2. **A2 契约强度**：schema 缺证据时是**硬阻断**（不许标完成）还是**软警示**（标红但可 override）？
   推荐软警示 + 人工 override，避免误伤探索性任务。
3. **SOPFlow 引入时机**：先 B1 验证需求，还是一开始就用 SOPFlow 做内核？
   推荐前者——避免在需求未验证时引入重依赖。
4. **C2 权限边界**：成员间派活是否沿用工具层 ask 权限？推荐沿用（与设计 §5.7 一致）。
