# 定时任务（Scheduled Tasks）设计

> 状态：**P0 已实现**（2026-10-02，多 agent 实现 + 验证；runtime 单测 46 例 / 全量
> 1523+286 无回归 / web tsc+build / cargo check 通过）。实现遗留（不阻塞，待拍板）：
> ① workflow 目标的 LLM 摘要回退全局默认 provider（fork agent 随 run 结束即删，取不到
> 「任务同款模型」）；② Tauri `get_keep_awake` 已实现但 web 未消费（UI 以 runtime
> `keep_awake` 真值回显，用户手动杀 caffeinate 时 UI 无感知）；③ 执行记录「近 30 天」
> 视图前端上限拉 300 行（6 页兜底）。已拍板决议：
> ① 错过只记 missed 不补跑；② 间隔下限 5 分钟；③ keep_awake 只挡系统睡眠
> （`caffeinate -i`，屏幕可暗可锁）；④ workflow 输入用 **schema 驱动表单**（数据源
> DSL `context.schema`，直接进 P0）；⑤ 影子会话**不进**全局搜索；⑥ 摘要用 **LLM 生
> 成**（一次 ≤160 字廉价调用，失败回退纯截断）；⑦ Workflows 页运行列表给定时触发的
> run 加「⏰ 定时」徽标。目标：给 Ginno 增加本地定时任务能力——按间隔/每日/每周/单次
> 计划自动执行 **prompt（影子会话）或 workflow（headless run）** 两种目标，入口放在
> 侧边栏左下角。包含：保持机器唤醒（防闲置睡眠）、任务级与全局开关、当天时间条（已
> 执行时段 hover 简介、点击跳转对话回放）、执行记录列表（按时间排序、可跳转对话回
> 放）。

## 0. TL;DR

- **入口**：侧边栏 footer nav（Connectors/KB/Workflows/Settings 之上）新增「定时任务」
  （icon: `AlarmClock`），路由 `/scheduled`（独立页，与 /connectors 同构）。
- **调度器**：runtime 内新增单例 asyncio 调度循环（30s tick，模式对齐 Goal driver
  loop），无 APScheduler/cron 库。计划类型 P0 只做四种预置：间隔 / 每日 / 每周 / 单次。
- **执行目标（target）二选一**：
  - **prompt**：每次触发新建一个**影子会话**（真实 session 文件，meta 标
    `type:"scheduled"` + `schedule_run_id`，**会话列表/搜索不显示**），headless 跑一轮
    `_run_stream`（范本：`_run_goal_turn`），`usage_source:"schedule"`。
  - **workflow**：每次触发走既有 `POST /api/workflow_runs` 的 headless 形态（不绑会
    话，`context_override` 携带表单保存的输入），复用 workflow run 全套生命周期与回放
    视图（`RunSubSessionView` 本就支持无父会话的 run）。输入用 **schema 驱动表单**
    （DSL `context.schema` 渲染、`context.initial` 给默认值，P0）。
- **执行记录**：`~/.ginno/schedule-runs/runs-YYYY-MM-DD.jsonl` 追加式日志（一行一次
  状态，同 run_id 末行胜出），完全对齐 `usage_store.py` 的既有模式。
- **保持唤醒**：Tauri 壳新增 `set_keep_awake` command，macOS 用常驻
  `caffeinate -i` 子进程实现（随壳退出自动释放）；只挡**闲置系统睡眠**（屏幕可暗可
  锁），防不了合盖/手动睡眠——UI 文案必须诚实说明。
- **对话回放**：prompt 执行复用 `RunSubSessionView` 的挂载范式——store 新增
  `activeScheduleRunId`，AppShell 主区域全屏渲染只读 transcript；workflow 执行直接用
  现有 `activeRunId` 打开 run 视图。均不离开工作区。
- **分期**：P0 = 本需求全部六条（双目标 + schema 输入表单一并落地）；P1 = 桌面通
  知、错过补跑、cron 表达式；P2 = 系统级定时唤醒（pmset）、Windows 支持。

---

## 1. 背景与目标

### 1.1 现状盘点（代码事实）

| 现状 | 位置 | 对本设计的影响 |
|---|---|---|
| 侧边栏 footer nav：Connectors / KB / Workflows / Settings 四个 Link + 版权 | `AppShell.tsx:764-789` | 入口直接在此加一项，含状态点的模式（connectorDot）可参考 |
| **无任何 cron/APScheduler/wall-clock 调度**（全 runtime + desktop grep 确认） | — | 调度器从零建，但有三处范本 |
| Goal driver loop：per-session `while True` + `asyncio.sleep` 轮询、task 注册表、lifespan 统一 cancel | `api/sessions.py:331`、`server.py:151-168` | 调度循环照此模式写，shutdown 契约一致 |
| 程序化 headless turn 范本：`_run_goal_turn`（构造 config + `_run_stream(None, graph, ...)` + `usage_source` 打标 + `_RUNNING_TURNS` 早置） | `api/sessions.py:268-318` | 定时触发直接抄这个协议，`_turn_lock`/busy 检查一并复用 |
| Session 创建 `uuid4().hex`、meta upsert 到 `sessions/_index.json` | `api/sessions.py:544-553`、`session_meta.py:27` | 影子会话用同一机制，meta 加标记字段 |
| 会话列表渲染与隐藏逻辑：`type:"subagent"` 嵌套到父行；无 `/session/[id]` 路由，打开 = `setActiveSession` | `AppShell.tsx:251-630`、`store.tsx` | `type:"scheduled"` 按"整类排除"处理，比 subagent 更简单 |
| RunSubSessionView：`activeRunId` 驱动，主区域全屏回看、工作区 `hidden` 不卸载 | `chat/RunSubSessionView.tsx`、`AppShell.tsx:798-815` | 影子会话回放照抄挂载骨架 |
| workflow headless 触发：`POST /api/workflow_runs` 的 `session_id`/`present_in_session_id` 可空，`context_override` 即输入；run 有独立生命周期/事件/回放（`wf_store.create_run` + `_spawn_run_task`） | `api/workflows.py:1425-1447,1209` | 定时跑 workflow 不需要影子会话——直接造无父会话的 run，回放走现有 run 视图 |
| workflow DSL 顶层 `context: {schema, initial}`（JSON Schema properties/required + 默认值，校验在 `validate_dsl`）；`context_override` 在 engine 里 merge over `initial` | `workflows/dsl.py:289-299,441-448`、`workflows/engine.py:187-189` | schema 驱动输入表单有真数据源：`schema` 渲染表单、`initial` 给默认、保存值落 `context_override`；无 context 定义的 workflow 隐藏表单 |
| 会话历史 REST：`GET /api/sessions/{id}/history` 返回 `{messages, last_error}` | `runtime.ts:123-137` | 回放视图的数据源，无需新接口 |
| usage 追加式日志：按天 jsonl、never raises、(path,mtime,size) 缓存、启动清理 | `usage_store.py` | 执行记录存储的模板；source 枚举可扩 `"schedule"` |
| Tauri 壳：有通知（notify-rust）、托盘、快捷键；**无任何电源/睡眠代码**；tauri.conf.json 无 plugins 段 | `apps/desktop/src/lib.rs:612-653,1338-1378` | keep-awake 从零建；通知链路（`ginno:notify`）P1 可复用 |
| 无日期库（原生 Date）；无通用时间轴组件；usage 有手写 SVG 图表原语（`useTip` tooltip、堆叠柱） | `settings/usage/charts.tsx` | 时间条手写 SVG，tooltip 复用 useTip 模式 |

### 1.2 目标（对应用户六条诉求）

1. **保持唤醒**：定时任务只在机器唤醒且 Ginno 运行时才会触发——提供「保持唤醒」开关
   （防闲置睡眠），并明确告知能力边界。
2. **开关**：全局总开关 + 单任务开关，两层独立。
3. **当天时间条**：24h 横条展示当天会执行的任务；已执行的画成时段色块，hover 出简介，
   点击跳到该次执行的对话回放。
4. **执行记录**：可查看历史执行；每条可按「主对话的模式」查看对话内容，但对应会话
   **不出现在会话列表**（影子会话）。
5. **排序**：执行记录按时间升降序。
6. **双目标**：任务既能定时跑 prompt（单轮对话），也能定时跑 workflow（既有配方的
   headless run）——同一套调度、时间条、执行记录与开关体系，产品上是一个功能。

### 1.3 非目标（本期不做）

- cron 表达式输入（P0 只有四种预置计划；表达式 P1）。
- 错过自动补跑（已决议：missed 只记账不补执行；补跑策略 P1 再议）。
- 系统级定时唤醒（`pmset repeat` 让睡眠中的机器醒来，P2）。
- Windows / Linux 的 keep-awake（当前 bundle 仅 macOS；Windows 用
  `SetThreadExecutionState`，P2）。
- 跨设备同步 / 云端调度——纯本地单机。
- 定时任务之间的依赖编排（那是 workflow 的领域，本功能不越界）。

---

## 2. 用户与场景

Ginno 是单机个人 Agent，用户即机主。核心场景：

| # | 场景 | 用户问题 | 落点 |
|---|---|---|---|
| S1 | 例行巡检 | 「每天早上 9 点把昨天的日志错误汇总给我」 | 任务列表 + 每日计划 |
| S2 | 间隔轮询 | 「每 30 分钟抓一次 PR 状态，有变化就记录」 | 间隔计划 |
| S3 | 一把梭验证 | 「任务配得对不对？现在跑一次看看」 | 任务卡「立即执行」→ 影子会话回放 |
| S4 | 盘点当天 | 「今天排了几个任务？都跑了吗？凌晨那次为什么失败？」 | 时间条 + 执行记录 |
| S5 | 回看结果 | 「早上那次巡检到底说了什么？」 | 执行记录 → 点击进对话回放 |
| S6 | 隔夜挂机 | 「想让任务整夜可触发，别让机器睡了」 | 保持唤醒开关 |
| S7 | 定时跑流程 | 「每晚 2 点跑一遍『仓库体检』workflow，早上看报告」 | workflow 目标任务 → run 回放 |

---

## 3. 产品方案

### 3.1 入口与信息架构

- 侧边栏 footer nav 新增（Connectors 之下、KB 之上）：`定时任务`（icon `AlarmClock`），
  路由 `/scheduled`。行尾状态点沿用 connectorDot 模式：**红** = 最近一次执行失败或存在
  missed；**灰** = 全局关闭；无点 = 正常。
- 页面自上而下三区：**头部（总开关 + 保持唤醒 + 下次执行）→ 当天时间条 → 页签
  （任务 / 执行记录）**。

```
┌────────────────────────────────────────────────────────────────────────┐
│ ⏰ 定时任务      [总开关 ●ON]   [保持唤醒 ●ON]   下次: 09:30 日志巡检(6h后) │
│                                                                        │
│  今天 · 2026-10-02 ‹ ›                    ■ok  ■error  ▨running  □计划  │
│  0        4        8        12       16       20       24              │
│  ├────────┼────────┼────────┼────────┼───────┼────────┤ ← 现在(14:32)  │
│  │        ▓▓▓      │   ▓▓▓▓▓▓ │ ▨▨          │      □  │               │
│  │        09:30    │  11:00▔12:14          │    21:00│               │
│                                                                        │
│  [ 任务 | 执行记录 ]                                                    │
│  …（见 §3.2 / §3.5）                                                    │
└────────────────────────────────────────────────────────────────────────┘
```

### 3.2 任务管理

任务列表 = 卡片行，按 `next_run_at` 升序（无下次的排后）：

```
┌────────────────────────────────────────────────────────────────────────┐
│  [+ 新建任务]                                                           │
│  💬 日志巡检   每天 09:30 · agent: dev · 项目: ginno      [●] [▶立即执行]│
│     上次: 今天 09:30 ✓ 3m42s · 下次: 明天 09:30            [编辑] [删除]│
│  💬 PR 状态   每 30 分钟 · agent: dev · 项目: ginno        [●] [▶]      │
│     上次: 14:02 ✓ · 下次: 14:32                                       │
│  ⚡ 仓库体检   每天 02:00 · workflow: 仓库体检 v3          [●] [▶]      │
│     上次: 今天 02:00 ✓ 6m10s · 下次: 明天 02:00                        │
│  📝 周报草稿  每周五 18:00 · workflow: 周报(已删除)        [○]          │
│     从未执行 · 已暂停 · 配方已删除，请编辑重选                          │
└────────────────────────────────────────────────────────────────────────┘
```

- 行首图标区分目标类型：💬 prompt / ⚡ workflow；行的计划、agent/项目（prompt）或
  workflow 名（workflow）随类型展示。
- **行内开关 `[●]/[○]`**：单任务启停；**总开关**关时所有行降透明度 + 显示
  「已暂停（全局关闭）」，行内开关不可点。
- **新建/编辑**：模态（对齐 `GoalEditor` 交互），第一项就是**目标类型**分段控件
  `[ 💬 对话 | ⚡ Workflow ]`，其余字段随之切换：
  - 名称（列表展示用）
  - **prompt 目标**：提示词（textarea，即影子会话里那条 user 消息）+ Agent（下拉，
    默认当前会话 agent）+ 项目（下拉，默认当前项目）
  - **workflow 目标**：workflow（下拉，来自 `g.workflows`，显示名称与版本）+ 输入
    （**schema 驱动表单**：按配方 `context.schema` 的 properties/required 渲染，
    `context.initial` 预填默认值，保存值落 `context_override`；无 context 定义的
    workflow 折叠隐藏）。必填缺失时任务不可保存（前端校验 + runtime PATCH 再校），
    配方后续被改出必填项时到点记 `error("missing_input")`。配方被删除后下拉回退显
    示「已删除」，任务可保存但触发时报 `error("workflow_missing")`
  - 计划：`间隔(每 N 分钟/小时) | 每日 HH:MM | 每周 周X HH:MM | 单次 日期+时间`，
    单选分段控件 + 对应输入；**间隔下限 5 分钟**（前端与 API 双侧校验）；单次执行完
    自动置为已暂停
  - 通知（P1，默认关）
- **删除**：ConfirmModal 二次确认；删除任务**不删**其影子会话与执行记录（账单性质
  数据不销毁，对齐 usage 的产品决策），记录里任务名冗余存储所以仍可读。
- **立即执行 `[▶]`**：不计入计划（执行记录 `trigger: "manual"`），用于验证任务配置。

### 3.3 保持唤醒（Keep Awake）

- **语义**：阻止**闲置系统睡眠**（`caffeinate -i`）。屏幕可以变暗/锁定，系统不睡；
  **不能**阻止：合盖睡眠、手动睡眠、电池耗尽强制睡眠。Ginno（壳+runtime）本身也必须
  在运行——托盘常驻即可。
- **实现**：Tauri command `set_keep_awake(enabled: bool)`。开启 = Rust spawn 并持有
  `/usr/bin/caffeinate -i` 子进程；关闭 = kill；壳退出/崩溃时子进程随父自动回收，
  无泄漏。状态查询 `get_keep_awake()`（返回是否活跃，供 UI 回显）。
- **真值与持久化**：runtime 的 `schedules.json` 里 `keep_awake` 字段是唯一真值。web 启
  动加载 `/scheduled` 数据后 invoke `set_keep_awake` 同步一次；用户切换时先 PUT
  runtime 再 invoke 壳。壳重启而 runtime 未动时由 web 重新同步，冷启动窗口期（web 未
  加载完）可能短暂无断言——可接受，不做 Rust 侧二次持久化。
- **UI**：头部开关三态文案——`保持唤醒 ●`（附小字「防止闲置睡眠；屏幕可能变暗，合
  盖仍会睡眠」）/ `保持唤醒 ○`。开关与定时任务功能本身解耦（用户可以只为防睡眠而
  开）。
- **联动提示（不强制）**：新建第一个任务或打开总开关时，若 keep_awake 为关，头部出现
  一条 inline 提示「建议开启保持唤醒，否则睡眠期间任务会错过 → [开启]」，可关掉。

### 3.4 当天时间条

24 小时横条（本地时区），自绘 SVG（`viewBox` 等比，0/4/8/12/16/20/24 刻度）：

- **已执行时段**：`started_at → finished_at` 画实心圆角条，色按状态：ok=青绿、error=红、
  skipped_overlap=灰、missed=描边空心（missed 无时长，画在 `scheduled_at` 的短刻度）。
  **进行中**：青绿 + 呼吸动画（右端开放式，延伸到「现在」线）。
- **计划刻度**：当天尚未到达的触发点（enabled 任务 + 全局开）画空心小方块；hover 显
  示任务名与计划时间。
- **重叠车道**：同时段多个执行上下错开成 2–3 条 lane（按 run 排序依次分配最小可用
  lane），不做时间轴缩放。
- **现在线**：当天视图画一条竖线 + 时间小字，每分钟移动。
- **日期切换**：`‹ 今天 ›`（默认今天；可回看历史日。未来日只显示计划刻度）。
- **hover**（复用 usage `useTip` 模式）：任务名（含 💬/⚡ 目标图标）、状态、
  `开始–结束（时长）`、摘要前两行（`summary` 字段 = LLM 生成的 ≤160 字摘要，见
  §3.7；missed 显示「错过（机器睡眠或应用未运行）」）。
- **点击**（仅 ok/error/running 的执行段）：按目标类型分流——prompt 执行打开影子会话
  回放（§3.6），workflow 执行用现有 `g.openRunView(workflow_run_id)` 打开 run 视图。
  计划刻度与 missed 刻度不可点。

### 3.5 执行记录

页签二，一行 = 一次执行（含 manual/missed/skipped_overlap）：

```
┌────────────────────────────────────────────────────────────────────────┐
│  排序: [时间 ↓]  任务: [全部 ▾]  目标: [全部 ▾]  状态: [全部 ▾] 日期:[近7天▾]│
│                                                                        │
│  时间              任务        目标     触发    状态   耗时    摘要        │
│  今天 14:02       PR 状态     💬       计划    ✓     1m12s  3 个 PR…     │
│  今天 09:30       日志巡检    💬       计划    ✓     3m42s  昨夜新增…    │
│  今天 02:00       仓库体检    ⚡       计划    ✓     6m10s  体检通过…    │
│  今天 06:00       数据备份    ⚡       计划    ◌错过  —      睡眠/未运行   │
│  昨天 15:20       日志巡检    💬       手动    ✓     3m51s  …            │
└────────────────────────────────────────────────────────────────────────┘
```

- **排序**：时间升/降序切换（默认降序）——即需求 5 的「简单按时间排序」，不做多列排序。
- **过滤**：任务、目标类型（💬/⚡）、状态（ok/error/missed/skipped/running）、日期范
  围（近 1/7/30 天）。时间条点击某天 → 此页签带日期过滤跳入。
- **行点击** → 按目标分流进回放（同 §3.4 点击）。missed 行不可点（无会话/无 run）。
- **分页** 50 行/页，与请求日志一致。
- 数据只显示元数据与摘要；对话内容进回放视图才拉取。

### 3.6 对话回放（两种目标，两条路径）

需求 4 的「按主对话的模式看、但不出现在会话列表」只作用于 **prompt 目标**：

- **prompt 目标（影子会话）**，复用 `RunSubSessionView` 的挂载范式：
  - **不出现在列表**：影子会话 meta 标 `type:"scheduled"`。`AppShell` 会话分组与
    `SessionSearchModal` 均整类排除（比 subagent 的嵌套逻辑更简单的过滤）。
  - **主对话模式查看**：store 新增 `activeScheduleRunId`，AppShell 主区域按
    `activeRunId` 同款方式挂载新组件 `ScheduleRunView`（工作区 `hidden` 不卸载，回看
    期间 WS/草稿 ref 存活，返回无缝）：
    - 顶栏对齐 `SubagentTopBar` 版式：⏰ 任务名 · 状态 pill · 计划时间/实际开始 ·
      耗时 · tokens · 「返回」（回 `activeScheduleRunId` 之前的会话）。
    - 主体：`GET /api/sessions/{session_id}/history` 拉全量消息，用 ChatStream 现有
      消息渲染组件（blocks/streamCards）以**只读 transcript** 呈现，无 composer、无
      WS。实现路径二选一（实现期定）：① 从 ChatStream 抽出纯渲染的 transcript 子组
      件复用；② 给 ChatStream 加 `transcriptOnly` 模式 prop。倾向 ①，避免只读场景
      背上 engine/WS 复杂度。
    - 回放是只读的——不能在影子会话里继续对话（想续问就「转入正式会话」：P1 提供一
      键 fork 为普通会话，P0 不做）。
- **workflow 目标**：零新视图——`g.openRunView(workflow_run_id)` 进现有
  `RunSubSessionView`（run observer + 事件流 + 节点详情本就是「主对话式」回放，且无
  父会话时返回按钮自动禁用）。执行记录/时间条只需携带 `workflow_run_id`。调度器创建
  的 run 打 `origin:"schedule"` 标记，Workflows 页运行列表据此显示「⏰ 定时」徽标
  （与手动/会话内运行区分）。

### 3.7 调度语义

- **循环**：runtime lifespan 启动单例 `_scheduler_loop`（`asyncio.create_task`，30s
  tick，随 lifespan cancel——与 Goal driver/`_connect_mcp_background` 同契约）。
- **触发**：tick 时对每个 enabled 任务比较 `now >= next_run_at`；命中且
  `now - next_run_at ≤ 60s`（GRACE）则触发，否则记 missed（覆盖睡眠/停机期间错过的点，
  每个错过的计划点记一条，跳过到未来）。
- **执行（按目标分流）**：
  - **prompt**：新建影子会话 → headless `_run_stream`（协议同 `_run_goal_turn`：构
    造 config 带 `thread_id/project_slug/agent_id/turn_id/usage_source:"schedule"`，
    过 `_turn_lock` 与 busy 检查）。
  - **workflow**：`wf_store.get_def(workflow_id)` 校验存在 → `wf_store.create_run(wf,
    session_id=None, present_in_session_id=None, context_override=…, origin="schedule")`
    + `_spawn_run_task(_run_workflow_bg(...))`（即 `POST /api/workflow_runs` 的 headless
    内联版，多带一个 `origin` 标记）；调度器订阅 run 终态（轮询 `_WF_RUN_TASKS`/
    `wf_store.get_run`，5s 步进）回写执行记录。
  - **摘要（两种目标一致）**：执行成功后用任务同款模型做**一次 ≤160 字的廉价摘要调
    用**（prompt 目标输入 = 末条助手消息；workflow 目标输入 = run 最终输出；无输出则
    跳过、`summary` 落状态文案）。该调用 fire-and-forget，不阻塞 `next_run_at` 推进；
    **失败回退纯截断**（输入文本前 160 字），摘要永远不缺席。调用计入 usage
    （`source:"schedule"`）。
  - 执行结束把 `summary` 与 tokens 回写执行记录（同 run_id 追加终态行）。
- **并发**：任务间允许并行；**同任务不重叠**——触发时上一次仍在跑则本次记
  `skipped_overlap`（间隔类任务防雪崩；workflow 常跑几分钟，这条对 ⚡ 目标尤其重
  要）。
- **对账**：启动时（lifespan）扫当日 jsonl 中仍为 `running` 的孤儿行（上次崩溃/被杀）
  改记 `error("interrupted")`，模式同孤儿 subagent/run 对账。
- **时区**：全部本地时区；`next_run_at` 持久化（epoch 秒），DST 边界按本地日历语义
  重算（每日 09:30 在 DST 切换日仍为本地 09:30）。

---

## 4. 数据设计

延续「无数据库、文件即状态」。

### 4.1 任务定义：`~/.ginno/schedules.json`

```jsonc
{
  "enabled": true,          // 全局总开关
  "keep_awake": false,      // 保持唤醒真值（web 据此同步 Tauri 壳）
  "tasks": [
    {
      "id": "st-a1b2c3",
      "name": "日志巡检",
      "target": {
        "type": "prompt",
        "prompt": "汇总 ~/.ginno/logs/sidecar.log 过去 24h 的 ERROR…",
        "agent_id": "dev",
        "project_slug": "ginno"
        // 或 {"type":"workflow","workflow_id":"wf-…","context_override":{"repo":"ginno"}}
      },
      "schedule": {
        "kind": "daily", "at": "09:30"
        // 或 {"kind":"interval","minutes":30}
        // 或 {"kind":"weekly","weekday":1,"at":"18:00"}   // 0=周日
        // 或 {"kind":"once","at":"2026-10-05T18:00:00"}   // 本地时间
      },
      "enabled": true,
      "notify": false,          // P1
      "created": 1759300000, "updated": 1759300000,
      "next_run_at": 1759395000,           // 调度器回写，UI 冷启动即有值
      "last_run": {"run_id":"sr-…","status":"ok","started":…,"finished":…}  // 冗余展示字段
    }
  ]
}
```

原子写（temp+rename），读侧容错（坏文件按空配置起，不崩 runtime）。

### 4.2 执行记录：`~/.ginno/schedule-runs/runs-YYYY-MM-DD.jsonl`

一次执行可产生两行（running → 终态），**同 run_id 末行胜出**：

```jsonc
{"run_id":"sr-x1y2","task_id":"st-a1b2c3","task_name":"日志巡检",   // 任务名冗余，任务删除后仍可读
 "target_type":"prompt|workflow","trigger":"schedule|manual","status":"running",
 "scheduled_at":1759395000,"started_at":1759395001,"finished_at":null,
 "session_id":"9f2c…",              // prompt 目标：影子会话；workflow 目标：null
 "workflow_id":"wf-…","workflow_run_id":"wr-…",   // workflow 目标；prompt 为 null
 "summary":null,"error":null,"input_tokens":null,"output_tokens":null}
{"run_id":"sr-x1y2", …,"status":"ok","finished_at":1759395223,
 "summary":"昨夜新增 12 条 ERROR，集中在 browser/relay…","input_tokens":18400,"output_tokens":1300}
```

- 按天分文件 + 90 天保留清理，逐行 append、never raises、坏行跳过、(path,mtime,size)
  读缓存——全部对齐 `usage_store.py`。
- **回放数据解耦**：prompt 目标的影子会话在项目 sessions 目录、workflow 目标的 run
  在 wf_store——执行记录只存指针（`session_id` / `workflow_run_id`），任一被删后记录
  仍在，回放视图给「已删除」空态（同 RunSubSessionView 的既有行为）。

### 4.3 影子会话 meta（`sessions/_index.json` 内该会话条目）

```jsonc
{ "id":"9f2c…", "type":"scheduled", "schedule_run_id":"sr-x1y2",
  "agent_id":"dev", "provider":…, "model":…, "created":…, "updated":… }
```

前端 `SessionMeta.type` 联合类型扩 `"scheduled"`。

### 4.4 与用量统计的关系

prompt 目标：`usage_source` 枚举新增 `"schedule"`（manual 触发与摘要调用同此
source）。workflow 目标：维持既有 `source:"workflow"`（运行节点/合成的采集已接线），
不重复打标——归因到「定时」靠执行记录的 `workflow_run_id` 关联；其**摘要调用**单独记
`source:"schedule"`。两类执行的模型调用都自然进入 usage 日志与统计页，可按来源过滤
——不新增采集点。

---

## 5. API 设计

### 5.1 runtime（FastAPI，前缀 `/api/schedule`）

| 接口 | 说明 |
|---|---|
| `GET /api/schedule` | `{enabled, keep_awake, tasks:[…]}`（含 next_run_at） |
| `PUT /api/schedule` | 整体更新 `enabled/keep_awake`（任务走下面的粒度接口） |
| `POST /api/schedule/tasks` | 新建，body 为任务对象（无 id/next_run_at），返回补全后对象；间隔 <5 分钟、workflow 必填输入缺失 → 400 |
| `PATCH /api/schedule/tasks/{id}` | 改名/改计划/启停等部分更新；enabled 翻转即重算 next_run_at |
| `DELETE /api/schedule/tasks/{id}` | 删除；不动执行记录与会话 |
| `POST /api/schedule/tasks/{id}/run` | 立即执行（manual，按 target 分流），返回 run_id |
| `GET /api/schedule/runs?date=&task_id=&target_type=&status=&trigger=&sort=asc|desc&page=` | 执行记录分页（同 run_id 去重） |
| `GET /api/schedule/timeline?date=YYYY-MM-DD` | `{runs:[…], planned:[{task_id,task_name,at}], }`——已执行 + 当天剩余计划点 |
| `WS /api/ws/schedule` | 事件：`run_started` / `run_finished` / `task_updated` / `missed`。时间条与执行记录页订阅实时刷新；next-run 头部信息同源更新 |

### 5.2 Tauri 壳（Rust command）

| command | 说明 |
|---|---|
| `set_keep_awake(enabled: bool)` | spawn/kill `caffeinate -i`；幂等 |
| `get_keep_awake() -> bool` | 子进程是否存活 |

通知（P1）：run 结束走既有 `ginno:notify` emit → notify-rust 链路，点击通知进回放视图。

### 5.3 runtime 内部模块

新增 `scheduler.py`（循环 + next_run 计算 + 触发 + 对账 + workflow 终态订阅 + 摘要
调用）与 `schedule_store.py`（schedules.json 读写 + runs jsonl，抄 usage_store）。路
由挂 `api/schedule.py`。`api/stream/turn.py` 与 `api/workflows.py` 的公共路由不改——
`_run_stream` 已支持 headless；workflow 走 `wf_store.create_run`（补一个可选
`origin` 字段透传进 run 记录）+ `_spawn_run_task` 既有内部函数。

---

## 6. 前端设计（实现层面）

- **导航**：`AppShell.tsx` footer 加 `<Link href="/scheduled">`（`AlarmClock`，含状态点）；
  `app/scheduled/page.tsx` 新路由（同 connectors 页结构）。
- **页面**：`components/scheduled/ScheduledPage.tsx`（头部 + 时间条 + 页签壳）、
  `TasksPanel.tsx`、`RunsPanel.tsx`、`TaskEditor.tsx`（模态；目标类型分段控件，
  workflow 下拉数据源 `g.workflows`，输入表单复用/抽取 `ContextEditor.tsx` 的
  schema 驱动表单——它已按 `context.schema` properties 渲染控件、无 schema 时退化为
  JSON 编辑器，保存值落 `context_override`）、
  `DayTimeline.tsx`（SVG 时间条，tooltip 抄 `settings/usage/charts.tsx` 的 `useTip`）。
- **回放**：`store.tsx` 加 `activeScheduleRunId` + `openScheduleRun/closeScheduleRun`；
  `AppShell.tsx` 主区域加 `ScheduleRunView` 挂载分支（与 activeRunId 分支互斥）；
  `ScheduleRunView.tsx`（顶栏 + 只读 transcript）。workflow 执行零新增——点击处直接
  `g.openRunView(workflow_run_id)`。
- **会话列表隐藏**：`AppShell` 分组处与 `SessionSearchModal` 过滤 `type==="scheduled"`
  （影子会话不进列表也不进搜索，已拍板）。
- **Workflows 页徽标**：运行列表行按 `run.origin==="schedule"` 显示「⏰ 定时」小徽标。
- **实时**：页面打开期间持有一条 `/api/ws/schedule` WS（同 connectors 通道模式），
  关页即断；不轮询。footer 状态点随 `run_finished` 事件更新（页面未开时不维护，冷数
  据由 `last_run`/GET 兜底）。
- **设置**：`keep_awake`/总开关不放 Settings——它们是本功能的一部分，留在 `/scheduled`
  头部（避免两处入口）。

---

## 7. 兼容与边界

| 情形 | 处理 |
|---|---|
| Ginno 未运行 / 机器睡眠错过触发 | 启动/唤醒后 tick 对账记 `missed`（每错过的计划点一条），不补跑 |
| runtime 崩溃留下 running 孤儿行 | 启动对账改 `error("interrupted")` |
| 同任务上次未跑完又到点 | 记 `skipped_overlap`，不排队 |
| workflow 配方被删除后到点 | 记 `error("workflow_missing")`，任务列表标「配方已删除」 |
| workflow 配方删除但历史 run 仍在 | 执行记录保留；回放走 run 视图（RunSubSessionView 已用 stub 配方名渲染） |
| 用户手动删影子会话文件 | 执行记录保留；回放显示「会话已删除」 |
| 删除任务 | 记录与会话保留；时间条历史照常渲染（task_name 冗余） |
| 合盖/手动睡眠 | keep_awake 无能为力——UI 文案与 missed 提示明确归因 |
| 单次任务执行完 | 自动 `enabled=false`，列表显示「已完成」 |
| 时区/DST | 本地日历语义重算 next_run_at |
| 磁盘写失败 | never raises，`_log.warning`，不影响调度主流程 |
| 多窗口/多页面 | 状态真值在 runtime；WS 事件广播，各页自行刷新 |

---

## 8. 分期计划

- **P0（MVP，用户诉求全部落地，prompt 与 workflow 双目标）**
  1. `schedule_store.py` + `scheduler.py`（四种计划、双目标触发、missed/对账/不重叠）+
     `api/schedule.py` 全部 REST + WS
  2. Tauri `set_keep_awake/get_keep_awake`（caffeinate）
  3. `/scheduled` 页：头部开关、任务 CRUD（目标类型切换 + workflow schema 输入表单）
     + 启停 + 立即执行、当天时间条（hover/点击/双目标分流）、执行记录（排序/过滤/分页）
  4. `ScheduleRunView` 只读回放 + 会话列表/搜索隐藏 scheduled + workflow 执行经
     `openRunView` 进现有 run 视图
  5. LLM 摘要调用（截断兜底）+ Workflows 运行列表「⏰ 定时」徽标（`run.origin` 标记）
  6. usage source `"schedule"` 接线（prompt 目标本体 + 两类目标的摘要调用）
- **P1**：桌面通知（复用 notify 链路）、错过补跑策略（`missed_policy: skip|run_once`）、
  cron 表达式计划、回放一键 fork 为正式会话、时间条 lane 展开、运行列表按「定时」过滤
- **P2**：`pmset repeat` 系统定时唤醒、Windows `SetThreadExecutionState`、任务级
  超时上限与重试、执行记录导出

## 9. 验收标准（P0）

1. 配置每日 09:30 的任务，把系统时间语义等价地验证（或临时改间隔为 1 分钟）：到点后
   执行记录出现 running→ok 两行，影子会话生成且**不出现在**侧边栏会话列表与搜索里。
2. 时间条上该执行显示为时段块，hover 出任务名/时长/摘要，点击进入回放，消息按主对话
   样式渲染，返回后原会话 WS 状态无损。
3. 总开关关闭 → footer 状态点变灰、任务行禁用、无任何触发；单任务开关同理。
4. keep_awake 开启后 `pmset -g assertions` 出现 caffeinate 断言；退出 Ginno 后断言消失。
5. 制造睡眠错过（停 runtime 跨过一个计划点再启动）→ 记录出现 missed；runtime 运行中被
  kill → 重启后该行变 error(interrupted)。
6. 同任务上次未完又到点 → 第二次记 skipped_overlap。
7. 执行记录按时间升降序切换正确；任务/目标类型/状态/日期过滤正确；分页正确。
8. 配置 workflow 目标任务：表单按 `context.schema` 渲染、`initial` 预填默认值；带
   `context_override` 到点生成无父会话的 run，执行记录出现 running→ok；时间条点击后经
   `openRunView` 进 run 视图，能看事件流与节点详情，返回按钮因无父会话而禁用；同任务
   未完再触发记 `skipped_overlap`。
9. workflow 配方删除后：任务列表标「配方已删除」，到点记 `error("workflow_missing")`；
   配方改出必填输入后到点记 `error("missing_input")`。
10. 间隔设为 4 分钟 → 前端与 API 均拒绝（400）。
11. 执行成功后 `summary` 为 LLM 生成的 ≤160 字摘要；断网/模型失败时回退为截断文本
    （摘要不缺席），usage 日志多一条 `source=schedule` 的摘要调用记录。
12. 定时触发的 workflow run 在 Workflows 页运行列表带「⏰ 定时」徽标，手动运行不带。
13. 定时执行的模型调用出现在用量统计：prompt 目标本体 source=schedule、workflow 目
    标本体 source=workflow、两类摘要调用 source=schedule。
14. 单测：next_run 四种计划计算（含 DST 模拟）、missed 对账、runs 去重（末行胜出）、
    双目标触发分流、间隔下限与必填输入校验、schedules.json 原子写与坏文件容错
    （`$GINNO_HOME` 隔离）。

## 10. 决议记录（2026-10-02，产品确认，无遗留开放问题）

1. ✅ **错过补跑**：只记 missed，不补执行（补跑策略 P1 再议）。
2. ✅ **间隔下限**：5 分钟，前端与 API 双侧校验。
3. ✅ **keep_awake**：只挡闲置系统睡眠（`caffeinate -i`），屏幕可暗可锁；不做
   「防屏幕变暗」子选项（有需要 P2 再加）。
4. ✅ **workflow 输入**：schema 驱动表单直接进 P0（DSL `context.schema` 渲染 +
   必填校验，缺输入到点记 `error("missing_input")`）。
5. ✅ **影子会话与全局搜索**：不进——列表与搜索均整类隐藏（P1 视反馈再议是否加
   ⏰ 徽标放开搜索）。
6. ✅ **摘要生成**：LLM 摘要（一次 ≤160 字廉价调用，失败回退纯截断），计入 usage
   `source:"schedule"`。
7. ✅ **Workflows 页运行列表**：定时触发的 run 加「⏰ 定时」徽标（`run.origin===
   "schedule"`）；按定时过滤 P1 再加。

实现期待定（不阻塞，开工时定）：回放 transcript 复用路径——① 从 ChatStream 抽纯
渲染子组件（倾向）vs ② 加 `transcriptOnly` 模式 prop。
