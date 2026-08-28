# Workflow 稳定性 + 容错 + 并行 — Implementation Notes

> 依据批准的改造计划（P1–P3 一次交付 + P4 评测件）实施。起源：
> docs/workflow-openhuman-comparison.html 附A 的 DSL 深度对比与借鉴清单。
> 本文记录落地内容与偏离，风格同 workflow-ux-implementation-notes.md。

## Status

- [x] P1a doctor 接入 `_run_synthesis` 重试回路（errors 回喂、warnings 透出、`fail_stage=doctor.<rule>`、synth-4→synth-5）
- [x] P1b 节点契约 catalog（`workflows/contracts.py` 单一事实源；synthesis prompt 惰性拼装；workflow-dev 种子 prompt 缩短 + `graph.build_stable_system` 运行时注入；loop parallel 契约行）
- [x] P1c propose_edit 接 doctor（errors 阻断、warnings 进 interrupt/rationale）
- [x] P1d `POST /api/workflows/dry-run`（normalize+validate+doctor+compile+可达性，零 LLM）
- [x] P1+ workflow-dev：step 缺省 `agent` 默认 dev（契约与种子 prompt 写明，运行时 resolve_agent 本就回退 dev）
- [x] P2a DSL 容错字段（retry/timeout_s/on_error）validate 规则 + 多出边显性化（validate error + doctor `edges.multi_out.unsupported` + `workflow_strict_multi_edge` flag）
- [x] P2b `BaseNode._execute_resilient`：retry/backoff(封顶30s)/timeout_s/on_error continue 软失败；排除 CancelledError/GraphBubbleUp/GraphRecursionError/SupervisorAbort
- [x] P2c `_drive_run_events` handled-error 分流（步骤 failed + run.warnings，不翻 run 状态）+ done 推送带 warnings + store 初始 warnings:0 + engine 三入口显式 recursion_limit
- [x] P3a loop.parallel：validate（body 必须 step/agent 且 writes 全 array、max_concurrency 1..8）+ doctor 两规则 + compiler 跳过 parallel body 的 __extract + steps_from_dsl 同步 + `workflow_parallel_loops`/`GINNO_WF_PARALLEL` 双闸门
- [x] P3b 运行时 gather：LoopNode 首轮交整表/回轮收尾、flag 关降级顺序+warning；`_execute_parallel_body`（Semaphore 限并发、per-item retry/on_error、per-item 内联 extract、index 序数组一次写回、单对 node_enter/exit、per-item loop_iter、loop_item_error）；`extract.py` 重构出 `extract_from_text` 共用
- [x] P3c 前端：WorkflowLogTimeline KIND_STYLE/EXPANDABLE/fmt（node_retry/loop_item_error/warning/并行文案）；RunBlocks 终态 ⚠ warnings 计数；types warnings 字段
- [x] P4 stats `by_prompt_version` 分组 + `scripts/synth_replay_batch.py` 回放批处理

## 验证

- `uv run pytest tests/unit tests/api`：862 passed（新增 test_workflow_retry 8 + test_workflow_parallel 10 + test_synthesis_stats_grouping 1 + 既有修正）
- `npx tsc --noEmit`（apps/web）：通过

## Deviations（与计划的偏离与决策）

1. **AgentNode 顺序路径保留自己的 turn 循环**：`_run_agent_turn` 为并行适配器抽出，顺序 execute 暂未切换复用它（减少并发编辑期风险）；合并去重列为后续清理项。
2. **per-item loop_iter 由 gather 适配器发出**（node_id=body、带 parallel/index），LoopNode parallel 分支不再发批量单条——时间线能逐项呈现启动，优于计划原文。
3. **无独立 on_item_error 字段**：per-item 失败策略复用 body 节点的 retry/on_error（stop/continue），DSL 面更小；语义等价计划目标。
4. **parallel 下 max_iters 不封顶 gather**：并行批处理 len(items) 全集；max_iters 仍约束顺序降级路径。validate 不交叉校验两者。
5. **trending e2e fixture 补 `context.initial.repos=[]`**：synth-5 起 doctor 回喂会拦下无来源 loop.over 的草稿；补 initial 后草稿 doctor-clean，而 run#1 的 repos/repositories 运行时错配叙事保留（0 迭代 → dev 会话修复）。
6. **暂停语义**：parallel body 内 item 间 check_pause；暂停恢复=整批重跑（与现行步中暂停同级，文档化接受）。per-item checkpoint 粒度依赖 FileCheckpointer pending_writes 升级，独立后续项。
7. **dry-run 与 propose_edit 的 doctor 接线由协作会话先落地**，本文确认其行为与计划一致（dry-run 含 unreachable 可达性集合）。

## Follow-ups（2026-08-17，两项保留项修复）

1. **顺序路径去重**：AgentNode.execute 顺序路径切换为复用 `_run_agent_turn`，
   删除第二份 turn 循环。事件顺序微差：agent 回退 warning 事件现在位于
   tool_call/tool_result 流之后（原先在 node_enter 之后），内容不变。
2. **FileCheckpointer pending_writes 升级 + per-item checkpoint 粒度**：
   - 构造参数 `surface_pending_writes`（engine 四入口 True；chat 默认 False，
     维持 pre-E5 的 `pending_writes=None` 字节级语义）。开启后 get_tuple 返回
     存储的 put_writes（langgraph 标准 mid-step resume：中断 superstep 内已完成
     任务不重跑），但**过滤 app 级通道** `_APP_PENDING_CHANNELS={"parallel_progress"}`。
   - 新 `get_app_pending(config, channel)`：节点适配器读回自己经 put_writes 持久化
     的增量进度（last-write-wins）。
   - gather 适配器每完成一个 item 即 put_writes 进度；激活重跑（pause/resume、或
     retry 拷贝 checkpoint 文件——pending writes 随文件带走）时按 index 恢复并跳过
     已完成 item，loop_iter 事件带 `resumed:true`；批量提交后进度清空。
   - **关键取舍（langgraph 语义陷阱）**：不能把 per-item 进度暴露给 pregel——
     langgraph 把「有 pending writes 的 task」视为已完成并在 resume 时跳过，
     body 会被整体跳过、剩余 item 永不执行。故进度通道对 pregel 隐藏、对适配器可见。
   - 测试：test_checkpointer_pending_writes（3）、test_parallel_resume_skips_done_items（2）。

## Follow-ups（2026-08-28，收尾轮）

对照批准计划的测试清单与 P1d 原文，补齐两处遗留：

3. **测试清单 #23 落地** — 新 `tests/e2e/test_workflow_parallel_pipeline.py`：
   prep（WRITE_JSON 快路径写 themes）→ parallel loop（gather，
   max_concurrency:1 保证脚本序确定）→ 下游 step 消费。覆盖：单对
   node_enter/exit 包整批、per-item loop_iter（parallel/of/index）、
   两个 item 走 WRITE_JSON 快路径、**一个 item 只回散文 → per-item 内联
   `extract_from_text` LLM 修正路径**（抽取 prompt 带来源文本，全链路恰好
   一次）、index 序 array 合成（context.reports）、下游 goal 按序读到三个
   标题。另含编译器形状断言：顺序节点注入 `__extract`、parallel body 跳过；
   parallel DSL 经 API 建为 v1。
4. **P1d 接线补齐**（原交付只有端点，UI/工具接线在 8/17 被静默落下）：
   - 新 `workflows/dryrun.py::dry_run_dsl` 单一事实源；
     `POST /api/workflows/dry-run` 改为薄封装（行为不变，既有 7 例 API 测试通过）。
   - dev agent 新工具 `workflow_dry_run(workflow_id | new_dsl_json)`：
     propose_edit 前自检草稿（零成本、无副作用、可自主调用——对齐借鉴清单
     「允许自主 dry-run、禁止自主真跑」）。`WORKFLOW_DEV_TOOL_NAMES` /
     `ALL_WORKFLOW_DEV_TOOLS` 收录；种子 prompt 更新；
     `ensure_workflow_dev_tools()` 幂等迁移旧装机（新 `test_workflow_dry_run_tool.py` 8 例）。
   - 前端：SummarizeModal「试运行」按钮（检查当前编辑草稿，绿/红回执；
     草稿一改动回执自动失效）；WorkflowInspector「试运行」按钮（检查已存
     DSL，回执带版本号，rollback/propose 后失效）。`lib/runtime.ts` 增
     `dryRunWorkflow` + `DryRunResult`。
5. **Debug 模式入库**（与本分支无关但同期遗留）：8/16 实现的浏览器
   Debug 门控当时未提交，且 51ac613 意外卷走其中三文件的守卫、使 HEAD
   悬空依赖未跟踪的 `debug.py`。29fad57 补齐：`debug.py` + 全部守卫 +
   `test_debug_mode.py`（9 例）。

## 验证（2026-08-28 收尾轮）

- `uv run pytest tests`（unit+api+e2e 全量）：**997 passed, 1 skipped**。
- `npx tsc --noEmit`（apps/web）：通过。
- `make sidecar`（web 静态导出 + PyInstaller onedir bundle + staging）：通过。
  `make app` 的 Tauri bundle 步骤需先退出正在运行的 Ginno.app（避免文件锁），
  由用户择机执行。

## 已知保留（记录不修）

- 并行 gather 的 usage 求和进单条 node_exit.usage；per-item 用量明细不入事件（成本核算粒度够用）。
- replay 脚本需真实 provider（opt-in 评测工具，CI 不跑）。
- 计划「验证」一节的真实-provider 冒烟（summarize→doctor 回喂、坏工具
  retry→continue）与 Playwright 渲染抽查：前者被 synth_replay_batch（需真实
  provider）覆盖为 opt-in；后者所需的事件渲染逻辑已有单测/时间线组件覆盖，
  端到端抽查随下次 make app 一并做。
