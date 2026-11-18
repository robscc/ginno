# Ginno 项目（Project）产品重设计

- 状态：**⏸ 已挂起（PARKED，2026-11-18 用户决策）**——设计已收敛（v0.3，
  全部拍板决策见 §3.2/§4.1），实现未开工；重启时从 §7 P1（项目实体 CRUD +
  切换器）直接开始，§9.2 改造清单里的文件级定位仍然有效。挂起期间不受
  CHANGELOG 约束，勿删。
- 日期：2026-11-17 ~ 2026-11-18
- 关联：`byom-remote-machines-design.md`（多机会话调度）、team 模板 API、`design-mocks/project-redesign-*.html`（三份交互演示）
- 附录 A：工作区文件被外部进程删除事件留档

## 1. 问题陈述

当前 Ginno 的「项目」名存实亡：只有一个 `default` slug 在用，会话、记忆、skills、
挂载目录全部堆在同一个命名空间里。用户实际的并行工作流（ginno 开发 / ams 平台 /
agent-prd / 调研）被迫混居，导致：

1. **上下文污染**：跨项目挂载互相干扰，wiki 检索、记忆注入、TODO 清单都是全局的；
2. **无法并行**：一个窗口一个会话，想在 ginno 里跑构建、同时在 ams 会话里查日志，
   只能开两个 Ginno 实例（sidecar 端口还会冲突）；
3. **进度不可见**：多个后台 session 在跑时，没有一眼可见的「哪个项目在忙」。

目标：把「项目」升级为**一等公民的工作空间**——新建/选择有完整产品流程，
session 与小队归属项目，智能体全局共享，界面支持多路并行分屏（按会话/按项目双模式），
活跃项目有醒目的运行态指示。

## 2. 核心概念模型

| 概念 | 归属 | 说明 |
|---|---|---|
| **Project 项目** | 顶层 | 工作空间：拥有自己的会话列表、小队、挂载目录、记忆（GINNO.md）、项目级 skills、TODO |
| **Session 会话** | 绑定项目 | 创建即归属、不可迁移（跨项目引用靠 artifact/attach_ref） |
| **Agent 智能体** | **全局** | 注册表全局共享（现状即如此，零改动）；skills 分全局/项目两级 |
| **Team 小队** | 绑定项目 | 小队成员、派活树、模板实例化结果都存在项目内（Multica 式常驻编制） |
| **Panel 面板** | 视图层 | 界面上的一个会话视窗，同一时刻一个面板显示一个 session，可切换 |

## 3. 产品流程设计

### 3.1 新建项目（3 步向导，可跳步）

入口：项目切换器下拉「＋ 新建项目」。

1. **基本信息**：项目名、图标（emoji/颜色，用于分屏面板识别）、描述；
2. **工作目录**：选本地文件夹作 primary 挂载（复用现有 context folders 能力），
   未选 = 纯聊天型项目；
3. **编制**（可选）：从 4 个内置模板（内阁/软件交付/调研/验收）实例化小队。

高级项（折叠）：项目级环境变量（复用 env_inject）、项目级 skills 目录、默认模型/权限档。

### 3.2 项目选择与切换（最终收敛版）

- **侧栏结构（自上而下）**：品牌行 → **项目切换器** → ＋新会话 ⌘N / 🔍搜索会话 ⌘K
  → 会话列表（固定分组：置顶/今天/Earlier）→ 智能体四态区 → 底部导航 8 项
  （AgentTeam/连接器/定时任务/后台任务/知识库/本能/工作流/设置，原样保留；
  AgentTeam 列表按当前项目过滤，`GET /api/teams?slug=` 后端已支持）。
- **切换器双形态**（2026-11-17 决策，取代独立总览页）：普通下拉（最近项目 + 当前项
  打勾 + 「查看所有项目 →」）→ 点该项**同一个 popover 原地扩展**为项目网格
  （图标/名字/running 徽章/最近会话摘要，点卡片即切换并收起）。不新开页面、
  不新记快捷键。
- **两条切换动线**：A 下拉直切（两级点击）；B 展开网格看状态（彩虹=running、
  琥珀=在等你）再点卡片切。殊途同归：侧栏上下文整体切换（项目名/图标/会话列表/小队）。
- default 项目永远存在、不可删除。

**已拍板移除项**（2026-11-17）：
- 侧栏 agent 筛选 chips 整块删除（跨项目看智能体由四态区承担；@ 提及保留在输入框）；
- 「时间/项目」二选一视图切换整个删除（项目=一级选择交给切换器；项目内列表固定按
  时间分组，`useSessionGrouping` 只留时间分支）；
- 独立「项目总览/Dashboard」页面不做（总览=切换器网格形态）。

### 3.3 会话与项目的绑定 + 新建会话

- 4 个入口，全部归属当前上下文：① 侧栏「＋」/⌘N → 当前项目；② 分屏面板头部
  「＋ 新会话」→ 该面板所属项目并直接落在该格子（原会话退到「切换会话」历史，不丢）；
  ③ 切换器网格卡片「＋」→ 明确指定项目；④ 输入框快捷命令 → 跟随聚焦面板。
- 实质改动：`lib/api/sessions.ts:213` 硬编码 `"default"` → 当前项目 slug；
  创建即归属、不可迁移；小队成员/subagent 会话自动继承父会话项目（现状已如此）。
- 全局搜索（⌘K）跨项目搜会话，命中后提示所属项目并一键切换/新面板打开。

### 3.4 小队与项目绑定（Multica 式）

- 每个项目内独立的小队区：成员列表、派活树、小队记忆；`from_template` 透传当前
  项目 slug（前端接线，API 已支持）；工作流/定时任务按项目注册与展示。
- 小队成员是「项目内的 agent 引用」：同一 Dev Agent 可在多个项目编制中并存。
- `PATCH /api/teams/{id}` 换 slug 需补白名单校验；换项目不自动迁 workspace（提示用户）。

## 4. 多面板并行（分屏 / 网格，双模式）

### 4.1 布局模型

- 顶栏布局切换器：**模式档「按会话｜按项目」** + 档位 `1×1 / 1×2 / 2×2 / 3×3`。
- **按会话模式**：每面板绑一个 session（可 4 格同项目、可跨项目）——最通用模型。
- **按项目模式（2026-11-17 决策补入）**：每格 = 一个项目，显示其最近活跃会话 +
  格内迷你会话切换；3×3 迷你模式即此模式紧凑档（一行摘要+状态，适合多项目挂机监控）。
- **布局与面板分配按项目记忆**：切回某项目恢复它上次的布局/格子内容
  （待定项：记忆粒度，见 §8）。
- 拖拽分隔条调比例；布局持久化到 settings；非聚焦面板来消息只亮未读徽标不抢焦点；
  **离屏面板渲染节流**（crystl 经验：10+ 并发的前提）。

### 4.2 通用规则

- 面板头部：项目图标+名字+彩色描边；聚焦面板决定右栏（代码/产物/TODO/通知）与
  输入框快捷条的绑定 session；面板内可随时「切换会话」。
- 多面板并发 turn 调度上限见 §9.3 风险。

## 5. Running 状态的彩虹设计

- **项目级**：切换器条目、网格卡片、面板描边——存在 running turn/后台任务的用
  **动态彩虹描边**（conic-gradient 缓慢旋转）+ 活跃计数徽标（⚡3）。
- **会话级**：列表项左侧细彩条 + 生成中脉冲点。
- **强度分级**：等待用户输入（ask_user/HITL 中断）→ **琥珀呼吸灯**（比 running 更
  需要人）；running → 彩虹旋转；空闲 → 无装饰。动画遵守 `prefers-reduced-motion`。

## 6. 迁移与兼容

- default 项目一切原地不动；首启扫描 `~/.ginno/projects/` 已有 slug 自动纳入。
- TODO 清单按项目拆分（顺带修并发覆盖 bug）；A2A/worker 寻址保持全局。

## 7. 分期路线

| 阶段 | 内容 | 价值 |
|---|---|---|
| P1 | 项目实体 CRUD API + 切换器（含网格形态）+ session 归属 + 侧栏改造（删 chips/删视图切换） | 概念落地 |
| P2 | 分屏双模式 + 面板 session 切换 + 布局持久化（按项目记忆） | 并行工作流核心 |
| P2.5 | 浮动 HITL 卡片（全局悬浮、项目色编码、跨会话可答） | 并行不漏审批 |
| P3 | 小队项目化 + 新建项目向导接线 | 编制 |
| P4 | 彩虹指示全量 + 3×3 迷你模式 + ⌘K 跨项目搜索 | 打磨 |

## 8. 待定问题

1. 挂载目录冲突（两项目 primary 同一目录）：允许但会话互不感知，靠 git 兜底。
2. token 用量/配额是否按项目出账单视图（P4 顺手）。
3. 项目归档 vs 删除（删除进回收站）。
4. sidecar 单实例下多项目并行 turn 的并发上限/排队策略（P2 前实测）。
5. 布局按项目记忆的粒度（全局布局 vs 每项目独立布局）——倾向每项目独立。
6. 3×3 值不值得做 vs 4 路封顶先行（倾向按项目模式 3×3 只读监控版先行）。

## 9. 技术落地方案（基于代码调研 2026-11-17）

### 9.1 现状盘点

**已就绪**：session 创建 API 本就收 `project_slug`（无校验，`api/sessions.py:71-73`）；
skills/goals/instincts/session-files 已 per-slug 隔离；team 已绑 slug（roster 字段 +
`list_teams(project_slug)` 过滤），`from_template` 已收 slug；agent registry 全局。

**缺口**：
1. 项目无实体（无 CRUD/清单 API，slug 是自由字符串）；
2. 前端硬编码 `default` 五处：`lib/api/sessions.ts:213`、`right/code/CodePanel.tsx:87`、
   `right/code/ChangesPanel.tsx:29`、`shell/TopBar.tsx:234`、`pin/PinApp.tsx:197`；
3. `useSessionGrouping.ts:165` 项目视图按 primary_folder 分组（将随视图切换删除一并处理）；
4. 无通用分屏组件（可复用：RightPanel tab registry、`PanelResizer`、ModPaneHost
   pane 条、workflow `StudioShell`）；
5. `from_template` 前端未接线。

另注：runtime `projects.py` 的「会话内自动发现仓库根」与 slug 体系正交（上下文可见性
提示，非工作空间），保持正交不合并。

### 9.2 改造清单（按 P1-P4）

- **P1**：新增 `api/projects.py`（GET/POST/PATCH/DELETE，落 `projects/<slug>/project.json`，
  default 隐式存在）；`CreateSessionRequest` 加存在性校验；前端 project store 替换五处
  硬编码；`ProjectSwitcher` 组件（下拉+网格两形态）；删 chips 区、删 groupView 切换。
- **P2**：`app/page.tsx` 改 LayoutContainer（布局 state + panels[]/projects[]）；
  ChatStream 组件化去单例假设（按 session 订阅、离屏节流）；`RunningRainbowBorder`。
- **P2.5**：HITL 卡片全局悬浮队列（复用 ask_user 事件通道）。
- **P3**：向导接线；team PATCH 校验。
- **P4**：`GET /api/projects/status` 聚合端点（每项目 running/waiting 计数）+ ⌘K。

### 9.3 风险点

- 多面板并发 turn 的资源叠加未知（graph 已有 backoff，P2 前实测 4 路构建类任务）；
- ChatStream 单例化假设需先做依赖审计再动；
- Pin/浮窗同步接入 project store。

## 9.4 竞品模式借鉴（Herd / crystl / Orca / Conductor / cmux，2026-11 联网调研）

**采纳**：① 全局 Agent 侧栏四态（Working/Blocked/Done/Idle，Herd/cmux 共识）→ 四态区；
② 两级导航（crystl Gem→Shard）→ 切换器+网格；③ 浮动 HITL 卡片（crystl）→ P2.5；
④ 离屏渲染节流 → P2 实现要求；⑤ Peek 悬浮预览 → P3+ 派活树增强。

**暂不做**：worktree 隔离（聊天为主场景；若 P2 后高频并行跑代码任务再做自动 worktree +
未合并徽章）；WORKBENCH.md 人机共读任务文件（与 TODO 按项目拆分时一并考虑）。

**反面教训**：GUI 与 headless 必须同源（Ginno 桌面/CLI 共享 sidecar 天然满足）；
功能堆叠会流失 Tauri 轻量优势。

## 附录 A：工作区文件被外部进程删除事件留档

**时间线**（外部进程删除本仓库未提交文件，截至 2026-11-18 共 4 次）：

| # | 时间 | 损失 | 备注 |
|---|---|---|---|
| 1 | 2026-10 | `docs/design/` 九份设计文档 | 当时未 git 固化，部分永久丢失 |
| 2 | 2026-11 初 | `apps/web/public/fileicons/*.svg` 24 个 | 可从 git 恢复 |
| 3 | 2026-11 中 | fileicons 再次被删 | 同上 |
| 4 | 2026-11-18 | 本设计 `docs/design/project-workspace-redesign.md` + `design-mocks/project-redesign-*.html` ×3 | 靠对话上下文重建（本次留档即重建版） |

**疑似根因**：头号嫌疑为 alink 进程（历史 fs_usage 观察指向，未捕获到决定性现场）；
删除均发生在文件**未提交**状态下，已提交文件从未丢失。

**对策（本次起强制执行）**：
1. 任何设计文档/UI 原型产出后**立即 git add + commit**，不做「攒一批再提交」；
2. 长会话产物在会话结束前完成固化；
3. 下次复现时第一时间跑 `fs_usage -w -f filesys | grep -E "unlink|rename"` 抓现场，
   并 `ls -lO /Applications | grep alink` 核对进程在位情况。
