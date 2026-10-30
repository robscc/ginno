# TopBar 统一规整 + 关联会话(Session Links)— 产品方案

> 状态:**已实现**(2026-10-30,P0+P1+P2 一次落地,待 make app 真机验证)。
> 配套可点原型:`docs/design/prototypes/topbar-unification-prototype.html`。
> 前一版「子会话状态/花名册页」方案已废弃——用户明确:不是子 session
> 概念(可 detach),不做独立页,做进会话 TopBar、与 mount 同级。
>
> 实现落点:`session_links.py`(投影+戳+mount/detach)、worker_bus
> `deliver.py`/`watch.py`(动作成功后落戳,延迟导入破环)、api/sessions.py
> `GET/POST/DELETE /api/sessions/{id}/linked`;前端 `TopBarFuncButton.tsx`
> (统一按钮)、`LinkedSessionsChip.tsx`(面板+挂载+incoming)、TopBar/
> GoalChip/ContextFoldersChip 触发器重皮。单测 tests/unit/test_session_links.py
> (12 例)。分期的 P2「面板内派活」与「SubagentBoard 收敛」仍另立项。

## 0. 需求原话 → 设计翻译

| 用户原话 | 设计含义 |
|---|---|
| 「在 session 对话栏里面 跟 mount 一个级别 里面显示」 | 新功能 = TopBar chip 行里、`ContextFoldersChip` 同级的一个按钮 |
| 「他不是子 session 的概念,因为可以 detach」 | 成员是**独立会话**,关系是「挂进当前对话的视野」,可随时解除(detach ≠ stop ≠ delete);不建父子树、不进侧栏层级 |
| 「设定目标 mount idle状态那边好好规整一下,统一体验,最好是每个功能一个同宽的按钮」 | GoalChip / ContextFoldersChip / running-idle pill 三种异质 chip → 统一为**同宽图标按钮**系统 |
| 「DevAgent 用 Icon 展示就好 展示在 Title 左边」 | agent 从「图标+名称 pill(标题右侧)」改为**纯图标(标题左侧)**,名称进 tooltip |
| 「session 统计也统一调,hover 展示数据」 | usage pill(↑↓⚡)并入按钮系统,**数据 hover 展开** |

## 1. 现状与问题

TopBar(`shell/TopBar.tsx:142`)现状 chip 行:

```
[Title] [#a1b2c3] [🤖 agent 图标+名称 pill] [● Running/Idle pill] [GoalChip] [ContextFoldersChip]    [↑12k ↓3k ⚡89%] [⬇] [⋮]
```

1. **五种视觉异质**:标题随行 pill、mono 小 chip、双图标 pill、状态 pill、
   图标 chip——宽度/内边距/交互各一套;GoalChip 与 ContextFoldersChip 是
   「图标+文字」宽度随内容涨缩,排在一起参差。
2. **agent pill 占宽**:名称越长挤得越厉害,标题被压缩;而 agent 的身份
   识别靠图标+颜色已足够(侧栏行同款 `AgentIcon` + `agentHex`)。
3. **usage pill 常驻数字**:↑↓⚡ 三段数字长期占据右侧宽度,大多数时刻
   用户并不读它——数据该按需(hover)出现。
4. **新需求无处安放**:当前会话派过活/挂过的那些会话(assign/spawn/
   watch 的目标)散在转写卡片与侧栏树里,对话主界面没有一览面。

## 2. 方案 A — TopBar 按钮系统(统一体验)

### 2.1 目标布局

```
[◉agent-icon] Title #a1b2c3 ································ [⏺] [🎯] [📂₂] [👥₃] [📊] │ [⬇ ⋮]
   左:身份            中:弹性空隙(标题 truncate)                功能按钮组(同宽)      右:动作
```

### 2.2 统一按钮规范(新组件 `TopBarFuncButton`)

所有功能按钮一个模子:

- **同宽同高**:`w-8 h-8`(32px)方形 ghost 图标按钮,`hover:bg-card`;
- **徽标**:右上角 `min-w-4 h-4` 圆形计数/状态点(背景 `--st-running`);
- **语义色一律走 `--st-*` 主题变量**(2026-10-30 实机反馈定稿):在跑/徽标
  = `--st-running`(暗青/深青)、挂载 = `--st-planned`、完成 = `--st-ok`、
  失败 = `--st-error`、等待 = `--st-missed`、空闲 = `--st-skipped`。
  **不用绿色**——亮色主题下洗白不可读,且应用状态色系(scheduled/
  globals.css `--st-*`)本就无绿;
- **激活态**:有内容/进行中时图标着色(goal 激活→橙、挂载>0→蓝、
  关联会话有在跑→青;空态一律 `text-faint`);
- **hover = tooltip**(统一 `title` 样式组件,150ms 延迟):一行摘要
  (按钮是什么 + 关键数据),见 §2.4;
- **click = popover 面板**:同锚点、同宽度(`w-80`)、同结构
  (列表/空态说明 + 底部主操作);Goal 用现有编辑弹窗不变。

### 2.3 各按钮定义

| 按钮 | 内容源 | 徽标 | tooltip 摘要 | click 面板 |
|---|---|---|---|---|
| **⏺ 状态** | `running`(现有 prop) | 运行中=呼吸绿点;idle 无徽标 | `Running · 3m` / `Idle` | 无面板(纯状态位;点按空行为) |
| **🎯 Goal** | GoalChip 现有数据 | goal 激活时 violet 点 | 目标一句话(超长截断) | 现有 Goal popover(重皮同宽) |
| **📂 Mount** | ContextFoldersChip 现有数据 | 挂载数 `n` | 挂载名列表 + ⭐primary + ro/rw | 现有文件夹面板(重皮同宽) |
| **👥 Sessions**(新,见方案 B) | 关联会话投影 | 在跑数(working+waiting) | 前 3 个 `标题·状态` + `+N` | 关联会话面板(新) |
| **📊 Stats** | `usage` prop(现有) | 无 | `↑in ↓out · ⚡cache% · n calls` 全量 | 无面板(纯 hover 数据;未来可点开 usage 页) |

### 2.4 身份区(左侧)

- **agent = 纯图标**(`AgentIcon` + `agentHex` 着色,`h-4.5 w-4.5`),
  放 Title **左边**;tooltip = agent 名称。无 agent(裸 main)不渲染。
- `#a1b2c3` mono id chip 维持现状(身份信息,非功能,不进按钮系统)。
- 标题 truncate 规则不变,左侧让出的宽度(~60-120px)全给标题。

### 2.5 右侧动作区

`⬇ 导出` 与 `⋮ 菜单` 维持(动作不是状态,不套徽标/tooltip 系统);
usage pill 移除(被 📊 取代)。

## 3. 方案 B — 关联会话(👥 Sessions,本次新功能)

### 3.1 概念模型

**像挂载目录一样,把会话挂进当前对话的视野。**

- 成员是**独立会话**(任何类型:main/subagent/delegation/…),与当前
  会话是「我看它/我派它」的**水平关系**,不是父子从属——detach 只解除
  视野关联,会话本体照常运行,侧栏层级不因此变化;
- 关系来源三类(面板内以小字标注):
  - **auto 派生**:本会话 `assign_worker` 的目标(rel: assigned)、
    `spawn_subagent` 的孩子(rel: spawned)、`watch_worker` 的目标
    (rel: watching)、delegation 目标(rel: delegated);
  - **user 手动挂载**(P2):面板底部搜索任意会话挂入(rel: mounted),
    对齐 `workers.find` 的标题前缀寻址;
  - **incoming(谁在派我)**:P2 再议(当前会话是 worker 时反向可见)。

### 3.2 数据与 API

- **派生为主,存储最小**:auto 关系全部**运行时投影**(信封
  `ginno_worker_msg.from` / meta `parent_session_id` / `subscribers` 现成),
  不落新库;meta 只存用户意图两个字段:
  `linked_user: [session_id]`(手动挂)、`linked_hidden: [session_id]`
  (detach 过的 auto 成员抑制——同源再发生不自动回来,除非用户手动挂)。
  - **watching 的实现修正**(2026-10-30 实机踩坑):subscribers 在**目标方**
    meta,订阅方反向派生要跨项目全量扫(§6 禁)→ watch 成功时在订阅方落
    `linked_auto` 戳;`linked_report` 另做**同项目 subscribers 兜底派生**
    (同一份索引零额外 I/O,覆盖历史订阅),启动时 `backfill_linked_stamps`
    全局补戳一次(幂等,跨项目也覆盖)。assigned 戳(`in_links`)在投递
    成功后双侧落;历史 assign 无法回补(发送方无任何记录,信封在目标
    checkpoint 里)。
- 新 REST:
  - `GET /api/sessions/{id}/linked` → `[{id,title,type,rel,live_status,updated}]`
    (`live_status` = `project_status` 投影,working/waiting/idle/done/
    failed/stopped/unknown);
  - `POST /api/sessions/{id}/linked` `{target}` / `DELETE …/linked/{target}`
    (只动 `linked_user`/`linked_hidden`)。
- **live 更新**:面板打开时 2s 轮询(TaskCard 同款);树内成员
  (spawned/delegated)本就有 `subagent.status` 推送到本会话 socket,
  白拿推送、轮询兜底。徽标数随面板外也刷新(挂 5s 轮询,同侧栏 Tasks
  角标)。

### 3.3 面板(👥 click)

```
┌────────────────────────────────────┐
│ Linked sessions            2 live │
│ ──────────────────────────────── │
│ 🟢 AMS 迁移方案核对      main · assigned │
│    2m · ⏹ Stop  ↗ Open  ✕ Detach │
│ 🟢 fix flaky e2e         subagent · spawned │
│    14m · ⏹ Stop  ↗ Open  ✕ Detach │
│ ⏳ Weekly report digest   subagent · watching │
│ ✅ delegation · refactor  done · delegated   (dim) │
│ ──────────────────────────────── │
│ [＋ Mount a session…]   (P2)     │
└────────────────────────────────────┘
```

- 排序:working/waiting 置顶 → 其余按 updated 倒序;终态行 dim;
- 行动作:**Open**(`setActiveSession` 现状跳转)、**Stop**(既有
  `/stop` 端点,级联,working/waiting 才显示,需 confirm)、
  **Detach**(解除关联;auto 成员落 `linked_hidden`,user 成员移出
  `linked_user`;一律不 confirm——无破坏性);
- 空态:一句话说明(「派活/订阅的会话会出现在这里」)。

### 3.4 与既有面的关系

- **不替代**转写卡片(WorkerResultCard 等):卡片是叙事流里的「事件」,
  面板是「当前状态」;两者数据同源。
- **不动侧栏树**:侧栏 parent-child 归属是 subagent 语义,继续;
  本面板是水平视野,两套并存互不干扰。
- SubagentBoard(主会话常驻委派板)与 spawned 成员高度重叠——
  面板落地后观察使用率,考虑下一轮收敛(本方案不动它)。

## 4. 分期

| 期 | 内容 | 量级 |
|---|---|---|
| **P0** | TopBar 按钮系统:`TopBarFuncButton` + 五按钮迁移 + agent 图标左置 + usage→📊 hover + 三块面板重皮同宽。纯前端 | ~1.5 天 |
| **P1** | 👥 关联会话:auto 派生投影 + linked API + 面板(Open/Stop/Detach)+ 徽标轮询 | ~2 天 |
| **P2** | 手动挂载(搜索)+ incoming 分组 + 面板内派活(assign 信封复用)+ SubagentBoard 收敛评估 | 另立项 |

## 5. 决策点(待拍板)

| # | 问题 | 倾向 |
|---|---|---|
| A | ⏺ 状态按钮 click 无行为,可接受? | 接受(纯状态位;若想点开看「本会话在跑的 turn/工具」,列为 P2 小项) |
| B | 关系成员 auto 派生 + 手动挂混合,还是纯手动? | 混合(推荐):纯手动有冷启动问题——用户派完活还得记得手动挂;派生+可 detach 两头兼顾 |
| C | detach 的 auto 成员要不要永久抑制(`linked_hidden`)? | 要(推荐):否则 detach 后下次同源 assign 又回来,「detach」语义失真 |
| D | 📊 Stats hover 展示会话累计(现状口径)还是加上今日/近 24h? | 先维持会话累计(数据现成);今日口径要聚合 usage 日志,列为后续 |
| E | agent 纯图标后,名称只在 tooltip,可接受? | 接受(与侧栏行同款识别逻辑:图标+颜色;名称 hover 即得) |

## 6. 风险

- **TopBar.tsx 触达面**:该文件同时服务 home/session 两态,改造需保
  home(无 session)态不出空按钮(无 session 时 🎯📂👥📊 整组隐藏,
  现状如此,维持);
- **派生投影的成本**:`GET /linked` 每次全量扫信封/meta——单会话关联
  数个位数,投影纯内存读,可忽略;但实现时禁跨项目全量 glob(只扫本
  会话触达过的目标集合);
- **stop 的破坏性**:级联停(含目标子孙),面板内必须 confirm;
- **旧 sidecar 兼容**:P0 纯前端、新 API 未上线前 👥 按钮隐藏
  (`api` 探测或版本门控),避免 404 空面板。
