# Workflow UX 全面优化设计方案

> 覆盖 13 项改进，从核心执行体验到辅助功能。
> 遵循现有设计语言：lucide-react 图标、自定义 CSS 变量（`text-txt / text-muted / text-faint`、`bg-panel / bg-card`、`text-violet / text-red / text-yellow / text-green / text-blue`）、无 `dark:` 前缀。

---

## 设计原则

1. **就近原则** — 信息在哪里产生，提示就在哪里出现；不强迫用户跳转页面才能知道发生了什么。
2. **主动等待 ≠ 被动观察** — 系统等待用户操作（human 节点、diff 确认）的视觉信号必须明显强于系统自动执行中的状态。
3. **可逆性优先** — 所有影响 DSL 或数据的操作（apply diff、rollback、重试）必须单步可撤销，不需要二次确认弹窗。
4. **不打断工作流** — 新增 UI 元素在默认态占用空间极小，需要时才展开。

---

## 优先级总览

| # | 功能 | 优先级 | 改动范围 | 影响 |
|---|------|--------|----------|------|
| 1 | version_propose diff 确认卡 | P0 | 前端（新组件）+ 后端（无变更） | workflow 编辑闭环 |
| 2 | HumanNode 等待态增强 | P1 | 前端（LiveRunBlock + RightDock）| 人机协作核心体验 |
| 3 | 运行中工具调用可见性 | P1 | 前端（store + LiveRunBlock）| 减少「黑箱焦虑」 |
| 4 | Supervisor 面板 + 事件样式 | P2 | 前端（Timeline + Inspector）| 调试可见性 |
| 5 | 版本历史 + Diff 入口 | P2 | 前端（新组件 + Inspector）| 版本控制可用 |
| 6 | 从失败步骤重试 | P2 | 前端 + 后端（新端点）| 减少重复执行成本 |
| 7 | Workflow 列表搜索/筛选 | P3 | 前端 | 组织管理 |
| 8 | Context 变量提示 | P3 | 前端（ContextEditor）| 填写引导 |
| 9 | Browser 通知 | P3 | 前端（store）| 后台感知 |
| 10 | 批量清除增强 | P3 | 前端 | 清洁度 |
| 11 | supervisor_intervene 事件样式 | P3 | 前端（Timeline）| 日志可读性 |
| 12 | Settings DSL 编辑器升级 | P3 | 前端 | 创建体验 |
| 13 | 自适应 stuck 检测 | P3 | 前端（store + RunBlocks）| 准确性 |
| 14 | 手动暂停 / 继续（运行中随时暂停，断点恢复） | P1 | 后端（engine 控制通道 + pause 端点）+ 前端（LiveRunBlock/ChatStream/WorkflowPanel） | 执行可控性 |
| — | **从会话总结 Workflow** | — | — | — |
| S1 | 聊天页入口 + session 选择器 | P0 | 前端（chat header）| 入口可达性 |
| S2 | SummarizeModal 重设计（DAG 预览 + 可编辑）| P0 | 前端（SummarizeModal）| 结果可审阅 |
| S3 | 草稿可编辑（节点名/目标/变量）| P1 | 前端（SummarizeModal）| 一次提交精确率 |
| S4 | 后端：DSL 生成质量增强 | P1 | 后端（api/workflows.py）| 生成准确率 |
| S5 | session 选择器 + 分段标注 | P2 | 前端 + 后端 | 灵活性 |
| S6 | 草稿持久化（不丢失）| P3 | 前端（localStorage）| 容错 |

---

# Part 1 — 产品设计方案

---

## P0 · DSL 变更确认卡（version_propose）

### 问题
`workflow-dev` agent 调用 `workflow_propose_edit` 工具后，workflow 进入 `paused` 状态，但前端没有任何 UI 让用户看到提案内容、审阅 diff、或决策 accept/reject。用户唯一能做的事是在 WorkflowPanel 看到一个黄色的 `paused` 徽标，却不知道为什么暂停。

### 设计目标
在 `LiveRunBlock` 内嵌一张 DSL 变更提案卡，用户无需跳转页面即可审阅 diff 并一键决策。

### UI 规范

**卡片出现位置**：`LiveRunBlock` 的步骤列表之后，作为最后一个区块插入（`status=paused` 且最新 interrupt 事件 `kind=version_propose` 时渲染）。

```
┌──────────────────────────────────────────────────────────┐
│ ⏸  PR Triage · workflow-dev 正在等待确认        02:14   │
├──────────────────────────────────────────────────────────┤
│  ✓  拉取 PR 列表                                          │
│  ✓  分析 PR 内容                                          │
│  ⏸  等待 DSL 确认                                        │
├──────────────────────────────────────────────────────────┤
│  📝  DSL 变更提案                            v3 → v4      │
│  workflow-dev 建议：新增安全审查节点，调整分析 prompt      │
│  ─────────────────────────────────────────────────────   │
│  ▾ 查看完整 diff  (3 处改动)                              │
│  ─────────────────────────────────────────────────────   │
│  - name: analyze_pr                                       │
│  -   prompt: "分析这个 PR"                                │
│  + name: analyze_pr                                       │
│  +   prompt: "分析 PR，重点关注安全漏洞和性能影响"          │
│  + - id: security_check                                   │
│  +   type: step                                           │
│  +   title: "安全审查"                                    │
│  ─────────────────────────────────────────────────────   │
│           [✓ 应用变更]          [✕ 拒绝]                  │
└──────────────────────────────────────────────────────────┘
```

**视觉规范**：
- 卡片容器：`rounded-md border border-yellow/30 bg-yellow/[0.04]`
- 标题行图标：`FileEdit`（lucide），`h-3.5 w-3.5 text-yellow`
- 版本 badge：`text-[11px] text-faint ml-auto`
- diff 区：复用 `DiffView` 组件，`max-h-48 overflow-auto`，默认收起只显示摘要行，点击展开
- [应用变更] 按钮：`btn-press bg-violet text-white`
- [拒绝] 按钮：`border border-line text-muted hover:bg-red/10 hover:text-red`
- 操作完成后：卡片变灰，显示结果文字（"已应用 · v4" 或 "已拒绝"），不可再次操作

**交互逻辑**：
1. 用户点击 [应用变更] → `POST /api/workflow_runs/{id}/decide` `{decision: "allow"}` → run 恢复执行
2. 用户点击 [拒绝] → `POST /api/workflow_runs/{id}/decide` `{decision: "deny"}` → run 恢复执行（agent 收到拒绝，可继续修改或结束）
3. 操作后按钮立即禁用 + spinner，响应后改为结果文字

---

## P1 · HumanNode 等待态增强

### 问题
workflow 因 `human` 节点暂停时，与因 `version_propose` 暂停的外观完全相同——只有一个黄色 `paused` 状态标签。用户不知道系统在问自己问题，也不知道在哪里回答。

### 设计目标
区分两类暂停：系统等确认（`version_propose`）和系统问用户（`human`）。后者需要更强的视觉信号 + 就地回答 UI，并在右栏 dock badge 上独立计数。

### UI 规范

**LiveRunBlock 内嵌问答区（kind=human interrupt）**：

```
┌──────────────────────────────────────────────────────────┐
│ 🙋  TODO 同步 · 需要你的输入                     02:15   │
├──────────────────────────────────────────────────────────┤
│  ✓  拉取最新 TODO                                         │
│  🙋  等待确认                              ← 节点状态图标 │
│  ╔════════════════════════════════════════════════════╗   │
│  ║  是否将以下 TODO 标记为 done？                      ║   │
│  ║    · #42  完成登录页面重构                          ║   │
│  ║    · #43  修复 CSP 头配置                          ║   │
│  ║                                                    ║   │
│  ║  回复（可选）                                       ║   │
│  ║  ┌──────────────────────────────────────────────┐  ║   │
│  ║  │                                              │  ║   │
│  ║  └──────────────────────────────────────────────┘  ║   │
│  ║  [✓ 确认继续]     [跳过]     [✕ 中止运行]          ║   │
│  ╚════════════════════════════════════════════════════╝   │
└──────────────────────────────────────────────────────────┘
```

**视觉规范**：
- run header 标题前图标改为 `MessageSquare`，颜色 `text-yellow`
- 步骤图标：waiting 步骤用 `MessageSquare h-3 w-3 text-yellow animate-pulse`
- 问答卡容器：`rounded-md border-2 border-yellow/40 bg-yellow/[0.05] p-3`
- question 文字：`text-xs text-txt whitespace-pre-wrap`（支持 markdown 渲染）
- textarea：`rounded border border-line bg-card text-xs resize-none min-h-[48px]`
- [确认继续]：`btn-press bg-violet`；[跳过]：普通小按钮；[中止运行]：`hover:bg-red/10 hover:text-red`

**RightDock badge 新增人工等待计数**：

```
  收起态 dock（当前）:
  [⚡2]   ← 蓝色脉冲，运行中计数

  新增 pending-human badge：
  [⚡2]  [🙋1]   ← 黄色，等待用户输入的 run 计数
```

- `pendingHumanCount` = `workflowRuns.filter(r => r.status === "paused" && latestInterruptKind(r) === "human").length`
- badge 样式：`bg-yellow text-black`，相同的 `animate-pulse` 效果
- 点击后打开右栏并自动滚动到第一个等待输入的 run card

**交互逻辑**：
1. [确认继续]（有回复）→ `POST /resume {answer: inputValue || ""}`
2. [跳过] → `POST /resume {answer: null, skip: true}`
3. [中止运行] → `POST /cancel`
4. 提交后输入区折叠，显示「已回复：{answer}」或「已跳过」

---

## P1 · 运行中工具调用可见性

### 问题
step 节点运行时，LiveRunBlock 只显示「节点名 + elapsed」，用户看不到 agent 在调用哪个工具、传了什么参数。长时间运行时用户无法判断是正常执行还是卡住。

### 设计目标
在当前运行节点下方显示最新一条 tool_call，轻量、不喧宾夺主，类似 IDE 底栏的状态提示。

### UI 规范

```
  步骤列表中，运行中节点的展示：

  ⚡  分析 PR 内容                          ← 节点行（原有）
      └ 🔧  search_code  ·  "auth refactor"  ← 新增工具调用行
```

**视觉规范**：
- 工具调用行缩进：`ml-5` 相对节点行
- 图标：`Wrench h-3 w-3 text-faint`
- 工具名：`text-[11px] font-mono text-muted`
- 参数摘要：`text-[11px] text-faint`，最长 50 字符截断 + `…`
- 整行：`flex items-center gap-1.5 text-[11px]`
- 当 tool_result 回来后，工具调用行消失（不展示历史，只展示当前进行中的）
- tool_call 同时有多个时，只显示最新一条

**tool_result 显示规则**：tool_result 事件到达后立即清除工具调用行，不展示结果内容（结果在 WorkflowLogTimeline 中查看）。

---

## P2 · Supervisor 面板真实 UI

### 问题
后端 supervisor 会在节点参数/输入校验失败时自动干预（coerce 类型、abort 执行等），并写入 `supervisor_intervene` 事件。但前端 Inspector 的 Supervisor 面板是纯占位文字，timeline 也没有专属样式，用户完全看不到干预发生了什么。

### 设计目标
让 supervisor 干预在 timeline 和 Inspector 中可见，用户能判断干预是否符合预期，为将来的 LLM decider 交互奠定 UI 基础。

### UI 规范

**WorkflowLogTimeline 中 supervisor_intervene 事件样式**：

```
  ◈  Supervisor 干预                         12:34:05
     节点: analyze_pr  ·  动作: coerce
     校验错误: temperature 期望 number，收到 "high"
     处理结果: 已强制转换为 0.7，继续执行
```

- 左侧图标：`ShieldAlert h-3.5 w-3.5 text-orange`
- 容器：`rounded border border-orange/30 bg-orange/[0.05] p-2`
- 标题行：`font-medium text-orange text-xs`
- 详情：`text-[11px] text-muted space-y-0.5`
- action badge：`coerce → 绿色`，`abort → 红色`，`patch_dsl → 紫色`，`skip → 灰色`

**WorkflowInspector Supervisor 面板（替换占位文字）**：

```
┌──────────────────────────────────────────────────────────┐
│  🛡  Supervisor                              自动模式     │
├──────────────────────────────────────────────────────────┤
│  本次运行  1 次干预                                        │
│  ┌────────────────────────────────────────────────────┐  │
│  │  ✓ coerce  ·  analyze_pr                          │  │
│  │  temperature: "high" → 0.7                        │  │
│  │  reason: 类型强制转换成功                           │  │
│  └────────────────────────────────────────────────────┘  │
│                                                           │
│  无干预时：「本次运行未触发 Supervisor 干预」（text-faint）│
└──────────────────────────────────────────────────────────┘
```

- 面板标题图标：`Shield h-4 w-4`
- 模式标签（右侧）：`text-[11px] text-faint`，当前写死「自动模式」，为未来 LLM decider 预留
- 干预卡片：`rounded border border-line bg-card p-2 text-xs space-y-0.5`
- action → `coerce/patch_dsl` 用绿色 `Check` 图标；`abort` 用红色 `X` 图标

---

## P2 · 版本历史 + Diff 入口

### 问题
后端有完整的版本控制（`/versions`、`/versions/diff`、`/rollback`），`DiffView.tsx` 组件已存在，但 Inspector 中没有任何入口让用户查看或对比版本历史。

### 设计目标
在 WorkflowInspector header 中增加版本号入口，点击展开版本历史侧抽屉，支持查看任意版本 diff 和回滚。

### UI 规范

**Inspector header 变更**（现有版本号 badge 变为可点击）：

```
  PR Triage                         v4 ▾  [开发会话]  [触发运行]
                                    ↑
                              点击展开版本抽屉
```

**版本历史侧抽屉**（从 Inspector 右侧 slide-in）：

```
┌───────────────────────────────┐
│  版本历史                  ✕ │
├───────────────────────────────┤
│  ● v4  今天 14:32  (当前)     │
│    v3  今天 11:05  [查看差异] │
│    v2  昨天 09:44  [查看差异] │
│    v1  3 天前      [查看差异] │
├───────────────────────────────┤
│  [↺ 回滚到此版本]（v3 选中时） │
└───────────────────────────────┘
```

**Diff 视图区（选中版本后在抽屉下方展开）**：

```
  v3 → v4 的差异
  ─────────────────────────────
  (复用 DiffView 组件，unified diff 格式，
   max-h-[300px] overflow-auto，无额外包装)
```

**视觉规范**：
- 版本号入口：`text-[11px] text-faint flex items-center gap-1 cursor-pointer hover:text-muted`，尾部 `ChevronDown h-3 w-3`
- 抽屉：`absolute right-0 top-0 h-full w-64 bg-panel border-l border-line shadow-lg z-10 transition-transform`
- 版本列表行：`flex items-center gap-2 py-1.5 px-3 text-xs hover:bg-card cursor-pointer`
- 当前版本：`text-violet font-medium`，前缀圆点 `●`
- [回滚] 按钮：`btn-press border border-orange/40 text-orange hover:bg-orange/10 text-xs`，二次确认改为按钮变成「确认回滚？」状态（inline confirm pattern，无弹窗）

---

## P2 · 从失败步骤重试

### 问题
当前 `/retry` 总是从第一个节点重新执行，对于前 N-1 步成功、只有最后一步失败的长 workflow，这既浪费计算资源，又浪费 token。

### 设计目标
在 RunErrorBox 中增加「从失败步骤重试」选项，后端利用 LangGraph FileCheckpointer 的已有 checkpoint 直接从上次失败节点的入口状态恢复，创建新 run 但不重走前面的步骤。

### UI 规范

**RunErrorBox 操作区**（在现有「重试」按钮旁增加第二个重试按钮）：

```
┌──────────────────────────────────────────────────────────┐
│  ✕  失败于: 分析 PR 内容                                  │
│  API rate limit exceeded (429)                           │
├──────────────────────────────────────────────────────────┤
│  [展开详情 ▾]    [复制错误报告]                           │
│                                                           │
│  [↺ 从头重试]   [↺ 从失败步骤重试]                        │
│                  ↑ 新增，从 analyze_pr 节点恢复           │
└──────────────────────────────────────────────────────────┘
```

- 「从失败步骤重试」按钮 tooltip：`从「{node_title}」节点开始重试，跳过已完成的步骤`
- 按钮样式与「从头重试」相同，文字稍小 `text-[11px]`，可区分
- 仅在 `error_detail.node_id` 存在 AND 该节点不是第一个节点时显示此按钮
- 操作后：显示新 run 的 LiveRunBlock，旧 run card 收起

---

## P3 · Workflow 列表搜索/筛选

### UI 规范

**`/workflows` 页面左栏顶部（WorkflowsPage）**：

```
┌───────────────────────────────────┐
│  Workflows              [+ 新建]  │
├───────────────────────────────────┤
│  🔍  搜索 workflow 名称...        │
│  [全部 ▾]   [● 系统]   [● 用户]  │
├───────────────────────────────────┤
│  ▶  PR Triage              系统   │
│  ▶  TODO 同步               系统  │
│  ▶  我的工作流               用户 │
└───────────────────────────────────┘
```

- 搜索框：`rounded border border-line bg-base px-2 py-1 text-xs placeholder:text-faint`
- 过滤 tab：`text-[11px]`，「全部/系统/用户」，active 时 `text-violet border-b border-violet`
- 系统 workflow（`is_builtin=true`）右侧显示 `text-[10px] text-faint uppercase` 的「系统」标签
- 搜索匹配：workflow name + description 模糊匹配，客户端实时过滤（不需要后端支持）

---

## P3 · Context 变量提示

### UI 规范

**ContextEditor 表单顶部（当 DSL 含 `{{variable}}` 插值时）**：

```
┌──────────────────────────────────────────────────────────┐
│  运行上下文                               [重置为默认]    │
│  ─────────────────────────────────────────────────────   │
│  模板变量   pr_number ✓   repo ✓   branch ！(未填)       │
├──────────────────────────────────────────────────────────┤
│  pr_number  [123           ]                              │
│  repo       [my-org/ginno  ]                              │
│  branch     [              ]  ← 高亮边框提示必填          │
└──────────────────────────────────────────────────────────┘
```

- 变量扫描：正则 `{{([^}]+)}}` 扫描整个 DSL JSON 字符串，去重后列出
- 已填变量：`text-[11px] text-green` + `Check h-3 w-3`
- 未填变量：`text-[11px] text-yellow` + `AlertCircle h-3 w-3`
- 对应 input 高亮：`border-yellow/60 focus:ring-yellow/30`
- [触发运行] 按钮在有未填变量时变为 `border-yellow` outline 样式（不禁用，允许带空值运行）

---

## P3 · Browser 通知

### 交互逻辑
- 首次触发运行时请求 `Notification.permission`（不主动弹，仅在用户操作时请求）
- 当 `document.visibilityState !== "visible"` 且 run 状态变为 `done/failed` 时触发通知
- 通知内容：
  - done：`✓ {workflow_name} 已完成  ·  {elapsed}`
  - failed：`✕ {workflow_name} 失败于「{failed_node_title}」`
- 点击通知：`window.focus()` + 打开右栏 WorkflowPanel

---

## P3 · 批量清除增强

**WorkflowPanel 右上角清除按钮改为下拉菜单**：

```
  [清除 ▾]
    ├ 清除已完成
    ├ 清除已失败
    └ 清除全部历史（含运行中）  ← 危险操作，文字红色
```

- 「清除全部历史」需 inline confirm：文字变为「确认清除全部？」再次点击才执行
- 对应后端：`POST /api/workflow_runs/cleanup` 已有 `status` 过滤参数，传入 `["done", "failed", "cancelled"]` 即可

---

## P3 · supervisor_intervene 事件样式（WorkflowLogTimeline）

见 P2「Supervisor 面板」中的 timeline 样式规范，此处实现于 `WorkflowLogTimeline.tsx` 的 kind switch-case。

---

## P3 · Settings DSL 编辑器升级

**WorkflowsSettings 创建/编辑 workflow 时**，将原有 `<textarea>` 替换为带语法高亮的代码编辑器：
- 使用 `react-simple-code-editor` + `prismjs/components/prism-json`（轻量，无需 Monaco）
- 添加 DSL schema 校验提示：保存时调用 `POST /api/workflows/validate`（如无此端点则前端实现基础校验）
- 编辑器样式：`font-mono text-xs bg-base border border-line rounded`，行号可选

---

## P3 · 自适应 stuck 检测

**现有逻辑**：`LiveRunBlock` 固定 5 分钟无心跳显示 ⚠️ stuck 警告。

**新逻辑**：
1. 在 store 中维护 `workflowRunHistory`：按 `workflow_id` 聚合最近 10 次成功 run 的 `steps[*].duration`
2. 当前 run 的某个步骤运行时长超过「该 workflow 该步骤历史均值 × 3」时显示 stuck
3. 若无历史数据（新 workflow 或该步骤从未完成过），fallback 到 120s（原来的 5min 缩短为 2min）
4. stuck 提示文字变更：`此步骤通常在 {avg}s 内完成，已运行 {elapsed}s`

---

# Part 2 — 技术方案

---

## P0 · version_propose 确认卡

### 新增组件

**`apps/web/src/components/workflow/ProposeDiffCard.tsx`**

```typescript
// Props
interface ProposeDiffCardProps {
  runId: string
  fromVersion: number
  diff: string        // unified diff string from interrupt payload
  rationale: string
  onDecide: (decision: "allow" | "deny") => Promise<void>
  decided?: "allow" | "deny" | null
}
```

组件内部状态：`loading: boolean`，`decided: "allow"|"deny"|null`。
复用 `DiffView.tsx`，diff 区默认 `collapsed`，点击展开。

### store.tsx 变更

在 `workflowRuns` 状态中为每个 run 存储最新 interrupt payload：

```typescript
// 新增字段到 WorkflowRun（前端扩展，不改后端 schema）
latestInterrupt?: {
  kind: "version_propose" | "human" | string
  question?: string      // human 节点问题
  diff?: string          // version_propose diff
  rationale?: string
  fromVersion?: number
}
```

WS `run.event` handler 中，当 `event.kind === "interrupt"` 时更新 `latestInterrupt`：

```typescript
case "run.event":
  if (event.data?.kind === "interrupt") {
    updateRun(runId, r => ({
      ...r,
      latestInterrupt: event.data
    }))
  }
```

### RunBlocks.tsx / WorkflowPanel.tsx 变更

在 `LiveRunBlock` 渲染步骤列表后，判断插入哪种 paused UI：

```typescript
if (run.status === "paused") {
  if (run.latestInterrupt?.kind === "version_propose") {
    return <ProposeDiffCard ... />
  } else if (run.latestInterrupt?.kind === "human") {
    return <HumanInputCard ... />
  }
}
```

### API 调用

使用已有的 `POST /api/workflow_runs/{id}/decide`，参数 `{decision: "allow" | "deny"}`。

---

## P1 · HumanNode 等待态增强

### 新增组件

**`apps/web/src/components/workflow/HumanInputCard.tsx`**

```typescript
interface HumanInputCardProps {
  runId: string
  question: string | null
  onSubmit: (answer: string | null) => Promise<void>
  onCancel: () => Promise<void>
}
```

内部状态：`answer: string`，`loading: boolean`，`submitted: boolean`。

### store.tsx 变更

新增派生状态 `pendingHumanCount`：

```typescript
const pendingHumanCount = workflowRuns.filter(r =>
  r.status === "paused" &&
  r.latestInterrupt?.kind === "human"
).length
```

将此值 export 供 `RightDock.tsx` 使用。

### RightDock.tsx 变更

在现有 `activeRunCount`（蓝色 badge）旁增加 `pendingHumanCount`（黄色 badge）：

```tsx
{pendingHumanCount > 0 && (
  <span className="absolute -right-0.5 -top-0.5 inline-flex h-4 min-w-[16px]
    items-center justify-center rounded-full bg-yellow px-1
    text-[10px] font-semibold text-black animate-pulse">
    {pendingHumanCount}
  </span>
)}
```

点击 dock 时若有 `pendingHumanCount > 0`，打开右栏后自动 `scrollIntoView` 第一张等待输入的卡片。

### API 调用

- 确认：`POST /api/workflow_runs/{id}/resume` body `{answer: string}`
- 跳过：`POST /api/workflow_runs/{id}/resume` body `{answer: null, skip: true}`
- 中止：`POST /api/workflow_runs/{id}/cancel`

---

## P1 · 运行中工具调用可见性

### store.tsx 变更

新增 `liveToolActivity: Map<string, {nodeId: string, toolName: string, argsPreview: string, ts: number}>`（key = run_id）。

WS `run.event` handler 更新逻辑：

```typescript
case "run.event":
  const evt = event.data
  if (evt.kind === "tool_call") {
    const firstCall = evt.calls?.[0]
    if (firstCall) {
      liveToolActivity.set(runId, {
        nodeId: evt.node_id,
        toolName: firstCall.name ?? "",
        argsPreview: truncateArgs(firstCall.args, 50),
        ts: evt.ts ?? Date.now()
      })
    }
  } else if (evt.kind === "tool_result") {
    liveToolActivity.delete(runId)
  } else if (["node_exit", "done", "error"].includes(evt.kind)) {
    liveToolActivity.delete(runId)
  }
```

`truncateArgs(args, maxLen)` 辅助函数：将 args 对象的第一个字符串值提取后截断。

### RunBlocks.tsx 变更

在渲染当前运行步骤时，查找 `liveToolActivity.get(run.id)`，若存在且 `activity.nodeId === step.id`，在步骤行下插入工具调用行：

```tsx
{activity && activity.nodeId === step.id && (
  <div className="ml-5 flex items-center gap-1.5 text-[11px] text-faint">
    <Wrench className="h-3 w-3 shrink-0" />
    <span className="font-mono text-muted">{activity.toolName}</span>
    <span className="text-faint">· {activity.argsPreview}</span>
  </div>
)}
```

---

## P2 · Supervisor 面板

### WorkflowLogTimeline.tsx 变更

在 kind→样式的 switch 中补充 `supervisor_intervene` case：

```typescript
case "supervisor_intervene":
  return {
    icon: <ShieldAlert className="h-3.5 w-3.5 text-orange" />,
    label: "Supervisor 干预",
    color: "text-orange",
    containerClass: "rounded border border-orange/30 bg-orange/[0.05] p-2",
    detail: (evt) => (
      <div className="space-y-0.5 text-[11px] text-muted">
        <div>节点: <span className="font-mono">{evt.node_id}</span>
          · 动作: <ActionBadge action={evt.action} /></div>
        {evt.errors?.length > 0 && (
          <div className="text-faint">{evt.errors.join("; ")}</div>
        )}
        {evt.reason && <div>{evt.reason}</div>}
      </div>
    )
  }
```

### WorkflowInspector.tsx 变更

替换占位 Supervisor 面板（第 262 行附近），改为从 `events` 过滤 `supervisor_intervene` 并展示列表。`events` 数据已在 Inspector 中通过 `GET /api/workflow_runs/{id}/events` 获取，直接过滤即可，无需新接口。

```typescript
const supervisorEvents = events.filter(e => e.kind === "supervisor_intervene")
```

---

## P2 · 版本历史 + Diff

### 新增组件

**`apps/web/src/components/workflow/VersionHistoryDrawer.tsx`**

Props：`workflowId: string, currentVersion: number, open: boolean, onClose: () => void`

内部逻辑：
1. 挂载时 `GET /api/workflows/{id}/versions` 获取版本列表
2. 点击某版本时 `GET /api/workflows/{id}/versions/diff?a={selectedVersion}&b={currentVersion}`，结果传入 `DiffView`
3. 回滚：`POST /api/workflows/{id}/rollback {to: selectedVersion}`，完成后触发 `onWorkflowUpdated()`

布局：`absolute right-0 top-0 h-full w-64 bg-panel border-l border-line flex flex-col z-10`，用 `translate-x-0/translate-x-full` 控制开关动画。

### WorkflowInspector.tsx 变更

版本号区域改为：

```tsx
<button
  onClick={() => setVersionDrawerOpen(true)}
  className="flex items-center gap-1 text-[11px] text-faint hover:text-muted"
>
  v{currentVersion}
  <ChevronDown className="h-3 w-3" />
</button>
{versionDrawerOpen && (
  <VersionHistoryDrawer
    workflowId={workflow.id}
    currentVersion={currentVersion}
    open={versionDrawerOpen}
    onClose={() => setVersionDrawerOpen(false)}
  />
)}
```

---

## P2 · 从失败步骤重试

### 后端：新端点

**`POST /api/workflow_runs/{run_id}/retry_from_checkpoint`**

实现思路（`api/workflows.py`）：
1. 校验 run status 为 `failed`，且 `error_detail.node_id` 存在
2. 查找 checkpoint 文件：`_run_checkpoint_path(run_id)`
3. 若 checkpoint 不存在 → 返回 `409 {error: "no_checkpoint", message: "该 run 无可用 checkpoint，请从头重试"}`
4. 创建新 run（同 `/retry` 逻辑），新 run ID 生成
5. 将旧 run 的 checkpoint 文件复制到新 run 的 checkpoint 路径
6. 启动新 run：`_run_workflow_bg(new_run, wf_def, ...)`，engine 会读到复制的 checkpoint 并从上次暂停点恢复

关键实现细节：
- LangGraph FileCheckpointer 使用 `thread_id = run_id` 作为索引，复制文件后新 run 以 `new_run_id` 作为 `thread_id`，engine 编译图时用 `config={"configurable": {"thread_id": new_run.id}}`
- checkpoint 文件中存储的是图状态（各节点 output + context），不是 run metadata，复制后直接可用
- 如果失败发生在第一个节点，checkpoint 可能不存在或为空 → 此时 fallback 到普通 retry

### 前端：RunErrorBox.tsx 变更

```tsx
{errorDetail?.node_id && !isFirstNode(run, errorDetail.node_id) && (
  <button
    onClick={() => retryFromCheckpoint(run.id)}
    className="rounded border border-line px-2 py-1 text-xs text-muted hover:bg-card2"
    title={`从「${failedNodeTitle}」节点恢复，跳过已完成的步骤`}
  >
    <RotateCcw className="h-3 w-3 mr-1 inline" />
    从失败步骤重试
  </button>
)}
```

`isFirstNode(run, nodeId)`：检查 `run.steps[0].id === nodeId`。

---

## P3 · 技术实现摘要

| 功能 | 关键改动 | 新增文件 |
|------|----------|----------|
| 列表搜索/筛选 | `WorkflowsPage.tsx`：加 `searchQuery` state，filter `workflows` 数组 | 无 |
| Context 变量提示 | `ContextEditor.tsx`：正则扫描 DSL string，渲染变量 badge 列表 | 无 |
| Browser 通知 | `store.tsx`：run status 变化时检查 `document.visibilityState`，调用 `new Notification(...)` | 无 |
| 批量清除增强 | `WorkflowPanel.tsx`：清除按钮改下拉，传 status 参数给 cleanup API | 无 |
| supervisor 事件样式 | `WorkflowLogTimeline.tsx`：补 kind switch case | 无 |
| Settings DSL 编辑器 | `WorkflowsSettings.tsx`：引入 `react-simple-code-editor` + `prismjs` | 无 |
| 自适应 stuck 检测 | `store.tsx`：维护 `stepDurationHistory`；`RunBlocks.tsx`：动态阈值计算 | 无 |

---

## 实施顺序建议

```
Sprint 1（P0+P1，约 3-5 天）
  1. store.tsx：WS handler 增加 latestInterrupt + liveToolActivity
  2. ProposeDiffCard.tsx（P0 核心组件）
  3. HumanInputCard.tsx（P1 核心组件）
  4. RunBlocks.tsx：接入以上两个组件 + 工具调用行
  5. RightDock.tsx：pendingHumanCount badge

Sprint 2（P2，约 3-5 天）
  6. WorkflowLogTimeline.tsx：supervisor_intervene 样式
  7. WorkflowInspector.tsx：Supervisor 面板真实 UI
  8. VersionHistoryDrawer.tsx + Inspector 版本号入口
  9. 后端：retry_from_checkpoint 端点
  10. RunErrorBox.tsx：从失败步骤重试按钮

Sprint 3（P3，约 2-3 天）
  11. WorkflowsPage：搜索筛选
  12. ContextEditor：变量提示
  13. store.tsx：Browser 通知 + 自适应 stuck 阈值
  14. WorkflowPanel：批量清除下拉
  15. WorkflowsSettings：DSL 代码编辑器
```

---

# Part 1 — 从会话总结 Workflow 增强

---

## S1 · 聊天页入口 + Session 选择器 (P0)

### 问题
「总结成流程」入口在 `/workflows` 页右上角，而用户完成一次好用的对话时人在聊天页，两者之间存在一次不必要的上下文切换。此外入口永远只取「最近一条」session，历史会话无法复用。

### 设计目标
把入口移进聊天页，就在对话结束后就能触发，同时提供 session 选择下拉应对历史复用场景。

### UI 规范

**聊天页 session header 增加操作按钮**：

```
┌────────────────────────────────────────────────────────────┐
│  ← 会话标题                     [总结成流程 ▾]  [···]      │
└────────────────────────────────────────────────────────────┘
```

- 按钮：`btn-press flex items-center gap-1 rounded-md border border-line px-2 py-1 text-xs text-muted hover:bg-card2 hover:text-txt`
- 图标：`Workflow h-3 w-3`
- 右侧 `▾` 展开 session 选择器（当历史 session 数 ≥ 2 时显示；否则直接触发当前 session）

**Session 选择器下拉**（`▾` 展开后）：

```
  [总结成流程 ▾]
  ├ ● 当前会话（推荐）
  ├   2 小时前 · 调试登录问题
  ├   昨天 · PR 分析会话
  └   3 天前 · 代码重构优化
```

- 下拉容器：`absolute top-full right-0 mt-1 w-56 rounded-lg border border-line bg-card shadow-lg z-20`
- 当前会话行：前缀 `●`（`text-violet`），`text-xs font-medium`
- 历史 session 行：`text-[11px] text-muted`，最多展示最近 10 条
- session 名称超长时截断：`truncate max-w-[160px]`

**加载中状态**（触发后 LLM 调用期间）：

```
  [总结成流程 ▾]  →  [⟳ 正在总结…]   ← 按钮内 spinner，不打开弹窗
```

---

## S2 · SummarizeModal 重设计 (P0)

### 问题
现有 Modal 展示原始 `JSON.stringify(dsl, null, 2)`——200 行 JSON 在对话框里，用户无法快速判断节点结构是否正确。`WorkflowDag.tsx` 已经可以可视化 DSL，完全可以在这里复用。

### 设计目标
新版 Modal 左侧 DAG 图预览，右侧可编辑的节点摘要列表 + 上下文变量确认，底部提供 4 个操作（含「进入开发会话精炼」）。

### UI 规范

```
┌──────────────────────────────────────────────────────────────┐
│  ⟡  总结成流程                                            ✕  │
│     PR 分析流程  ·  从「当前会话」提炼 · v1 草稿              │
├────────────────────┬─────────────────────────────────────────┤
│                    │  节点 (3)                    [展开 JSON] │
│   ┌──────────┐     │  ┌───────────────────────────────────┐  │
│   │  拉取 PR │     │  │  ① 拉取 PR 列表          step    │  │
│   └────┬─────┘     │  │  目标  获取待处理 PR 并汇总…      │  │
│        │           │  ├───────────────────────────────────┤  │
│   ┌────▼─────┐     │  │  ② 分析 PR 内容          step    │  │
│   │  分析内容│     │  │  目标  深度分析代码变更和影响…     │  │
│   └────┬─────┘     │  ├───────────────────────────────────┤  │
│        │           │  │  ③ 生成评审报告          step    │  │
│   ┌────▼─────┐     │  │  目标  撰写结构化评审报告…        │  │
│   │  生成报告│     │  └───────────────────────────────────┘  │
│   └──────────┘     │                                          │
│                    │  上下文变量 (2 个已识别)                  │
│  [↺ 重新总结]      │  ┌───────────────────────────────────┐  │
│                    │  │  pr_number   string   [可选 ▾]    │  │
│                    │  │  repo        string   [可选 ▾]    │  │
│                    │  │                   [+ 手动添加变量] │  │
│                    │  └───────────────────────────────────┘  │
├────────────────────┴─────────────────────────────────────────┤
│ [取消]  [🔧 进入开发会话精炼]   [仅创建]   [▶ 创建并运行]    │
└──────────────────────────────────────────────────────────────┘
```

**视觉规范**：
- Modal 最大宽度：`max-w-3xl`（比现有 `max-w-2xl` 略宽）
- 左栏（DAG）：`w-[280px] shrink-0 border-r border-line p-4 flex flex-col gap-3`
  - 复用 `WorkflowDag` 组件，传入 `dsl`，`compact` prop 关闭点击交互
  - 下方 `[↺ 重新总结]` 按钮：`text-[11px] text-faint hover:text-muted`
- 右栏（节点列表）：`flex-1 overflow-auto p-4 space-y-3`

**节点卡片**（可编辑态）：

```
  ┌─────────────────────────────────────────────────────┐
  │  ① 拉取 PR 列表                        step  [···]  │
  │  目标  获取待处理 PR 并汇总元信息                    │
  └─────────────────────────────────────────────────────┘
  点击「目标」文字 → 变成 inline textarea（单击展开，blur 保存）
  [···] 展开菜单：重命名 / 删除节点
```

- 卡片：`rounded-md border border-line bg-card p-2.5 text-xs`
- 节点序号：`rounded bg-card2 px-1.5 py-0.5 text-[10px] font-mono text-faint`
- 类型标签：`text-[10px] uppercase text-faint`
- 目标文字：`text-muted cursor-text hover:text-txt`，编辑时 `outline-none border-b border-violet`

**上下文变量区**：
- 识别到变量：`text-[11px] font-mono text-muted`，`[可选 ▾]` 下拉可改为「必填」
- 未识别到变量：`text-[11px] text-faint italic`，显示「LLM 未识别到变量，可手动添加」
- [+ 手动添加变量] → inline 表单展开

**底部操作栏**：
- `[取消]`：普通边框按钮
- `[🔧 进入开发会话精炼]`：`border border-violet/40 text-violet hover:bg-violet/[0.06]`，图标 `Bot h-3 w-3`
- `[仅创建]`：普通边框按钮
- `[▶ 创建并运行]`：`bg-gradient-to-r from-violet to-fuchsia text-white`（与现有保持一致）

**「展开 JSON」切换**：右栏顶部小链接，点击后替换节点列表为 JSON 编辑器（保留给高级用户）。

---

## S3 · 草稿可编辑（节点名/目标/变量）(P1)

### 交互细则

**节点标题编辑**：单击标题文字进入编辑模式，blur 或 Enter 保存到本地 dsl state（不调后端），Modal 提交时携带修改后的 dsl。

**节点目标编辑**：同上，`<textarea>` 自动高度伸缩（`resize-none overflow-hidden`），`onInput` 调 `el.style.height = el.scrollHeight + "px"`。

**变量必填/可选切换**：点击 `[可选 ▾]` 下拉，选「必填」后在 context.schema 对应字段里加上 `required` 标记，`[创建并运行]` 时会校验。

**删除节点**：从 `[···]` 菜单选「删除」→ 从 `dsl.nodes` 和 `dsl.edges` 中同时移除，DAG 图实时更新（传入修改后的 dsl 重渲染 WorkflowDag）。

**`[↺ 重新总结]`**：清空当前 dsl 草稿，重新调用后端 summarize API（允许用户在发现结果很差时一键重试，不需要关闭再重开 Modal）。

---

## S4 · 后端生成质量增强 (P1)

### 三项改进，均在 `api/workflows.py`

**改进 1：工具输出智能摘要（替代硬截断）**

`_trace_text` 对 `ToolMessage.content` 的处理从「截断到 200 字符」改为：
- 内容 ≤ 400 字符：全量保留
- 内容 > 400 字符：保留前 200 字符 + `[…截断…]` + 后 100 字符（保留结尾往往比截断更有用）
- 如果 tool name 是文件读取类（`read_file`、`Read`、`cat`），只保留结果的前 3 行作为摘要（避免整个代码文件进入 trace）

**改进 2：DSL 校验失败自动重试**

生成后若 `validate_dsl(dsl)` 返回 errors，带错误信息再调一次 LLM（最多 2 次）：

```python
for attempt in range(3):
    resp = await model.ainvoke([...])
    dsl = normalize + validate
    if not errs:
        break
    # 第2/3次调用时在 trace 末尾追加校验错误信息
    trace += f"\n\n[Previous DSL had errors: {'; '.join(errs)}. Fix them.]"
```

**改进 3：Context 变量提取提示强化**

在 `_SYNTHESIZE_PROMPT` 末尾追加：
```
After writing the DSL, list all placeholders you used in goal/prompt fields
({{variable_name}} patterns) as context.schema properties with type:"string"
and a matching initial value of "" so the caller can fill them before running.
```

---

## S5 · Session 选择器 + 消息范围标注 (P2)

### Session 选择器

**触发入口**：聊天页 header 的 `▾` 展开后，session 列表从 `GET /api/sessions` 获取（若无此端点则从 store 的 session 列表读取）。列表显示：session 名称 + 相对时间 + 消息数量。

### 消息范围标注（轻量版）

在 SummarizeModal 触发前增加一个可选步骤「选择范围」：
- 默认：全部消息（「全量」）
- 可选：「仅最近 N 轮对话」（滑块或下拉，选 5/10/20/全部）
- 后端：`_trace_text` 接受 `last_n: int | None` 参数，截取 `messages[-last_n:]`

不做精确的消息起止标注（成本高、使用频率低），滑块版本可覆盖 80% 的实际需求。

---

## S6 · 草稿持久化 (P3)

用户关闭 Modal 后草稿（含编辑内容）存入 `localStorage["summarize_draft"]`，下次打开 Modal 时检查是否有未保存草稿，提示「恢复上次草稿」或「重新总结」。

草稿结构：`{dsl, sourceSessionId, savedAt: timestamp}`，超过 24h 自动丢弃。

---

# Part 2 — 从会话总结 Workflow：技术方案

---

## S1 · 聊天页入口 + Session 选择器

### 前端改动

**触发位置**：聊天页 session header 组件（当前项目中确认路径后插入，搜索 `session` + `header` 相关组件）。

新增 `SummarizeButton` 组件：

```tsx
// 无历史 session 时直接触发，有时展示下拉
function SummarizeButton({ currentSessionId }: { currentSessionId: string }) {
  const [open, setOpen] = useState(false)
  const recentSessions = useRecentSessions(10) // 从 store 读取

  const trigger = (sessionId: string) => {
    setOpen(false)
    onSummarize(sessionId)  // 调用现有 summarizeSessionToDsl
  }

  if (recentSessions.length <= 1) {
    return <button onClick={() => trigger(currentSessionId)}>...</button>
  }
  return (
    <div className="relative">
      <button onClick={() => setOpen(v => !v)}>总结成流程 <ChevronDown /></button>
      {open && <SessionDropdown sessions={recentSessions} onSelect={trigger} />}
    </div>
  )
}
```

**SummarizeModal 改动**（`SummarizeModal.tsx` 重写）：

新增 props：
```typescript
interface SummarizeModalProps {
  dsl: Record<string, unknown>    // 原有
  busy?: "create" | "run" | null  // 原有
  error?: string | null            // 原有
  onClose: () => void              // 原有
  onCreate: (run: boolean, editedDsl: Record<string, unknown>) => void  // 携带编辑后的 dsl
  onRetry: () => void              // 新增：重新总结
  onOpenDevSession: (wfId: string) => void  // 新增：创建后打开开发会话
}
```

内部 state：
```typescript
const [localDsl, setLocalDsl] = useState(dsl)  // 用于编辑
const [showJson, setShowJson] = useState(false)  // JSON 视图切换
```

节点编辑：在 localDsl.nodes 上做 immer 式更新，WorkflowDag 接收 localDsl 实时重渲染。

### API 调用路径不变
仍调用 `api.summarizeSessionToDsl(sessionId)`，返回 dsl 后传入新 Modal。

---

## S4 · 后端生成质量增强

### `api/workflows.py` 修改

**`_trace_text` 函数改进**：

```python
def _trace_text(messages, last_n: int | None = None) -> str:
    msgs = messages or []
    if last_n:
        msgs = msgs[-last_n:]
    
    FILE_READ_TOOLS = {"read_file", "Read", "cat", "head", "tail"}
    
    lines: list[str] = []
    for m in msgs:
        if isinstance(m, HumanMessage):
            lines.append(f"USER: {_content_str(m)[:500]}")
        elif isinstance(m, AIMessage):
            c = _content_str(m)
            if c.strip():
                lines.append(f"AGENT: {c[:500]}")
            for tc in getattr(m, "tool_calls", None) or []:
                lines.append(f"  -> {tc.get('name')}({json.dumps(tc.get('args') or {}, ensure_ascii=False)[:200]})")
        elif isinstance(m, ToolMessage):
            c = _content_str(m)
            tool_name = getattr(m, "name", "tool") or "tool"
            # 文件读取类工具：只保留前 3 行
            if tool_name in FILE_READ_TOOLS:
                preview = "\n".join(c.splitlines()[:3])
                lines.append(f"  <= {tool_name}: {preview}… [file content omitted]")
            elif len(c) > 400:
                # 保留首尾，比纯截断更有信息量
                lines.append(f"  <= {tool_name}: {c[:200]}…[…]…{c[-100:]}")
            else:
                lines.append(f"  <= {tool_name}: {c}")
    return "\n".join(lines)
```

**DSL 校验重试循环**：

```python
trace = _trace_text(messages, last_n=data.get("last_n"))
extra_hint = ""
dsl = None
for attempt in range(3):
    resp = await model.ainvoke([
        SystemMessage(content=_SYNTHESIZE_PROMPT),
        HumanMessage(content=trace + extra_hint)
    ])
    raw = resp.content if isinstance(resp.content, str) else str(resp.content)
    dsl = _extract_json_obj(raw)
    if not isinstance(dsl, dict):
        extra_hint = f"\n\n[Attempt {attempt+1} error: response was not a JSON object. Reply ONLY with {{...}}]"
        continue
    dsl = wf_dsl.normalize_dsl(dsl)
    errs = wf_dsl.validate_dsl(dsl)
    if not errs:
        break
    extra_hint = f"\n\n[Attempt {attempt+1} DSL errors: {'; '.join(errs)}. Fix and return corrected DSL only.]"
```

**`_SYNTHESIZE_PROMPT` 末尾追加变量提取提示**（在现有字符串末尾）：

```python
_SYNTHESIZE_PROMPT = (
    ...  # 现有内容不变
    "After writing the DSL, ensure all {{variable_name}} placeholders used in "
    "goal or prompt fields appear as properties in context.schema with "
    'type:"string" and an empty string as context.initial value, '
    "so the caller can fill them before running."
)
```

**接受 `last_n` 参数**：在 `summarize_session_to_dsl` 函数中读取 `data.get("last_n")`，传给 `_trace_text`。

---

## S5 · Session 选择器（前端）

`runtime.ts` 已有 session 列表接口（或可从 store 读取），`SummarizeButton` 直接消费，无需新增后端接口。

若 store 中没有 session 列表，添加：
```typescript
// lib/runtime.ts
export const listSessions = () => get<{sessions: Session[]}>(`${BASE}/sessions`)
```

---

## S6 · 草稿持久化

在 SummarizeModal 的 `onClose` 回调中：
```typescript
if (!submitted) {
  localStorage.setItem("summarize_draft", JSON.stringify({
    dsl: localDsl,
    sourceSessionId,
    savedAt: Date.now()
  }))
}
```

下次触发 summarize 时，若 `localStorage["summarize_draft"]` 存在且 `savedAt` 在 24h 内，展示 banner：
```
⟳ 有一份 {相对时间} 前的未保存草稿  [恢复]  [忽略，重新总结]
```

---

## 更新后的实施顺序

```
Sprint 1（P0+P1，约 3-5 天）
  ── 原有 1-5 项不变 ──
  + S1a. 聊天页 header 增加 SummarizeButton 入口
  + S2.  SummarizeModal 重写（DAG 预览 + 可编辑节点）
  + S4a. 后端 _trace_text 改进（智能截断）
  + S4b. 后端 DSL 生成重试循环

Sprint 2（P2，约 3-5 天）
  ── 原有 6-10 项不变 ──
  + S3.  节点/变量编辑细节打磨（inline edit + 变量必填切换）
  + S4c. _SYNTHESIZE_PROMPT 变量提取提示强化

Sprint 3（P3，约 2-3 天）
  ── 原有 11-15 项不变 ──
  + S5.  Session 选择器下拉（历史会话复用）
  + S1b. last_n 消息范围参数（后端 + 前端滑块）
  + S6.  草稿 localStorage 持久化
```






