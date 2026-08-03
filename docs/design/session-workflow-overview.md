# Session × Workflow 产品交互 & 技术方案 · 总览与选型

> 本文件在三个方案分支上**内容一致**，用于横向对比与选型。每个方案的完整交互说明、多轮模拟、技术方案、逐屏截图见各自分支：
> - **方案 A · 会话优先**：分支 `design/session-workflow-a` ｜ 文档 `docs/design/session-workflow-A.md` ｜ 原型 `docs/design/prototypes/a/index.html` ｜ 截图 `docs/design/screenshots/a/`
> - **方案 B · 工作室优先**：分支 `design/session-workflow-b` ｜ 文档 `…-B.md` ｜ 原型 `…/b/index.html` ｜ 截图 `…/b/`
> - **方案 C · 双模混合**：分支 `design/session-workflow-c` ｜ 文档 `…-C.md` ｜ 原型 `…/c/index.html` ｜ 截图 `…/c/`
>
> 说明：三套设计均以**独立分支 + 自包含静态原型 + 截图**交付，**未改动任何现有业务逻辑/主分支代码**。原型为单文件 HTML（内联 CSS/JS，无外部依赖），可直接双击打开，或 `cd docs/design/prototypes/<a|b|c> && python3 -m http.server` 后访问；用 `?screen=<id>` 直达某一屏。

---

## 0. 现状一句话（设计起点）

当前 Ginno 里 **session 与 workflow 是两套弱连接的对象**：左栏 Sessions = 对话归档；`/workflows` = 版本化"配方"库；二者仅靠"从最近一个会话总结"（且直接建 v1、跳过确认）与"开发会话"相连。缺口：① 无统一"任务/运行"视图；② 运行被甩出对话（隔离 thread，聊天里只剩跳转卡，无法观察/干预）；③ run↔session 关联弱（`session_id` 没传、靠正则匹配 `run_id`）、无 run 级 WS 推送（1.5s 轮询）、无 cancel/resume/rerun；④ 缺少"对话⇄流程"双向演化。三个方案以不同取向补这些缺口（基建层——run 推送/控制、确认通道解耦——三者**高度共用**，差异在**产品主战场与对象模型**）。

---

## 1. 选型决策矩阵

| 维度 | A 会话优先 | B 工作室优先 | C 双模混合 |
|---|---|---|---|
| 主交互入口 | 聊天页 | Workflow Studio 页 | 统一「任务」列表 |
| 会话↔流程关系 | 流程=会话的投影 | 会话服务于流程 | 同对象双视图，双向同步 |
| 学习成本 | 低 | 中-高 | 中 |
| 轻/对话型用户契合 | ★★★★★ | ★★ | ★★★★ |
| 重流程/无头/定时契合 | ★★ | ★★★★★ | ★★★★ |
| 配方跨会话复用 | 二等（套用） | 一等 | 一等（蓝图↔实例） |
| 运行可观察/可干预 | 强（对话内运行块） | 强（观察台+人工决策） | 强（投影+流程视图） |
| 实现成本 | 中 | 高 | 中-高 |
| 对现有代码侵入 | 中（server WS+engine） | 高（Studio 前端重写） | 高（session 对象模型重构） |
| 主要风险 | 长会话冗长；无头场景弱 | 对话亲切感被工具化稀释 | 双模映射/投影正确性是长尾难点 |

---

## 2. 各方案速览

### 方案 A · 会话优先 —「聊着沉淀，跑回对话」
- **核心**：workflow 不立主战场，是 session 的"过程视图 + 可固化派生配方"。主交互始终在聊天页。
- **关键屏**：① 聊天主视图（消息带「↳流程节点」标记、右栏「过程」mini-DAG）；② 提炼为流程（**可选会话/轮次范围** + diff 确认，修复现状）；③ **对话内运行**（运行块就地展开，含暂停/取消/从本步重跑，右栏实时 context+来源 meta）；④ 配方库（次级，"套用→新建会话"）。
- **最适合**：把 agent 当协作伙伴、以对话为主的用户；希望最低学习成本顺手沉淀流程。

### 方案 B · 工作室优先 —「像 n8n/Dify 那样搭流程」
- **核心**：workflow 是一等公民 + 专属 Studio（画布+节点检查器+运行观察台+版本+开发抽屉）；session 退化为"开发会话/观察会话"。
- **关键屏**：① Studio 设计（点阵画布、节点端口、选中→右检查器就地编辑）；② **运行观察台**（实时 mini-DAG + 事件流 + Supervisor 人工决策卡）；③ 从会话导入（**任选会话**+范围+确认）；④ 开发会话抽屉（编辑不离开 Studio，内联 diff 确认）。
- **最适合**：需要反复运行、可观测、可干预复杂流水线的重流程/自动化用户。

### 方案 C · 双模混合 —「一个任务，对话/流程随便切」
- **核心**：统一对象「Task」= 一个 thread，带模式切换（对话/流程/混合）+ 步骤轨道 + 节点↔消息双向映射；Blueprint 与实例分离、双向闭环。
- **关键屏**：① 统一任务列表（模式徽标+状态+过滤）；② 对话视图（模式切换+步骤轨道+↔步骤标记+自动生成结构提示）；③ 流程视图（节点↔对话片段+编辑节点同步注入对话）；④ 蓝图套用/存为版本。
- **最适合**：相信"对话与流程本是一体"、愿投入做统一对象与双向映射，追求最大灵活度。

---

## 3. 共用基建（三方案都要做，可作为"第 0 期"独立先行）

无论选哪个，以下后端/协议改动都必要且**互不冲突**，建议先做、与产品取向解耦：
1. **run↔session 强关联**：`POST /workflow_runs` 真正写入 `session_id` + 新增 `present_in_session_id`；移除"正则匹配 run_id"的 hack。
2. **run 级推送替代轮询**：新增 `run.bind / run.event / run.status`（A/C 走会话 socket 扇出，B 倾向独立 `/ws/runs/{id}`）。
3. **run 控制**：`cancel` / `resume{context_patch?,decision?}` / `rerun_from`；engine 支持 human interrupt 可恢复（现状对 orphan interrupt 直接 abort）。
4. **确认通道解耦**（呼应设计稿 Q9）：新增入站 `version_response` / `run_control`，使 diff 确认与运行控制不再寄生 `permission_response`，便于后续移除 permission 子系统。
5. **summarize 增强**：`summarize-from-session` 支持 `range` 且**默认只返回草稿**（交 diff 确认），不再"只取 `sessions[0]` 直接建 v1"。

---

## 3.5 Supervisor 统一设计（第二轮补充）

> 你指出的缺口已补齐：三个方案均新增 Supervisor 产品设计。统一概念 + 各方案落点 + 独立原型/截图如下。

**统一概念**：Supervisor 是挂在执行上的治理层，在检查点（每步后/指定节点/出错时）发出控制决策 `继续/重试本步/跳过/改context/暂停/中止`。**auto**=独立 LLM 按策略+预算结构化裁决（默认仅异常干预、pass-through 不刷屏；不确定/超预算**回退 human**）；**human**=检查点 interrupt 推裁决卡。**安全阀**：每步 retry_limit、单 run 干预上限、token 预算、confidence 回退阈值。**配置三层**：蓝图/DSL 默认 → 运行前 override → 运行中切模式。所有裁决记 `supervisor_decision` 事件，可审计/回放。

**各方案落点（差异在呈现与聚合）**：
| | Supervisor 呈现 | 原型/截图 |
|---|---|---|
| A 会话优先 | human 裁决卡**留在对话流**（与运行块一体）；auto 为小字注释；右栏「过程/Supervisor」三层配置+监督日志 | `a/supervisor.html`（a5 human / a6 auto） |
| B 工作室优先 | Supervisor 为 Studio **一等公民**：控制台 tab（mini-DAG 画 sup 门+auto 时间线+预算条+配置检查器）+ 左栏**跨运行决策收件箱**（多无头运行同时等人的盯盘刚需） | `b/supervisor.html`（b5）+ `b_b2`（观察台裁决卡） |
| C 双模混合 | **双视图同源**：流程视图把 supervisor 画成节点间的"门"，对话视图把同一 interrupt 渲染为内联卡，任一视图决策另一视图同步消解；配置随蓝图/实例携带 | `c/supervisor.html`（c5 门 / c6 内联卡） |

**技术增量（共用）**：DSL `supervisor` 扩展 `{enabled,mode,checkpoints,retry_limit,strategy,model?}`；compiler 插门节点（现状为桩）；engine 实装 auto 结构化裁决 + human 可恢复 + 应用决策；WS `supervisor.event/decision/pending`；REST `POST /workflow_runs/{id}/decide`、`PUT /workflows/{id}/supervisor`、run 创建 `supervisor_override`；收件箱=`GET /workflow_runs?supervisor_pending=true`。

---

## 4. 推荐与组合策略

- **若产品主叙事是"对话型个人 AI 助手"** → 选 **A**（成本最低、最贴合现有体验，运行回到对话的闭环已能解决最大痛点）。
- **若主打"可复用/可观测自动化流水线"** → 选 **B**（Studio + 观察台 + 人工决策是重流程的刚需）。
- **若愿赌"对话=流程"的统一心智** → 选 **C**（上限最高，但映射/投影正确性是长期投入）。
- **渐进组合（推荐落地路径）**：先做 §3 共用基建 → 以 **A** 为底座（对话内运行/观察 + 范围化提炼/确认）→ 叠加 **C** 的"步骤轨道 + 模式切换 + 节点↔消息映射"作为增强 → 把 **B** 的 Studio/观察台作为"高级/自动化"可选入口。如此每一步都可独立交付、可回退，且逐步逼近 C 的统一模型而不一上来承担其重构风险。

---

## 5. 如何查看每个方案

```bash
# 切到某方案分支
git checkout design/session-workflow-a     # 或 -b / -c
# 看设计文档
$EDITOR docs/design/session-workflow-A.md
# 看原型（任选其一）
open docs/design/prototypes/a/index.html   # 直接打开
# 或起本地服务
cd docs/design/prototypes/a && python3 -m http.server 8099
# 看关键截图
ls docs/design/screenshots/a/
```

> 提示：原型顶部的页签可切换各屏；亦可用 `?screen=<id>` 直达。每个截图文件名与文档"逐屏"章节一一对应。

---

## 6. 待你拍板（跨方案共问）

- 主战场取向：A / B / C / 渐进组合？
- 共用基建（§3）是否作为独立第 0 期先行？
- run 推送载体：会话 socket 扇出 vs 独立 run socket？
- 运行可干预深度：本期是否含 human 决策（supervisor=human）与 rerun_from？
- 是否保留"无 DAG 拖拽编辑器"原则（拓扑靠对话/导入/检查器生成）？
