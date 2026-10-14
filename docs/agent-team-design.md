# AgentTeam 模式设计（Agent 实体化 + 团队协作）

> 状态：**已实施（P0–P3 全部落地）**——实施对账见文末「§8 实施记录」
> 日期：2026-10-12（2026-10-13 重建；2026-10-14 P3 + 记忆/并发闸/徽标收尾）
> 前置：Ginno 现有 agent/session/subagent/delegation 机制（本文 §2 逐条对到文件与行号）
> 外部对标：Claude Code Agent Teams、OpenAgents Workspace、herdr、Vibe Kanban、DeepSeek Harness `limuyang2/agent-team`、OpenHands delegation
> 配套原型：`design-mocks/agent-team-design.html`（可交互，三个视图）

---

## 0. 一句话

**AgentTeam 不是一个新引擎，而是把 Ginno 已有的零件重新接线：**
`@agent` 从「本轮路由覆盖」升级为**跨 session 消息投递**，`parent_session_id` 树从「一次性任务委派」升级为**团队编制**，`AIMessage.agent_id` 从「交接分隔线」升级为**群聊回帖**。

三个原始需求 → 三个已有的落点：

| 需求 | 落点（现成零件） |
|---|---|
| ① 新界面，能同时看到多个 Agent 当前状态 | `_push_session_event` 会话级广播 + `AgentConfig.status` + 新 `/team` 路由 |
| ② Agent 下有 session，Agent 有独立记忆 | **已经是事实**：agents 目录下每个 agent 有自己的 `MEMORY.md` + per-session checkpoint |
| ③ 主会话 @Agent，结果回主会话 | `mentions[{kind:"agent"}]` 解析器 + `steer_enqueue` 投递通道 + `additional_kwargs.agent_id` 回帖渲染 |

---

## 1. 目标与非目标

**目标**

1. Agent 从「一份 JSON 配置」变成**有运行态的实体**：有身份、有会话、有状态、有记忆、可被 @ 点名、可被观察。
2. AgentTeam 模式下的主会话是一个**可寻址的协作空间**：用户 @ 谁，消息就进谁的工作台，结果以回帖回到主会话。
3. 用户始终能回答三个问题：**谁在干什么？谁卡住了？谁需要我？**（herdr 的原话："You never hunt for the stuck one."）

**非目标（本期不做）**

- 不做多机/云端 agent 网格（herdr `machine add` 那一层）。
- 不做 git worktree 隔离 / PR 自动化（Vibe Kanban、Agent Orchestrator 那一层）——Ginno 的隔离粒度是 session 与 context folder，不是 worktree。
- 不引入外部 agent 进程管理（Claude Code/Codex CLI 的托管已有 `delegate_agent` 覆盖）。

---

## 2. 现状盘点（可复用零件）

| 能力 | 现状 | 位置 | 缺口 |
|---|---|---|---|
| Agent 身份 | `AgentConfig{id,name,icon,color,system_prompt,provider,model,tools_allow,memory_scope}` | `agents/registry.py` | **只是配置**：无实例、无 session 绑定、无运行态持久化 |
| Agent 状态 | `status`（idle/running）运行时计算不落盘 | 同上 docstring | 无 blocked/waiting/done/unknown；前端无展示面 |
| Agent 独立记忆 | agents 目录下 `<id>/MEMORY.md` + `memory/` | `agents/memory.py` | ✅ 已具备；缺的是**团队共享记忆** |
| session = 独立上下文 | `FileCheckpointer` per session（full/delta） | `checkpointer.py` | ✅ |
| 父子 session 树 | `parent_session_id` / `depth` / `type ∈ {subagent, delegation}` | `session_meta.py`、`delegation_sessions.py`、`types.ts:166-203` | ✅ 语义是「任务委派」，需扩展为「团队编制」 |
| 会话内多 agent 轮流说话 | `AIMessage.additional_kwargs.agent_id` + 前端 `HandoffDivider` | `graph.py:1019-1179`、`TranscriptList.tsx:87-95` | ✅ **群聊渲染的现成基础，几乎零新增** |
| @agent | `mentions[{kind:"agent",id}]` → `agent_override`（本轮路由覆盖） | `commands/resolver.py`、`commands-and-mentions-design.md` §4.1 | **不能投递给另一个 session** |
| 跨 session 定向消息 | `steer_enqueue(session_id, entry)`（per-session stash，超步边界 drain） | `server_shared.py:168` | 定位是「人对运行中 agent 的干预」，无 agent→agent |
| 结果回传 | subagent 调度器：running→steer 注入 / idle→开新 turn | `subagent-design.md` §5.5 | 语义是「验收」不是「回帖」；父会话注入是**对话内容**，不是可点击的**消息对象** |
| 并发护栏 | 并发上限 5 / 深度 3 / 完成门（turn 结束 ∧ 无运行中子代） | `subagent-design.md` §5.3/§5.6 | ✅ 直接复用 |
| 实时状态推送 | `_push_session_event` + 会话级广播（断线重连不丢） | `server_shared.py:442` | ✅ |
| 路由与壳 | AppShell 跨路由保活；`/kb` `/workflows` `/scheduled` `/connectors` 独立页先例 | `AppShell.tsx:912-945` | 需要新增 `/team` |

**结论**：①③ 各差一层「投递语义」，②已经具备只差产品化。真正的重点在**交互设计与状态可见性**，而不是 runtime 大改。

---

## 3. 数据模型底座（三方案共用）

### 3.1 四层概念

```
Agent（角色模板，现有）        agents 目录下 <id>.json
  └─ AgentInstance（实体化）    实例：一个 agent 的一份运行态 + 一条 session
       └─ Session（上下文载体）  sessions/<session_id>.json（checkpoint）
Team（协作层）                  teams 目录下 <team_id>.json
  └─ 主会话 session（addressable 的协作空间）
```

关键判断：**Agent ≠ Session**。一个 agent 可以有多个实例（同一个人干两摊活），实例才是「有 session、有状态、被 @ 到」的对象。这是 `limuyang2/agent-team` 的核心洞见（"多次添加同一个助手，每次都会成为独立的团队成员实例"），也是 Claude Code agent teams 的做法（spawn 时可复用 subagent 定义，成员是独立实例）。

### 3.2 持久化

```jsonc
// teams 目录下 <team_id>.json
{
  "id": "t_7f3a",
  "name": "产品迭代小组",
  "project_slug": "default",
  "main_session_id": "…",          // 主会话（= 协作空间 = 用户 @ 人的地方）
  "leader_instance_id": "ai_01",   // 可选：领队成员
  "members": [
    {
      "instance_id": "ai_01",
      "agent_id": "dev",           // 指向 AgentConfig
      "display_name": "dev",       // 可改（herdr agent rename 的等价物），instance_id 不变
      "session_id": "…",           // 该成员的工作台（独立 checkpoint）
      "role": "leader" | "member",
      "status": "idle|working|blocked|waiting|done|unknown|error",  // 语义态（§3.3）
      "status_detail": "等待权限确认 / 正在跑 pytest…",               // 纯展示（§3.4）
      "seen": { "main": 1750000000, "pin": 1750000000 },             // 每客户端独立记（§3.3）
      "machine": "local",          // 预留多机（本期恒 local，§5.8）
      "last_active": 1750000000,
      "created": 1750000000
    }
  ],
  "shared_workspace": { "context_folders": ["…"], "primary_folder": "…" },
  "created": 1750000000
}
```

- 与 `agents/*.json` 同风格（单文件、可读、可手改、无 DB）。
- session meta 扩展：`type` 增加 `"team"`（主会话）与 `"team-member"`（成员工作台），沿用 `parent_session_id = main_session_id` + `depth`——**复用 `_session_meta_descendants` 的级联删除与侧栏树**，零新机制。

### 3.3 状态机（成员实例）

采用 herdr 的**五态语义模型**（idle / working / blocked / done / unknown），加 Ginno 自己的 `waiting`（嵌套完成门）与 `error`：

```
            @投递 / 派任务
idle ──────────────────────► working ──┬──► done     （turn 结束且用户【尚未看过】）
 ▲                            │  ▲     │
 │  用户查看该成员工作台        │  │     ├──► blocked  （有未答的 permission / ask_user 卡）
 └────────────────────────────┘  │     ├──► waiting  （turn 结束但还有运行中子代，§5.6 完成门）
        done → idle（seen）      │     ├──► error    （重试耗尽）
                                 │     └──► unknown  （状态不可判定：进程重启、turn 中断未愈合）
      人类答复 / steer 注入 ──────┘
```

**两个关键语义（照 herdr，修正我原先的设计）：**

1. **`done` ≠ 终态归档，而是「完成但你还没看」**。herdr 原文：*"done is idle but not yet marked seen; explicit pane focus / agent focus mark it seen, reads do not"*，且 *"each TUI client tracks viewed completions independently"*。
   → Ginno：`done` 是**收件箱语义**，用户打开该成员工作台才转 `idle`；主窗口与 pin 浮窗**各自维护 seen**，徽标可以不同。这是「谁需要我」能成立的根本机制——否则完成态一闪即逝，用户回来什么都看不到。
2. **`unknown` 是一等状态，不是 idle 的别名**。herdr 明确 *"unknown = present but unclassifiable, not proof of completion"*，且 Codex 故意 fallback 到 unknown 而非 idle（因为活跃 turn 与响应后的界面长得一样）。
   → Ginno：进程重启 / turn 被强杀未愈合时，成员状态标 `unknown` 而不是假装 idle。**宁可显示"我不确定"，不要显示错误的"它空闲"**——用户据此决定要不要点进去看。

**`blocked` 是这套设计里最有价值的一态**——herdr 的产品主张正是「你不用挨个 pane 去找谁卡住了」。

> **Ginno 在这里有结构性优势**：herdr 靠**读终端屏幕**猜状态（detection manifest 正则匹配 pane 底部的审批 UI），所以它必须"deliberately strict"，匹配不上就 fallback 到 idle，还会误判（*"unusual new prompts may initially show as idle instead of blocked"*）。
> Ginno **拥有事件流本身**——permission / ask_user 卡是 runtime 自己发出的结构化事件，`blocked` 是**权威的、零误判的**，不需要任何 manifest、屏幕解析或远程规则更新。这一条要写进产品叙事：**herdr 需要 22 个 agent 的适配清单，Ginno 一个都不需要。**

### 3.4 语义状态 vs 展示信息（必须分离）

herdr 的一条硬纪律：*"state controls waits, notifications, and rollups. The summary token is display-only."* 二者用不同的上报通道（`pane.report_agent` vs `pane.report_metadata`），互不污染。

→ Ginno 照做，修正我原先把 `status_detail` 和 `status` 混在一个字段里的设计：

| 字段 | 性质 | 驱动什么 | 举例 |
|---|---|---|---|
| `status` | **语义态**，枚举，权威 | 状态上卷、`wait` 收敛、通知、徽标 | `blocked` |
| `status_detail` | **纯展示**，自由文本，可随时变 | 只渲染，**绝不参与任何逻辑判断** | 「正在跑 pytest」 |

理由：展示文本会频繁变化（每个工具调用都变），若让它参与逻辑，状态机会抖动；而语义态必须稳定到可以被 `wait` 依赖。

---

## 4. 三个候选方案

### 方案 A：「群聊总线」— 主会话即群聊（对标 OpenAgents Workspace、Signal）

**形态**：主会话彻底变成一条群聊 thread。用户 `@dev 把 X 改了`，消息以 @ 气泡发出；dev 在自己的工作台跑，结果以**带头像的回帖**落回 thread，点回帖展开完整 transcript。

```
┌ AgentTeam · 产品迭代小组 ──────────────────────────┐
│ ● dev working  ○ writer idle  ⏸ research blocked   │  ← 顶栏状态条
├────────────────────────────────────────────────────┤
│ 你  @dev 把 auth 模块的 token 刷新改一下           │
│ ┌ dev ─────────────────────────────────────────┐  │
│ │ 收到，正在改。改完了：3 个文件，测试 12 passed │  │  ← 回帖（可展开全文）
│ └────────────────────────────────────────────────┘  │
│ 你  @writer 基于上面的改动写一段 release note       │
│ ┌ writer ───────────────────────────────────────┐  │
│ │ ⏸ 需要确认：发在哪个分支？                     │  │  ← blocked 直接可见
│ └────────────────────────────────────────────────┘  │
└────────────────────────────────────────────────────┘
```

| 维度 | 评价 |
|---|---|
| 复用度 | **最高**。`@agent` 解析器、`additional_kwargs.agent_id` + HandoffDivider 渲染、group thread 全部现成 |
| 认知成本 | 最低（Slack 心智模型，用户零学习） |
| 并发可见性 | 弱。多 agent 同时跑时，thread 被回帖刷屏；"谁在干什么"要在状态条里猜 |
| 主要风险 | 主会话上下文被回帖撑爆（需要「回帖全文进成员 session、主会话只留摘要」的纪律） |
| 改动量 | 小：投递路由 + 回帖写 checkpoint + 状态条 |

### 方案 B：「工位墙」— 主会话即调度台（对标 herdr、Claude Squad、Vibe Kanban）

**形态**：不再有"一个主对话"，而是一面**agent 工位墙**：每人一列，列内是各自的实时对话流。用户在顶部输入框 @ 谁，消息落到那一列；也可以点进任意列直接对话。状态用行头圆点表达（working/blocked/idle/done）。

```
┌ AgentTeam ────────────────────────────────────────────────┐
│  @dev 改 auth 的 token 刷新          [全部] [仅活跃]        │
├──────────────┬──────────────┬──────────────┬──────────────┤
│ ● dev working│ ○ writer idle│ ⏸ res blocked│ ✓ wf done    │
│ ├ 改 auth.ts │              │ ├ 找不到 API │ ├ 已交报告   │
│ ├ pytest 12✅ │              │   key，确认? │              │
│ ├ 提交完成   │              │              │              │
└──────────────┴──────────────┴──────────────┴──────────────┘
```

| 维度 | 评价 |
|---|---|
| 复用度 | 高。每列 = 一个 `ChatStream` 实例（PinStream 已证明协议可 100% 复用）；herdr 的状态语义直接照搬 |
| 并发可见性 | **最强**。四路并行一眼看全，卡住的自己举手 |
| 认知成本 | 中。用户要建立"看墙"的习惯；窄屏不友好（4 列已在笔记本上捉襟见肘） |
| 主要风险 | 每个可见列都是活 WS + 活渲染，性能与内存；「主会话」概念被稀释（用户需求 ③ 的"主会话"落点模糊） |
| 改动量 | 中：多 session WS 编排 + 布局；投递逻辑同 A |

### 方案 C：「领队工坊」— 主会话即 Lead（对标 Claude Code Agent Teams、dsh agent-team）

**形态**：主会话保留完整对话形态（现状不变），但右侧多一个**成员面板**；成员是独立根 session，有自己的状态、记忆、模型。用户在主会话 @ 成员 = 直接给该成员发消息（**不经过 Lead**）；也可以让 Lead 自主分派。结果回到主会话，Lead 负责汇总与验收。点成员行 → 打开该成员的完整 transcript 视图。

```
┌ AgentTeam · 产品迭代小组 ──────────────┬─ 成员 (3) ──────┐
│ 你  @dev 把 auth 的 token 刷新改一下    │ ● dev working   │
│                                         │   改 auth.ts…   │
│ Lead：已分派给 dev，我在等               │ ○ writer idle   │
│                                         │ ⏸ research 等你 │
│ ┌ dev ▸ 回帖（可展开）─────────────────┐│ ✓ wf done·未读  │
│ │ 改完 3 个文件，12 passed            ││ + 添加成员      │
│ └──────────────────────────────────────┘│                │
└─────────────────────────────────────────┴────────────────┘
```

| 维度 | 评价 |
|---|---|
| 复用度 | 高。成员面板 ≈ 现有子会话树（`AppShell.tsx:364-745` 已渲染 parent/子行 + 状态点）；`spawn_subagent`/`list_subagents`（含 wait）已是完整的 Lead 工具箱 |
| 并发可见性 | 中高。面板恒在、状态一目了然；点进去看全文 |
| 认知成本 | 中低。主对话习惯完全不变，多了一个"团队成员名单" |
| 主要风险 | 与 subagent 概念的边界要讲清楚（subagent = 临时工；team member = 常驻）；token 成本高于单 agent（多份独立 context） |
| 改动量 | 中：实例化 + 面板 + 投递（投递逻辑同 A） |

### 4.4 对比与推荐

| 维度 | A 群聊总线 | B 工位墙 | C 领队工坊 |
|---|---|---|---|
| 需求①「同时看多 Agent 状态」 | 弱 | **强** | 中高 |
| 需求③「主会话 @ + 结果回主会话」 | **强**（本身就是） | 中 | **强** |
| 与现有代码契合 | **最高** | 高 | 高 |
| 认知成本 | **最低** | 中 | 中低 |
| 多路并行可观测 | 低 | **最高** | 中 |
| 窄屏/笔记本可用 | 好 | 差 | **好** |
| 实现风险 | 低 | 中（多 WS 性能） | 中 |

> **推荐：C 为骨架，A 为交互语汇，B 为总览视图。**
> - **主形态用 C**：主会话形态不变（用户需求 ③ 最自然），成员面板承担需求 ①，`@` 承担投递。
> - **回帖用 A 的渲染**：`additional_kwargs.agent_id` 已有，群聊回帖零成本。
> - **补一个 B 的总览页**：`/team` 的「团队总览」tab，网格展示所有成员的 live 状态（不是完整对话列，是状态卡 + 最近 3 行摘要），把并发可见性拉满而不付多 WS 的代价。

---

## 5. 推荐方案的产品设计

### 5.1 信息架构

```
左侧导航（AppShell 底部区，与 /workflows 同级）
  └─ 🤝 AgentTeam                     → /team

/team
  ├─ 团队选择器（多团队）+ 新建团队
  ├─ tab 1「主会话」                   ← 默认；= 该 team 的 main_session，套 ChatStream
  │     └─ 右侧：成员面板（常驻）
  ├─ tab 2「团队总览」                 ← 方案 B 的降本版：状态卡网格
  └─ 点成员 → 成员工作台（该 session 的完整 ChatStream，套成员顶栏）

主会话成员面板行：
  ● dev       working · 改 auth.ts · 4m12s        [进入] [停止]
  ○ writer    idle · 上次：release note           [进入]
  ⏸ research  blocked · 需要 API key             [进入]
  ✓ wf        done·未读 · 报告已回帖 (2 次)        [进入] [归档]
  ? test-run  unknown · 重启时 turn 未愈合        [进入]
```

### 5.2 交互：@ 投递（核心链路）

```
用户在主会话输入 "@dev 把 auth 的 token 刷新改一下"
  │
  ├─1 前端：mentions=[{kind:"agent", id:"dev"}]
  │
  ├─2 runtime（新分支，仅 team 模式）：
  │     resolve_mentions → agent_override  【旧语义：本轮换 persona】
  │        ↓ 在 team 主会话里改为
  │     deliver_to_instance(instance=ai_01, text=...)  【新语义：投递】
  │
  ├─3 投递路径（复用 steer 的既有二分法，subagent-design §5.4）：
  │     成员 blocked        → 原子拒绝，一个字都不发（§5.3.3）
  │     成员 session 在跑   → steer_enqueue(member_sid, entry)   ← 不打断，下一超步边界吸收
  │     成员 session 空闲   → 直接 invoke，开新 turn
  │
  ├─4 主会话落一条「已投递」回执气泡（不占 LLM context，纯 UI 事件）
  │
  ├─5 成员在自己的 session 里跑（独立上下文、独立记忆、独立工具白名单）
  │
  └─6 成员 turn 结束 → 回帖回主会话：
        主会话 checkpoint 追加 AIMessage(content=摘要,
                                   additional_kwargs={agent_id:"dev", team_instance:"ai_01"})
        前端按现成 agentId 渲染 → 带 dev 头像的回帖
        全文留在成员 session（点「展开」跳成员工作台）
        成员状态 → done（未读），用户看过才转 idle
```

设计要点与理由：

1. **投递不换 persona**：这是与现有 `@agent` 语义的**唯一行为差异**，且只在 team 主会话生效，普通会话保持旧行为（向后兼容，零回归）。
2. **回帖是"消息对象"不是"对话内容注入"**：subagent 现有回传是往父对话里塞一段 `<ginno_subagent_result>` 文本；回帖改用 `AIMessage.additional_kwargs.agent_id`，是**结构化、可折叠、可点击**的，且不污染主会话的 LLM 上下文（摘要可截断）。这也让主会话的"谁说了什么"天然可检索。
3. **不引入 agent→agent 直连**（与 `subagent-design` §5.4 的 V1 结论一致）：成员间不互相发消息，一切经过主会话。Claude Code 允许直接互发，但代价是权限模型被绕过（"agent 消息不构成任何批准"）；Ginno 的取舍是**可解释性优先**，V2 再评估。
4. **已投递 ≠ 已读**：回执气泡要能升级为「成员已开始 → 已完成」，靠 §5.3 的状态回流。

### 5.3 状态回流（需求 ① 的实现）

零新事件，纯聚合：

| UI 状态 | 判据（现有信号） |
|---|---|
| `working` | 成员 session 在 `_RUNNING_TURNS` 且无 pending interrupt |
| `blocked` | 该 session 有未答的 permission / ask_user 卡（事件已有，只需按 session 聚合） |
| `waiting` | 自己的 turn 已结束但还有运行中子代（沿用 subagent §5.6 完成门） |
| `done` | turn 结束且**该客户端尚未查看**（seen 标记，§3.3） |
| `idle` | 无 turn、无 pending、无子代、已 seen |
| `unknown` | 进程重启后 turn 状态不可判定 / `_heal_interrupted_turn` 未能愈合 |
| `error` | 重试耗尽（`AUTO_RETRY_MAX=2`）后终态 |

推送：复用 `_push_session_event` 的**会话级广播**（2026-10-05 已改为广播、断线重连不丢），新增一个 `team.changed` 事件带 `{team_id, members:[{instance_id, status, status_detail, last_active}]}`，主会话 socket 与 `/team` 总览页共用。成员状态变化由 turn 生命周期钩子触发（turn 开始/结束/被 interrupt/失败），不新增轮询。

`status_detail` 的取法（不额外调 LLM）：working 时取最后一条工具调用的可读标签（`toolLabels.ts` 已有映射，如「正在跑 pytest」）；blocked 时取 permission 卡的标题；idle 时取上次回帖摘要的前 40 字。**它只用于渲染，不参与任何判断**（§3.4）。

### 5.3.1 状态上卷（herdr 的核心工作流，必须抄）

herdr 原文：*"The sidebar rolls state upward. A blocked agent makes its pane, tab, and workspace look blocked. A working agent makes the workspace look active. A done agent stays visible until you view it."*

→ Ginno 的三级上卷，**优先级 blocked > unknown > working > done > waiting > idle**：

```
成员实例  ──上卷──►  团队行（侧栏）  ──上卷──►  全局徽标（导航项 / Dock）
```

| 层 | 展示 | 例 |
|---|---|---|
| 成员行 | 自己的语义态 + 展示文本 | `⏸ research blocked · 等你回答` |
| 团队行（侧栏） | 取全部成员的**最高优先级态** + 计数 | `👥 产品迭代小组  1 等你 · 2 working` |
| 导航项 / Dock | 只有 `blocked` 与 `done` 冒泡（需要用户行动的才打扰） | `🤝 AgentTeam  ●1` |

**上卷纪律（照 herdr 的两条）：**
1. **`done` 不会被自动清掉**——*"A done agent stays visible until you view it"*。用户不点进去，徽标就一直在。这是"离开半天回来还能知道发生了什么"的关键。
2. **只有需要行动的态才向上冒泡到全局**。working 只在团队行可见，不污染 Dock——否则 4 个成员并行时全局徽标永远亮着，等于没有信号。

### 5.3.2 `wait` 原语（脚本与 agent 都能用）

herdr 把「等某个 agent 到某个状态」做成一等原语（`agent wait --until blocked --timeout 120000`），这是它能被脚本和**其他 agent** 驱动的基础。

→ Ginno 对应能力，**直接扩展已有的 `list_subagents(wait=true)`**（同一套阻塞语义、同一个 `_TURN_STOP` 协作式打断），改成按成员实例寻址：

```python
list_members(wait=True, until=["blocked","done"], ids=None, timeout_s=0)
```

- `until` 可重复，默认收敛到 `idle | done | blocked`（与 herdr 默认一致）；
- `timeout_s=0` = 无限等（herdr 同款默认，但**必须在 UI 上可见**，否则用户以为卡死）；
- 用户 stop 可打断等待（复用 `_TURN_STOP`）；
- 到点返回各成员当前状态（partial 语义）。

**为什么这条重要**：有了 `wait`，「Lead 分派 → 等全部完成 → 汇总」才不需要轮询和 sleep，P3 的自主协作才有干净的实现基础。

### 5.3.3 投递的原子性（herdr 的教训，必须抄）

herdr v0.8.2 的一条修正：*"Prompting a recognized approval/question dialog now fails atomically with `agent_blocked`: no text and no Enter are sent."*
v0.9.0 又补：*"A timeout or `agent_prompt_stalled` still does not prove that no input was sent, so read the agent before retrying to avoid a duplicate prompt."*

→ Ginno 的投递规则：

1. **成员处于 `blocked` 时，投递直接失败并返回明确原因**，绝不把消息塞进队列让它"看起来发出去了"。用户必须先回答那个卡住的问题。UI 上 @ 补全菜单里 blocked 的成员标注「⏸ 需先答复」并禁用投递。
2. **投递超时 ≠ 未投递**。回执必须区分三态：`delivered`（已进 steer 队列/已开 turn）、`stalled`（发出去但成员没进入 working，可能已生效）、`failed`（确定没发）。`stalled` 时 UI 提示「请先查看该成员工作台再重试」，**不提供一键重发**——防止重复投递。
3. **投递成功要有可验证的证据**（herdr v0.9.0：*"confirms that both prompt text and Enter were sent"*）→ Ginno 以「成员 session 的 checkpoint 里出现了这条 HumanMessage」或「steer 队列里有这个 steer_id」为凭据，而不是以"函数没抛异常"为凭据。

### 5.4 记忆：Agent 独立 + 团队共享（需求 ②）

| 层 | 落点 | 注入时机 |
|---|---|---|
| **Agent 记忆**（已有） | agents 目录下 `<id>/MEMORY.md` + `memory/` | 该 agent 每次 turn 的系统提示（`read_agent_memory`，现状不变） |
| **实例记忆**（新） | teams 目录下 `<team_id>/members/<instance_id>/notes.md` | 仅该实例的 turn；用于「这个团队里 dev 的临时约定」 |
| **团队共享记忆**（新） | teams 目录下 `<team_id>/TEAM.md` | 所有成员的 turn 都注入（`wrap_context_section("team_context", …)`，与 `injected_wiki` 同范式） |

- 成员**不共享对话上下文**（这是 `limuyang2/agent-team` 反复强调的隔离价值：角色与模型上下文互不污染），只共享 TEAM.md + 共享 workspace（context folders）。
- 记忆写入沿用现有 memory 管线的「用户可见、可编辑、可撤回」纪律（memory-kb-gates），不引入自动静默写入。
- TEAM.md 由用户在团队设置里维护，或由 Lead 提议、用户确认（与 GINNO.md 同治理模型）。

### 5.5 与现有 subagent / delegation 的边界

| | subagent | delegation | **team member** |
|---|---|---|---|
| 生命周期 | 一次性任务，完成即归档 | 一次性外部进程调用 | **常驻**，跨多轮、跨天 |
| 上下文 | fresh（standalone brief） | 外部 agent 自管 | **持久累积**（自己的 checkpoint 一直长） |
| 记忆 | 继承父 agent | 无 | **agent 记忆 + 实例记忆 + 团队记忆** |
| 谁发起 | 主 agent 或 `/subagent` | 主 agent | **用户 @**（V1）/ Lead 分派（V2） |
| 结果 | 摘要注入父对话 | 回执 + 归档 | **回帖到主会话**（结构化消息） |
| 复用关系 | — | — | team member **可以** spawn subagent（成员自己拆活） |

一句话：**subagent 是临时工，team member 是同事。** 二者共用 session 树、状态机、并发护栏，不共用生命周期语义。

### 5.6 并发、成本与护栏

沿用 subagent 的三件套并针对常驻语义调整：

- **成员数上限**：默认 6（硬限）。理由：面板可读性 + 并发 token 成本；超出提示「先归档不活跃成员」。
- **同时 working 上限**：默认 3（`settings` 可调）。超出的投递进入该成员的**待处理队列**（成员面板显示「排队 2」），成员空闲后自动 drain——比直接拒绝更符合「同事」心智。
- **深度**：team member 内部可 spawn subagent，仍受全局 depth≤2 约束（team 主会话不计入 depth）。
- **成本可见**：成员面板每行显示该实例累计 token（`usage` 事件已有 per-session 计数）；总览页显示团队合计。多份独立 context 的成本高于单 agent，必须让用户看得见（Claude Code 文档同样明确警告 agent teams "use significantly more tokens"）。
- **stop 级联**：停成员 → 其运行中 subagent 一并协作式停止（复用现有遍历）；解散团队 → 全部成员停止 + 归档（checkpoint 保留，可回放）。

### 5.7 权限

- 成员**继承团队的 permission 策略，只可收紧**（与 subagent 同一规则）。
- 成员的 permission / ask_user 卡出现在**成员工作台**，同时把 `blocked` 状态冒泡到主会话面板与总览页——用户在哪儿都能看见「谁在等我」，点进去代答。
- **投递不等于批准**：用户 @ 成员只表达「请你做这件事」，成员执行敏感操作时照常走 permission。这条要写进成员的系统提示（防止模型把 @ 当成免检通行证）。

### 5.8 持久性：「关掉窗口它还在跑」（herdr 的第一卖点）

herdr 的整个产品立论是 *"Always running. Herdr isn't an app you keep open... Close the lid or drop the network and the agents keep working; restart the machine and Herdr brings the layout back and resumes their sessions."*

**Ginno 的处境**：sidecar 是常驻进程 + FileCheckpointer 落盘，所以「关掉前端窗口 / 断 WS，成员的 turn 继续跑」**天然成立**（`_TURN_TASKS` 在服务端，WS 只是观察者；2026-10-05 已把回复改成广播，断线重连不丢）。这是既有资产，不是新工作。

**但有两个真实缺口，必须补：**

| 缺口 | herdr 的做法 | Ginno 的对策 |
|---|---|---|
| **应用重启后状态丢失** | 恢复保存的 layout + 支持的 agent 可 resume 原 session | 团队编制落盘（§3.2）→ 重启后重建成员面板；成员 session 靠 checkpoint 天然可回放；**turn 中断的走 `_heal_interrupted_turn`，愈合不了就标 `unknown`**（§3.3），绝不假装 idle |
| **「离开期间发生了什么」无从知晓** | `done` 保持可见直到你查看（seen 语义） | 团队级**未读汇总**：重连时给主会话推一条「你离开的 42 分钟里：dev 完成 2 项、research 卡在权限、writer 完成 1 项」——这是 `done` 语义的团队版，也是 OpenAgents「Inbox」的等价物 |

> **明确不做**：herdr 的 `machine add`（多机 SSH 网格）、`--remote` 远程 attach、direct terminal attach。Ginno 是本地桌面应用，多机是另一个产品。但**团队编制文件的格式要为它留余地**（成员条目里预留 `machine` 字段，本期恒为 `"local"`）。

### 5.9 Agent-native：同一套控制面给人和 agent 用

herdr 的第 3 条产品主张：*"The CLI and the socket API are the same surface agents drive. They split panes, start each other, prompt each other, and wait until another agent is genuinely blocked instead of firing keystrokes and hoping."*

它的实现是三层同一个底座：CLI wrapper → agent skill → raw socket，全部走同一个 newline-delimited JSON 本地 socket。

→ **Ginno 的对应做法**：团队控制面只实现一次，三个入口共用：

| 入口 | 谁用 | 形态 |
|---|---|---|
| HTTP/WS API（`api/teams.py`） | 前端 UI | 权威实现 |
| 工具（`list_members` / `deliver_to_member` / `assign_task`） | **Lead agent 与其他成员** | 薄封装，直接调同一批函数 |
| Skill（`/team`） | 用户在任意会话里 | 提示词 + 调同一批工具 |

**关键纪律**：工具层**不得**复制一份状态逻辑。herdr 之所以能做到"agent 等到另一个 agent 真的 blocked 而不是盲发按键"，正因为 `agent wait` 和 UI 读的是同一个服务端状态。Ginno 同理——`list_members(wait=True)` 与成员面板必须读同一个 `teams/status.py`。

这也让 P3（Lead 自主分派）从"新功能"降级为"把已有 API 暴露成工具"，风险大幅降低。

---

## 6. 实现计划（分期）

### P0：实体化 + 投递（最小可用闭环，约 3-4 天）

| 改动 | 位置 |
|---|---|
| `teams/` 存储模块（CRUD + 锁 + 原子落盘，照 `session_meta._IDX_LOCK` 的教训做） | 新 `packages/runtime/src/ginno_runtime/teams/store.py` |
| 状态聚合（语义态 + seen 标记 + 上卷），**唯一权威来源** | 新 `teams/status.py` |
| session meta `type` 增加 `"team"` / `"team-member"` | `types.ts`、`api/sessions.py` |
| 投递分支：team 主会话里 `agent_override` → `deliver_to_instance`（含 blocked 原子拒绝 + 三态回执，§5.3.3） | `commands/resolver.py` + `api/stream/turn.py` |
| 回帖写主会话 checkpoint（`additional_kwargs.agent_id`） | `api/stream/` 成员 turn 结束钩子 |
| `team.changed` 状态聚合事件 | `server_shared.py` |
| API：`GET/POST/PATCH /api/teams`、`POST /api/teams/{id}/deliver` | 新 `api/teams.py` |

**验收**：建团队 → 主会话 @dev → dev 在自己 session 跑 → 回帖出现在主会话 → 面板状态 working → done → 点进工作台后转 idle；research 处于 blocked 时 @ 它被原子拒绝并提示先答复。

### P1：界面（约 3 天）

- `/team` 路由 + AppShell 底部导航项（照 `/workflows` 先例）。
- 主会话 tab：ChatStream + 右侧成员面板（复用 `AppShell.tsx:524-745` 的行渲染与状态点样式）。
- **状态上卷**（§5.3.1）：侧栏团队行徽标 + 导航项全局徽标（只冒泡 blocked / done）。
- 成员工作台：ChatStream + 成员顶栏（角色/模型/状态/返回主会话），working 时输入 = steering，idle 时 = 继续对话（**与 subagent 视图逐字节同构**，可直接复用 `RunSubSessionView.tsx` 的模式）。**打开即标记 seen**（done → idle）。
- 回帖卡片：折叠摘要 + 「展开全文」跳成员工作台。
- 未读汇总条（§5.8）：WS 重连时推「你离开的 N 分钟里…」。

### P2：总览页 + 记忆分层 + 持久性（约 3 天）✅ 已实施

- 「团队总览」tab：状态卡网格（方案 B 的降本版），每卡 = 头像 + 状态 + 最近 3 行 + token 计数；点击进成员工作台。**不挂多路 WS**，只订阅 `team.changed` + 按需拉最近消息。
- 实例记忆 + TEAM.md 注入（`wrap_context_section`）。✅ 2026-10-14 补齐（`teams/memory.py`，P2 当初只落了总览页/持久性）。
- **重启恢复**（§5.8）：应用启动时从 teams 编制文件重建面板；`_heal_interrupted_turn` 愈合失败的成员标 `unknown`。

### P3：Lead 自主分派 + wait 原语（约 2 天）✅ 已实施（2026-10-14）

- `list_members(wait=True, until=[...], ids=None, timeout_s=0)`——**扩展已有的 `list_subagents(wait=true)`**，同一套阻塞语义与 `_TURN_STOP` 打断，换成成员实例寻址（§5.3.2）。
- `deliver_to_member(instance_id, text)` / `assign_task(instance_id, goal)`：与 HTTP API **共用同一批函数**，工具层不复制状态逻辑（§5.9）。
- 这是 Claude Code agent teams 的「self-coordination」+ herdr 的「agent-native」能力；放在最后，因为 P0-P2 已满足全部三条原始需求。

### P3+（2026-10-14 顺带收尾，原型对照补齐）

- **成员并发闸**（§5.6「成本失控」）：同团队同时在跑的成员 turn 上限 3（`MAX_CONCURRENT_RUNNING`）；满了排队（回执 `delivered` + `channel=queue`），turn 结束自动 drain；blocked 原子拒绝仍优先于排队。
- **Dock 徽标冒泡**（§5.3.1）：`GET /api/teams/badge`（blocked + done 未读合计）→ Tauri 壳 `badge.rs` 轮询 → macOS Dock badge。只有 blocked/done 冒泡，working 止于团队行。

---

## 7. 风险与取舍

| 风险 | 应对 |
|---|---|
| 主会话上下文被回帖撑爆 | 回帖只写摘要（>500 字压缩，复用 subagent §5.5 的摘要链路）；全文永远在成员 session |
| `@agent` 语义分叉造成困惑 | 只在 team 主会话改变行为；普通会话不变；UI 上 team 主会话的 @ 补全项标注「→ 投递给成员」 |
| 多成员并发改同一文件 | 与 subagent 同一现实：进程级写锁只防踩踏不防语义冲突。靠团队共享 workspace 的 context folder 划分 + brief 里「分文件」约束；V2 可评估 worktree 隔离（Vibe Kanban 路线） |
| 成本失控 | 成员数/并发上限 + per-instance token 常显 + 总览页合计 |
| 团队状态文件并发写坏 | 照 `session_meta.py` 的 2026-10-05 事故教训：所有写路径串行（`threading.RLock`）+ tmp+rename 原子落盘 |
| 与 subagent 概念重叠 | §5.5 的边界表写进 user-guide；UI 上 team 成员面板与子会话树**分区渲染**，不混排 |
| 状态误判（把卡住的显示成空闲） | Ginno 用**结构化事件**判态而非屏幕解析，天然无误判；不可判定时标 `unknown` 而非 fallback 到 idle（§3.3） |
| 投递重复/丢失 | 三态回执 `delivered / stalled / failed`；`stalled` 时禁止一键重发，要求先查看成员工作台（§5.3.3，herdr v0.9.0 同款教训） |
| 全局徽标噪音 | 只有 `blocked` 与 `done` 冒泡到导航项/Dock，working 止于团队行（§5.3.1） |
| 展示文本参与逻辑导致状态抖动 | `status`（语义）与 `status_detail`（展示）严格分离，后者绝不进判断（§3.4） |

---

## 8. 对标结论（为什么这样选）

| 项目 | 借鉴 | 不借鉴 |
|---|---|---|
| **Claude Code Agent Teams** | Lead + 独立 context 的成员、共享任务列表、`blocked` 可见、成员可复用 subagent 定义、成本警告 | agent→agent 直连邮箱（绕权限）、tmux/iTerm2 分屏（Ginno 是桌面应用，不需要终端复用） |
| **OpenAgents Workspace** | @mention 投递、shared thread、Inbox（= 我们的未读汇总 + blocked 冒泡）、Task board | 多人协作/账号体系/移动端（Ginno 是本地单用户） |
| **herdr** | 见 §8.1 专节（本次重点参考） | 终端 pane 托管、屏幕解析式状态检测、多机 SSH 网格、direct terminal attach |
| **Vibe Kanban** | 看板作为总览形态（我们的「团队总览」tab 是它的轻量版） | git worktree per task、PR 自动化、issue tracker（超出本期范围） |
| **limuyang2/agent-team** | **成员是独立根 agent 而非 subagent**、同一助手可多实例、成员不共享对话上下文只共享 workspace、per-member 模型/权限/思考模式、配置快照 | 悬浮球入口（Ginno 有正式导航）、DSH 插件生命周期 |
| **OpenHands delegation** | task/subtask 的对话层级划分（印证我们的「主会话/成员 session」两层） | Docker 沙箱运行时（Ginno 走 permission + context folder） |

**Ginno 的差异化**：别人要么在终端里堆 pane（herdr / Claude Squad），要么把 agent 当一次性 worker（Vibe Kanban / Bernstein）。Ginno 是桌面应用，**成员工作台是一等公民**——每个成员都有完整可回放、可 steering、可搜索的对话，这是终端形态给不了的。这一点在 `subagent-design.md` §3 已经作为差异化优势写明，AgentTeam 是它的自然延伸。

### 8.1 herdr 深度参考（本次重点）

herdr 是这批对标里**产品思考最深**的一个——它不造 agent，只做"agent 运行的地方"，因此把全部精力投在**可观测性与可寻址性**上，正是需求 ① 的核心。逐条对照：

| herdr 的设计 | 原文依据 | Ginno 采纳 | 落点 |
|---|---|---|---|
| **五态语义模型** idle/working/blocked/done/unknown | *"state_labels keys must be idle, working, blocked, done, or unknown"* | ✅ 全盘采纳 + 加 `waiting`/`error` | §3.3 |
| **`done` = 完成但未读**，看过才转 idle，且**每客户端独立记 seen** | *"done is idle but not yet marked seen... each TUI client tracks viewed completions independently"* | ✅ 这是"离开半天回来还知道发生了什么"的根本机制 | §3.3、§5.3 |
| **`unknown` 是一等状态**，不是 idle 的别名 | *"unknown = present but unclassifiable, not proof of completion"* | ✅ 重启/愈合失败时标 unknown，绝不假装 idle | §3.3 |
| **状态上卷**：blocked 的成员让 pane/tab/workspace 都显示 blocked；done 保持可见直到你查看 | *"The sidebar rolls state upward... A done agent stays visible until you view it."* | ✅ 三级上卷（成员→团队行→全局徽标），只冒泡需行动的态 | §5.3.1 |
| **语义态 vs 展示信息严格分离** | *"state controls waits, notifications, and rollups. The summary token is display-only."* | ✅ `status` / `status_detail` 拆成两个字段 | §3.4 |
| **`agent wait --until blocked`** 作为一等原语，可被脚本和 agent 调用 | *"wait until another agent is genuinely blocked instead of firing keystrokes and hoping"* | ✅ 扩展已有 `list_subagents(wait=true)` | §5.3.2 |
| **投递原子性**：对 blocked 的 agent 发提示 → 原子失败，一个字都不发 | *"fails atomically with `agent_blocked`: no text and no Enter are sent"* | ✅ blocked 时拒绝投递，@ 菜单里禁用 | §5.3.3 |
| **超时 ≠ 未发送**，重试前必须先读，否则重复投递 | *"A timeout or `agent_prompt_stalled` still does not prove that no input was sent, so read the agent before retrying"* | ✅ 三态回执 delivered/stalled/failed，stalled 禁一键重发 | §5.3.3 |
| **CLI / skill / socket 三层同一个控制面** | *"Every layer — CLI wrappers, the agent skill, and the raw socket protocol — shares the same underlying control surface"* | ✅ API / 工具 / skill 共用同一批函数，工具层不复制状态逻辑 | §5.9 |
| **持久性**：detach 后继续跑，重启恢复 layout 与 session | *"Close the lid or drop the network and the agents keep working"* | ✅ 已有资产（sidecar 常驻 + checkpoint）+ 补重启恢复与未读汇总 | §5.8 |
| **自定义 agent 名**（`agent rename w1:p1 reviewer`） | 显示名与目标 id 解耦 | ✅ 成员实例可改显示名（`instance_id` 不变） | §3.2 |
| 屏幕解析式状态检测 + detection manifest + 远程规则热更新 | *"reads the live bottom of the pane... Rules in a detection manifest decide"* | ❌ **不需要**——见下 | — |
| 多机 SSH 网格 / `--remote` / direct terminal attach | `herdr machine add workbox` | ❌ 本期不做，但成员条目预留 `machine` 字段 | §5.8 |

**最重要的一条洞察 —— Ginno 在 `blocked` 检测上有结构性优势：**

herdr 必须**读终端屏幕猜状态**，所以它的 blocked 检测只能"deliberately strict"：*"Herdr only marks blocked when the live bottom-buffer snapshot matches known visible approval, question, or permission UI"*，匹配不上就 fallback 到 idle，并且承认会误判：*"unusual new prompts may initially show as idle instead of blocked until Herdr learns that screen shape"*。为此它维护了 22 个 agent 的适配清单、一套 manifest 版本协商机制、远程规则热更新、本地 override 优先级、以及 `herdr agent explain` 调试命令——**这一整套复杂度全部来自"它站在 agent 外面看屏幕"**。

Ginno **站在 agent 里面**：permission 卡、ask_user 卡、turn 开始/结束、interrupt 都是 runtime 自己发出的结构化事件。所以：

- `blocked` 是**权威的、零误判的**，不需要 manifest、不需要屏幕解析、不需要远程规则更新；
- 新增一个 agent 类型（`agents/subagents/*.md`）**自动获得完整状态语义**，不需要任何适配工作；
- 不需要 `agent explain` 这种"为什么状态错了"的调试命令——状态不会错。

这条应该写进产品叙事：**herdr 需要 22 个适配清单才能知道谁卡住了；Ginno 一个都不需要，因为事件是它自己发的。**

**反过来，herdr 有而 Ginno 该学的**：它对"状态语义"的严谨程度远超一般产品（done/seen/unknown 的区分、上卷优先级、投递原子性、超时不等于失败）。这些都不是终端形态特有的，是**多 agent 可观测性的通用真理**，本方案已逐条吸收。

---

## 9. 待决问题（评审时定）

1. **团队与项目的关系**：团队绑 project_slug（推荐，与 session 一致）还是全局？
2. **主会话的 Lead 是谁**：固定一个 agent 当 Lead（推荐 dev/general），还是主会话无 persona、纯路由？
3. **成员离线**：团队关闭后成员 session 是否保留？（推荐保留 checkpoint，归档不删——成员是常驻资产，与 delegation 的 `_MAX_DELEGATION_SESSIONS` 清理策略不同）
4. **@ 多个成员**：`@dev @writer 各自做 X` 是并行投递两条，还是禁止？（推荐并行，受并发上限约束）
5. **总览页是否需要 live 对话预览**：P2 先做状态卡，若用户反馈不够再升级为方案 B 的多列 live 视图。
6. **seen 的粒度**：按客户端（主窗口 / pin 浮窗各记一份，herdr 做法）还是全局一份？推荐按客户端，但需要确认 pin 浮窗是否要展示团队徽标。

---

## 10. 配套原型

`design-mocks/agent-team-design.html` —— 可交互 HTML（沿用仓库现有 mock 的设计系统），三个视图：

| 视图 | 展示 |
|---|---|
| **主会话** | 未读汇总条（§5.8）→ @ 投递气泡 → 投递回执 → dev 回帖卡（可展开全文）→ 并行投递 2 条 → research 的 blocked 卡 → **投递被原子拒绝**（§5.3.3）→ Lead 汇总；右侧常驻成员面板（5 成员 × 语义态/展示文本/token/ctx 条 + 上卷说明）；@ 补全菜单（blocked 成员标 ⛔ 不可投递） |
| **团队总览** | 方案 B 的降本版：状态卡网格（含一张 `unknown` 卡）+ 设计理由与 herdr 对比说明 |
| **成员工作台** | 完整 ChatStream + 成员顶栏（三层记忆 chip）+ steering 输入框 |

---

## 11. 实施记录（对账）

设计六里程碑全部落地；本节是「方案 → 代码」的索引，供排障与后续演进对照。

| 批次 | 日期 | 内容 | 落点 |
|---|---|---|---|
| P0 | 2026-10-13 | 实体化 + @ 投递 + 回帖 + 状态聚合（commit `d5b6152`） | `teams/store.py`、`teams/status.py`、`teams/deliver.py`、`api/teams.py` |
| P1 | 2026-10-13 | `/team` 独立空间 + 成员面板/工作台/回帖卡（commit `4a04cf4`） | `apps/web/src/components/team/`、`app/team/` |
| P2 | 2026-10-13 | 产品重构：完全隔离 + 左栏团队列表 + 成员多会话 + 重启恢复 + 实体领队（commits `1f6a615`、`8752c38`） | `TeamShell.tsx`、`teams/route.py`、`api/teams.py::recover_teams` |
| P2 尾巴 | 2026-10-14 | 记忆分层：TEAM.md + 实例 notes.md 注入（§5.4） | `teams/memory.py`、`api/stream/turn.py`（`team_context` 段）、`TeamMemoryModal.tsx` |
| P3 | 2026-10-14 | Lead 自主分派：`list_members(wait=True)` / `deliver_to_member` / `assign_task`（§5.3.2/§5.9） | `tools/team.py`、`teams/wait.py` |
| P3+ | 2026-10-14 | 成员并发闸（§5.6）+ Dock 徽标冒泡（§5.3.1） | `teams/deliver.py`（`_QUEUES`/`_drain_queue`）、`api/teams.py::/badge`、`apps/desktop/src/badge.rs` |

**实现与设计的偏差**（均为实施时修正）：

1. 团队不在主侧栏分组，而是**完全隔离的 `/team` 空间**（P2 产品重构定稿）——「我的对话」与「团队空间」是两个世界；设计稿的侧栏分组图按此理解。
2. Lead 双落点：主会话 persona（无 @ 时路由模块默认投给它）+ 实体 leader 成员实例（有自己的会话，2026-10-14 用户定稿）。
3. 成员间直接投递仍按 §5.2 要点 3 推迟（成员工作台里 @ 别人 = 旧 persona 语义）；多机（§5.8）`machine` 字段恒 `"local"`。
4. 回帖上限实施为 1200 字截断（`REPLY_MAX_CHARS`），非 LLM 压缩——成员 turn 本身已有摘要，主会话侧截断已够。

**测试索引**：`tests/unit/test_team_store_status.py`（存储/状态）、`tests/unit/test_team_p3.py`（记忆/wait/并发闸）、`tests/api/test_team_p0.py`（端到端 + 记忆/徽标/工具绑定端点）。
