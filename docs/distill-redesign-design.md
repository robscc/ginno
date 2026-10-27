# 「沉淀为」重设计：把一次性会话变成可复用资产

- 日期：2026-10-26
- 状态：设计定稿（决议已定，待实现）
- 前置：V2 P1-2 的 DistillMenu / api/distill.py、workflow synthesis（SummarizeModal）、
  memory promote（PromoteModal）、suggestions（V2 P2-1）
- 原型：`docs/distill-redesign.html`

## 决议（2026-10-26）

| 项 | 决定 |
|---|---|
| 草稿生成 | **旁路 LLM**（$.model.fork 语义：会话 checkpoint 为上下文单发、不进对话、source=distill；离线降级模板） |
| 知识卡落位 | **维持 Raw/**——沉淀物是原料，检索化交给 Wiki 编译；UI 显式标注「不会被检索」并给一键「编译为 Wiki」；Memory/ 作为可选落位 |
| 入口收敛 | **「总结成流程」并入沉淀菜单**（工作流行直连 SummarizeModal，last-N/草稿恢复保留），移除独立 chip |
| 实施范围 | **P0 一次做满三层入口**：会话菜单重做 + turn hover + 划选浮层（suggestions 后端补发放 P1） |

## 0. 一句话

现状是「一个按钮、三条腿、只有一条腿能走」：workflow 有完整的 LLM 草稿→预览→编辑→创建流，
skill 和知识卡却是**无预览、无确认、确定性模板的一键哑写**——产物是工具调用参数堆栈和
占位符骨架，知识卡还落进**不被索引的 Raw/**（死知识）。重设计让三条腿同构：

> **选材 → 生成草稿 → 预览/编辑 → 落盘 → 复用反馈**

## 1. 现状（代码事实）

| 环节 | skill | 知识卡 | workflow |
|---|---|---|---|
| 入口 | DistillMenu（ChatStream.tsx:698） | 同左 | 同左 + SummarizeMenu 独立入口（重叠） |
| 生成 | 无 LLM，`_skill_md` 模板（distill.py:88-116） | 无 LLM，问题/步骤/占位（distill.py:176-195） | LLM ≤3 次自纠（synthesis.py） |
| 预览/确认 | **无**，点即写盘 | **无**，点即写盘 | SummarizeModal：DAG+节点卡+JSON+dry-run |
| 落点 | `~/.ginno/skills/<slug>/`（CJK slug 与 installer 规则不一致） | **Raw/**——indexer 明确排除（api/knowledge.py:60-75），不可检索 | `~/.ginno/workflows/`，可 @ 引用 |
| 反馈 | toast 路径 | toast 路径 | 创建即可运行/进 dev 会话 |
| 质量 | 正文是 `tool — args` 堆栈；trigger 硬编码；中文 only | 结论是占位符 | 已达标 |

横向问题：
1. `distilled_to` 标记只写不读（UI 永远重复推荐）。
2. 只有会话粒度；后端有 `msg_range`（workflows.py:250）但聊天侧只暴露 last-N。
3. 完成 turn 无 hover 动作（失败气泡才有）；无划选动作（95c8250 只做了选区基建）。
4. 建议 90% 已接线：客户端 `action.type:"distill"` 已实现（useSuggestions.ts:175），后端从不发。
5. 知识库未配置时 kb 400；skill 名无校验，坏 frontmatter 静默不可加载。
6. 记忆→知识 PromoteModal 是被埋没的金标准（dedup 门 + 落位 + 可编辑 + 应用）。

## 2. 设计原则

1. **三条腿一个生命周期**：任何形式的沉淀都不允许「点即写盘」。
2. **草稿必须 LLM 生成**：走旁路单发调用（`$.model.fork` 同款：以会话 checkpoint 为上下文、
   不进对话、用量记账），确定性模板降级为离线兜底。
3. **落盘前必经确认，确认处必可编辑**。
4. **落盘即活**：知识卡落可检索区；skill 落盘前过解析校验；workflow 落盘即可 @/运行。
5. **入口分三层**：会话级（重做菜单）、turn 级（hover，新）、划选（新）；建议条做上下文触发。

## 3. 交互设计

### 3.1 会话级：菜单重做（保留头部按钮）
- 每项显示**将生成什么**的一句话预览（由一次轻量旁路调用或启发式给出：
  「≈12 步的部署检查 skill」「3 条结论的知识卡」「6 节点工作流」）。
- **推荐徽标**：按会话特征分类（工具密集→workflow；稳定流程→skill；事实结论→kb）。
- **已沉淀状态**：读 `distilled_to`，已沉淀的形式显示 ✓ 与产物名，可「查看 / 再沉淀」。
- **入口收敛**：「总结成流程」（SummarizeMenu chip）并入本菜单的工作流行，消除双入口
  （SummarizeMenu 的高级能力——last-N、草稿恢复——进选材器）。

### 3.2 选材 + 形式（新，统一面板）
打开后先选「用什么 + 存成什么」：
- **来源**：整个会话 / 最近 N 轮 / 指定消息区间（复用 summarize-trace 的 msg_range，聊天侧首次暴露）/ 划选文本（来自划选入口时锁定）。
- **形式**：三张卡各带「什么时候选它」（怎么做→skill / 知道了什么→知识卡 / 怎么自动化→workflow）+ 第四项「让 Ginno 决定」（分类器 = 3.1 的推荐逻辑）。

### 3.3 草稿确认（统一 DistillModal）
一个壳三种内容，顶部可**换形式重生成**，底部统一操作（保存 / 重新生成 / 换形式 / 取消；未保存离开确认）：
- **skill**：字段区（名称[ASCII 校验 + 重名/影子检测]、触发方式 user/model/both、工具绑定）+ 正文编辑（`$ARGUMENTS` 占位提示）；「验证」跑 `_parse_skill_file`，**不过不许保存**；正文语言随 `prompt_language`。
- **知识卡**：LLM 摘写（问题 / 结论 / 适用条件 / 来源链接 `[[wiki]]`）；**落位维持 Raw/ 为默认**——沉淀物是原料，检索化交给 Wiki 编译管线；表单显式标注「Raw/ 不参与检索」，提供**一键「编译为 Wiki」**（保存反馈里直接给，跑 wiki-compile 定向该卡）；`Memory/` 为可选落位（在索引内、即存即检索）；**去重门**复用 promote preview（WikiRetriever top-3，≥0.5 → 建议合并并给出目标页）。
- **workflow**：内核即现有 SummarizeModal（不动），只统一入口与壳。
- 草稿生成流式反馈（旁路调用，进度可见，失败给确定性模板兜底 + 「离线草稿」标记）。

### 3.4 落盘反馈（复用闭环）
- skill：「已创建 `/deploy-checklist`——新会话输入 `/deploy-checklist` 试用」+ 去 Skills 设置。
- 知识卡：「已存入 `Memory/xxx`——下次相关问题会自动注入」+ 查看知识卡。
- workflow：「已创建——立即运行 / 用 @ 引用」。
- `distilled_to` 前端回读：菜单打 ✓；建议条不再推。

### 3.5 turn 级与划选
- 完成 turn 的 assistant 气泡 hover 出「沉淀」图标（沿用失败气泡的 `opacity-0 group-hover` 模式，
  TranscriptList.tsx:246 先例），默认选材=该 turn，直达选材器。
- 划选浮层「沉淀选中」（selection 基建 95c8250 已就位），选材锁定为划选文本，直达知识卡/skill。

### 3.6 建议（后端补一条）
`suggestions.py` 增加 `_distill_suggestion`：多轮 + 工具调用多 + 未沉淀 + 非自动化会话 →
发 `action.type:"distill"`（客户端已实现，零前端改动）。

## 4. 后端契约

```
POST /api/sessions/{id}/distill/preview
  {kind: skill|kb|workflow|auto,
   scope: {type: all|range|turn|selection, from?, to?, turn_id?, text?}}
→ {ok, kind(resolved if auto), draft:{...}, meta:{
     suggested_name, name_valid, collisions, dedup?, landing, validation}}
# 不落盘；LLM 旁路生成（fork 语义）；auto=分类器只在此处落地

POST /api/sessions/{id}/distill/apply
  {kind, draft, edits}
→ {ok, path|name, reuse_hint}
# skill: _parse_skill_file 必过；kb: 落选定位（默认 Memory/）+ 去重结论随行；
# workflow: 转发现有 create_def(synthesis_id=...)；三者都写 distilled_to
```

- 草稿旁路：复用 `$.model.fork` 的机制在 server 侧直调（会话 checkpoint 为上下文、
  单发、不建影子会话、用量记账 source="distill"）。
- 命名规则统一：`sanitize_name`（installer.py:38 的 ASCII 规则）为唯一实现，CJK 由 LLM
  起英文名 + 描述保留原文。

## 5. 分期

| 期 | 内容 | 验收 |
|---|---|---|
| **P0（含三层入口）** | preview/apply 两段式 + 旁路 LLM 草稿 + 统一 DistillModal + distilled_to 回读 + skill 校验与命名统一；**会话菜单重做**（预览句/推荐徽标/已沉淀 ✓/吸收 SummarizeMenu）+ **turn hover** + **划选浮层** + 「编译为 Wiki」一键 | 三种形式都走「草稿→编辑→确认」；turn/划选两步内进草稿；断网给离线兜底 |
| **P1** | suggestions 后端补发（distill action）+ auto 分类细化 + msg_range 高级选材 | 建议条能触发；多轮多工具会话被推荐 |
| **P2** | 草稿队列（中断恢复）、skill 编辑器（Settings 补 body 查看/编辑） | — |

## 6. 风险与说明

1. 旁路 LLM 调用的成本与限流（每次预览一次 fork；推荐徽标用轻量启发式，不发 LLM）。
2. 建议条的触发阈值（多轮/多工具的判定）放 P1 实测调参，先保守。
3. SummarizeMenu 并入是破坏性 UI 变更：保留草稿恢复能力，入口只留沉淀菜单。
4. 知识卡维持 Raw/ 落位（决议）：「死知识」风险由两件事兜住——表单显式标注不可检索 + 保存反馈给一键「编译为 Wiki」；后续可观测沉淀→编译的转化率再决定是否改默认。