# Workflow UX Redesign — Implementation Notes

> 依据 docs/workflow-ux-redesign.md 实施。按 Sprint 1 → 2 → 3 顺序。

## Status

- [x] Sprint 1: P0+P1 核心执行体验
  - [x] 1. store.tsx WS handler：latestInterrupt + liveToolActivity
  - [x] 2. ProposeDiffCard.tsx（P0 version_propose 确认卡）
  - [x] 3. HumanInputCard.tsx（P1 human 问答卡）
  - [x] 4. RunBlocks.tsx：接入以上两个组件 + 工具调用行
  - [x] 5. RightDock.tsx：pendingHumanCount badge
  - [x] S1a. 聊天页 header 增加 SummarizeButton 入口
  - [x] S2. SummarizeModal 重写（DAG 预览 + 可编辑节点）
  - [x] S4a. 后端 _trace_text 智能截断
  - [x] S4b. 后端 DSL 生成重试循环
- [x] Sprint 2: P2
  - [x] 6. WorkflowLogTimeline：supervisor_intervene 样式（含 P3 #11）
  - [x] 7. WorkflowInspector：Supervisor 面板真实 UI
  - [x] 8. VersionHistoryDrawer + Inspector 版本入口
  - [x] 9. 后端 retry_from_checkpoint 端点（engine.continue_workflow + astream(None)）
  - [x] 10. RunErrorBox 从失败步骤重试按钮（LiveRunBlock/Panel/Chat/Inspector 全接入）
- [x] Sprint 3: P3
  - [x] 11. WorkflowsPage 搜索筛选（搜索框 + 全部/系统/用户 tabs）
  - [x] 12. ContextEditor 变量提示（{{context.x}} 扫描 + 填充状态 chips + 黄框高亮）
  - [x] 13. Browser 通知（store 状态 diff + visibilityState 判断 + 懒请求权限）
      + 自适应 stuck 阈值（runDurationByWorkflow 最近 10 次 ×3，fallback 120s）
  - [x] 14. WorkflowPanel 批量清除下拉（已完成/已失败/全部 inline confirm）
  - [x] 15. Settings DSL 编辑器（见 Deviation）

## 验证
- `npx tsc --noEmit`（apps/web）：通过
- `uv run pytest tests/api tests/unit`：665 passed（含新增 test_workflow_ux_new_features.py 12 例）
- `make app`：构建通过（exit 0）

## 端到端审计后的修复（2026-08-09 第二轮）

### 🔴 Bug 修复
1. **retry_from_checkpoint 新 run 永远 paused**：复制的 checkpoint 记录内嵌
   `session_id` 仍是源 run id，FileCheckpointer._write 按该字段定位路径 →
   续跑 checkpoint 全写回源 run 文件。修复：复制后把记录 `session_id`
   改写为新 run id（api/workflows.py retry_from_checkpoint 端点）。
   测试断言同步更新（原来断言的是 bug 行为）。
2. **HumanNode 步骤永远 pending**：HumanNode 不发 node_enter/exit，进度条
   永远 1/2。修复：_drive_run_events 在 interrupt 事件时置步骤 running、
   resume 事件时置 done（后端）；ChatStream run.event 同样处理（前端）。
3. **pending_interrupt 字段名不匹配**：后端打戳 `node_id`，前端读 `node`，
   导致问答卡不显示节点名、等待步骤不高亮。前端 types/RunBlocks 改为 node_id。
4. **聊天内嵌 run 卡 stuck 误报**：WS run.event 不刷新本地 `updated`，
   长步骤有工具活动也报卡住。修复：每个 run.event 同步 `run.updated`。

### 补齐的设计缺口
5. ProposeCard 决策后回执文字（「已应用 v3→新版本 / 已拒绝」4s 渐隐）
6. WorkflowLogTimeline supervisor_intervene 专属卡片样式
   （ShieldAlert + 橙容器 + action 分色 badge：coerce 绿/patch_dsl 紫/abort 红/skip 灰）
7. 未填模板变量时 Inspector「运行」按钮变 yellow outline（不禁用，tooltip 列变量）
8. Browser 通知点击 → 打开右栏 Workflow tab
9. RightDock 黄色 badge 加 animate-pulse
10. HumanInputCard question 支持 markdown（复用 chat/Markdown 组件）
11. S5 前端入口：summarize 下拉增加「范围」chips（全部/最近5/10/20条），
    runtime.ts summarizeSessionToDsl 传 last_n
12. S6 草稿恢复改为显式 banner（[恢复] / [忽略，重新总结]），不再静默恢复；
    草稿携带 sourceSessionId（恢复后 ↺重新总结 可用）
13. SummarizeModal：JSON 视图可编辑（parse 后回写 local）+ goal textarea 自动增高
14. session 下拉显示相对时间 +「（推荐）」标记
15. WorkflowDag 增加 interactive prop，Modal 预览禁用节点点击
16. VersionHistoryDrawer 版本行显示时间（后端 list_versions 返回文件 mtime ts）
17. 删除 /workflows 页旧「从会话总结」按钮（双轨入口收敛到聊天页）
18. createFromSummarize/openDevFromSummarize 补传 description
19. tool_call 批量调用显示最新一条（原取第一条）

### 已知保留的偏离（低优先级，记录不修）
- 「清除全部历史（含运行中）」：后端 cleanup 只清 terminal 状态且按钮在
  有 active run 时隐藏——运行中 run 本就不可清，语义改为「清除全部终态历史」
- Settings DSL 编辑器无语法高亮（零依赖决策保留）
- 自适应 stuck 为 run 级而非步骤级（无步骤时长数据源）
- dev 会话不带 workflow id 绑定（与 Inspector openDevSession 行为一致）

## 用户反馈修复（2026-08-09 第三轮）：总结流程切换 session 丢状态 + 不新增 workflow

**症状**：总结成流程后切换 session，按钮"卡住"（弹 banner 而不是正常总结），
且点创建后 workflow 列表不新增。

**根因**：
1. S6 草稿 banner 是**阻塞式**的——只要存在草稿（每次关闭 modal 都会存），
   之后每次点「总结成流程」都只弹 [恢复/忽略] banner，不做新总结。banner 无
   关闭键、且跨 session 持久（ChatStream 不重挂载）→ 用户切换 session 后按钮
   行为"不符合预期"，且永远到不了创建动作。
2. 创建成功后 modal 直接关闭、无明确回执，用户无法确认 workflow 是否真的新增。

**修复**：
- `openSummarize` 永远执行 fresh 总结，不再被草稿拦截；草稿恢复改为下拉菜单
  顶部的**显式 opt-in** 行（恢复草稿 · X 分钟前，含删除键），`savedDraft` 用
  useMemo 按需读 localStorage（draftTick 驱动刷新）。
- 移除阻塞 banner 及 `pendingDraft` 状态。
- 「仅创建」成功后保留 modal 显示绿色回执「已创建工作流「name」，可在
  Workflows 页查看与运行」+ 完成键（防重复创建）；「创建并运行」仍关闭并滑入
  run 卡。两条创建路径改 `await reloadWorkflows()` 保证列表即时刷新。
- 空 DSL（总结失败 `{}`）不再存成草稿（`hasNodes` 守卫），避免失败残留误导。
- SummarizeModal 增加 `useEffect([dsl])` 重同步 local/rawJson，修复 ↺ 重新总结
  时 modal 已挂载导致 useState(dsl) 不更新的陈旧问题。

## 用户反馈修复（2026-08-09 第四轮）：切菜单丢失所有进行中状态（根因：整页刷新）

**症状**：点「总结成流程」→ 按钮变「总结中…」→ 切到 Workflows/Settings 菜单
再切回 sessions → 按钮重置回「总结成流程」。（不止总结——所有 workspace 状态
都会丢，只是总结最容易被观察到。）

**根因**（与前端状态无关）：sidecar 的静态服务 `_serve_web` 不认识 Next 静态
导出的路由 RSC payload（`/workflows.txt`、`/settings/*.txt`）。App Router 软导航
fetch 这些 .txt 时拿到的是 SPA fallback 的 index.html → 路由放弃软导航，**回退
为整页重载** → 整个 React 树重挂载，ChatStream 的 sumLoading/弹窗/草稿全部丢失。
AppShell「hidden 保活」设计因此从未真正生效过。

**修复**（packages/runtime/src/ginno_runtime/server.py `_serve_web`）：
- 精确文件存在即直接返回（覆盖所有 .txt RSC payload 及导出内静态资源）；
- `.html`/`.txt` 一律 no-store（.txt 无内容哈希，构建间同名会变）；
- 解析路径做 WEB_OUT 包含性校验（防 %-decode 的目录穿越）；
- 其余行为不变（`/workflows` → workflows.html，未命中 → index.html SPA fallback）。

**验证**（Playwright 对真实 sidecar）：
- `/workflows.txt`、`/settings/model-api.txt` 返回真实 RSC payload（text/plain, no-store）；
- `%2e%2e` 穿越被挡（回 index.html）；
- 菜单跳转 / → /workflows → /settings → /kb → /workflows：window marker 存活
  （确认软导航，无整页重载）;
- 用户场景复现：假 provider（慢响应 8898 端口）让总结请求挂起 → 按钮显示
  「正在总结…」→ 切 /workflows → 点会话切回 → 按钮仍为「正在总结…」（修复前为
  重置）。注：测试中需 NO_PROXY=127.0.0.1，否则公司系统代理劫持回环流量回 502
  （测试环境特性，与产品无关）。
- 665 后端测试全过；make app exit 0。

## 用户反馈修复（2026-08-09 第五轮）：特定 session 总结失败（thinking 块）

**症状**：session 7700a4a8… 点「总结成流程」失败（HTTP 200 但 ok:false）。

**根因**：公司 hub 的 Anthropic 模型开了 extended thinking，`resp.content`
不是字符串而是块列表 `['', {'thinking': ...}, '<DSL JSON>']`。代码
`raw = resp.content if isinstance(...) else str(resp.content)` 得到 list 的
repr → `_extract_json_obj` 解析失败 → "model did not return a JSON DSL object"。

**修复**：增强 `graph.text_of_content`（跳过 thinking 块，支持 dict/对象块的
text 提取），替换所有 `str(content)` 同款站点：
- api/workflows.py：summarize 响应解析 + `_trace_text` 会话消息读取
  （历史消息 content 也可能是块列表，原来 trace 里混入 repr 噪音）
- workflows/nodes/builtin.py：AgentNode 结果、LLMNode 输出、tool_result 事件
- memory/summarize.py：记忆总结写入

**验证**：对 7700a4a8 会话真实复现——修复前 parsed=None，修复后产出 4 节点
合法 DSL（validate 无错）。新增回归测试：text_of_content thinking 场景 ×2 +
summarize API thinking 模型 ×1。668 测试全过；make app exit 0。

## Deviations
（记录偏离文档的决策）

- P0 确认卡：`workflow_propose_edit` 的 interrupt 发生在**会话图**（workflow-dev agent），
  不会发生在 workflow run 内（run 步骤中 workflow_* 工具被剥离）。ChatStream 已有
  propose 卡，本实现按设计做了打磨（busy 态、可折叠 diff、改动数统计），未新建
  LiveRunBlock 内嵌卡片（该场景实际不存在）。
- S1 入口：聊天页 composer 区已有「总结成流程」按钮，改造为带 session 选择下拉 +
  loading 态，未移到 session header（避免与现有布局冲突）。
- pending_interrupt：设计文档说放前端 store，实际改为**后端在 run JSON 上打戳**
  （`_set_run_pending_interrupt`），因为 WorkflowPanel 的 run 列表来自
  reloadWorkflowRuns 全量刷新，前端 store 方案会在刷新时丢失。
- S6 草稿持久化：与 Sprint 1 一起实现（改动极小，直接并入）。
- 「进入开发会话精炼」：先创建 workflow（v1），再 newSession("workflow-dev")，
  会话标题带 workflow 名；不做 workflow id 绑定（与 Inspector openDevSession 一致）。
- #15 DSL 编辑器：未引入 react-simple-code-editor/prismjs 新依赖，改为
  monospace textarea + 实时 JSON/结构校验提示（入口/节点校验），服务端创建时
  仍跑完整 validate_dsl。效果等价、零依赖风险。
- 自适应 stuck：按 workflow 级完成时长（最近 10 次 done run）×3 计算阈值，
  未做文档中「按步骤历史」的粒度（step 级时长目前无持久化数据源，run 级足够）。
- retry_from_checkpoint 引擎实现：新增 engine.continue_workflow，用
  graph.astream(None) 从 checkpoint 的 pending 节点续跑（LangGraph 对失败节点
  不提交 superstep，checkpoint 中保留 next），而非 Command(resume)。
