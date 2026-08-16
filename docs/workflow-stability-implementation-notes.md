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

## 已知保留（记录不修）

- `_run_agent_turn` 与 AgentNode 顺序路径重复（见 Deviation 1）。
- 并行 gather 的 usage 求和进单条 node_exit.usage；per-item 用量明细不入事件（成本核算粒度够用）。
- replay 脚本需真实 provider（opt-in 评测工具，CI 不跑）。
