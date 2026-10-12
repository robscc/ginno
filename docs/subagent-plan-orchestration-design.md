# 子代理计划编排（Plan Orchestration）设计

> 2026-10-12 立项。现状痛点：主 agent 只能逐个/一次性批量 spawn 子代理
> （ae01d586 实测手工分批 70+ 子会话），没有「清单」这个中间层——依赖顺序、
> 并发节奏、失败重试、进度总览全靠主 agent 人肉驱动。
>
> 新流程三段式：**主 agent 生成任务清单 → watcher 按清单调度子代理并回写
> 状态 → 主 agent 依据清单变化做决策**。

## 1. 角色与总体流程

```
主 agent（规划者/决策者）        Plan Watcher（编排器，新增）        子代理
────────────────────────       ─────────────────────────────       ──────
1. create_subagent_plan ──────▶ 生成 Plan（文件持久化）
   (goal/约束/验收/依赖/类型)      │
                                 ├─ 调度循环：
2.（空闲，等事件）                │   pending ∧ 依赖满足 ∧ 并发未满
                                 │   ──▶ spawn（复用 create_subagent）──▶ 执行
                                 │
                                 ├─ settle 钩子（复用 on_turn_settled）：
                                 │   item.status 回写 + plan.updated 事件
                                 │
3. ◀── 清单增量注入（去抖合流）──┤   item 终态（done/failed/stopped）
   验收结果 / amend 重试 /        │
   收尾报告                      ├─ 全部终态 → plan completed
   （可 amend_plan 增删改）──────▶│   + 注入完成总结，watcher 退出
```

三个角色：

- **主 agent**：规划与决策。工具面新增 `create_subagent_plan` /
  `amend_plan` / `cancel_plan`（后者用户命令 `/plan` 也可达）。不参与调度。
- **Plan Watcher**：sidecar 内 per-parent 后台任务（同 goal driver 的
  `_start_goal_driver` 模式）。只做三件事：按规则 spawn、回写状态、
  在恰当时机唤醒主 agent。**不自己做业务决策**（不自动重试、不自动改清单）。
- **前端 plan board**：`subagent.plan` 卡从「确认前的一次性快照」升级为
  「活的清单」——条目状态（复用五态/鸭子三色）、进度计数（3/12）、
  点条目跳子会话、清单级折叠。

## 2. Plan 数据模型

```jsonc
// ~/.ginno/projects/<slug>/sessions/<sid>/plans/<plan_id>.json
{
  "plan_id": "pl_xxx", "session_id": "<parent>", "title": "...",
  "status": "active | completed | cancelled",   // plan 级
  "version": 3,                                  // amend 计数（乐观锁）
  "created": 1791..., "updated": 1791...,
  "items": [{
    "item_id": "it_1",
    "goal": "...", "constraints": "...", "acceptance": "...",
    "agent_type": "implementer",        // 可空 = 默认
    "depends_on": ["it_0"],             // DAG；环在 amend/create 时校验拒绝
    "status": "pending | blocked | running | done | failed | stopped",
    "child_session_id": null,           // spawn 后回填
    "attempts": 1,                      // 同 item 重试计数
    "result_summary": "", "error": "", "settled_at": 0
  }]
}
```

要点：

- **文件持久化**（内存注册表 + 落盘，同 goal store 模式）。对比：
  `_PENDING_PLANS` 是内存的、重启即丢——已知缺陷，本设计不重蹈。
- 条目状态机：`pending →(依赖满足) running → done | failed | stopped`；
  依赖未满足的 pending 显示为 `blocked`（派生态，调度时推导，不落盘）。
  与子代理五态对齐，前端零新词汇。
- `child_session_id` 断链恢复：watcher 靠它把 item 与子会话 meta 对齐
  （重启恢复、状态校准都用这一条）。

## 3. Watcher 调度循环

per-parent 单任务，事件驱动 + 兜底轮询（10s）：

1. **可调度集合**：`pending ∧ depends_on 全终态 ∧ running 数 < max_concurrent`
   （复用全局并发帽 `max_concurrent()`，与直发 spawn 共用同一池——清单不能
   侵占直发的额度，反之亦然）。
2. **spawn 复用 `create_subagent`**（busy 检查、mods deny、深度上限、类型
   收紧全部继承，零新路径）。spawn 结果回填 `child_session_id` + `running`。
3. **settle 回写**：挂现有 `on_turn_settled`（子代理完成门已有）——按
   `child_session_id` 反查 item，写终态 + `result_summary`/`error`，
   落盘 + 推 `plan.updated` WS 事件（前端 board 实时刷）。
4. **完成判定**：全部条目终态 → `plan.status=completed` → 注入完成总结
   （§4）→ watcher 退出。
5. **停止语义**：用户 stop 子会话 → item `stopped`（终态）。`/plan cancel` /
   `cancel_plan` → 未启动条目直接 `stopped`，在跑的下发协作式停止
   （复用 stop_subagent_tree 语义但不动别张清单的子会话）。

## 4. 主 agent 的唤醒（去抖合流注入）

子代理成批完成时**不能每条唤醒一次父 turn**（10 条目同刻完成 = 10 次唤醒
= 10 次模型调用）。合流规则：

- settle 事件先进 per-plan 缓冲；**去抖窗口 8s**（无新 settle 即冲刷；
   窗口内再来则顺延，最长 30s 强制冲刷——防长尾批次粘连）。
- 冲刷时合并成**一条**增量注入，复用 `_inject_result` 的 steering 信封：

```
<ginno_plan_update plan_id=pl_xxx version=5>
清单变更（自上次注入以来）：
- [done]   it_3 建档批次03代理档案 —— 摘要…
- [failed] it_4 建档批次04 —— 错误…
- [running→] it_5, it_6 仍在跑
进度：6/12 完成，1 失败。
你可以：验收上述结果（对照 acceptance）；amend_plan 重试/替换失败条目；
全部完成后输出收尾报告。除非需要用户决策，否则自主推进。
</ginno_plan_update>
```

- 注入走父 turn 的既有通道：父空闲 → invoke 新 turn；父运行中 → steering
  （与现有子代理结果注入同路径，前端同款渲染，零新管线）。
- 信封文本即「行动菜单」：不新增决策工具，靠提示词引导（验收/amend/收尾）。

## 5. 与现有路径的关系

| 现有路径 | 处置 |
|---|---|
| `/subagent split <task>`（用户拆解） | 保留确认卡；确认后**进入 watcher 调度**（不再一次性全 spawn）——依赖/并发由此接管。`_PENDING_PLANS` 确认后转为持久化 Plan |
| `spawn_subagent`（agent 直发） | 保留为零开销路径（单任务不值得建清单）；不强行归一 |
| goal driver | 并存：goal 驱动父，plan 编排子。一个 goal 下可用多张清单 |
| delegation（claude-code/pi） | 清单项可指定 `backend` 字段走委托执行器（v1 决策点 D1） |

## 6. 恢复与边界

- **sidecar 重启**：扫 plans 目录 → active 清单重建 watcher；running 条目按
  子会话 meta 实况校准（子会话已终态 → 回写；仍跑 → 继续等 settle）；
  孤儿 pending 继续调度（默认；设置 `plan_orchestration.resume` 可关）。
- **父会话删除**：既有 parent_session_id 级联删子会话；watcher 随之取消，
  plans 目录随 session 目录保留（与其他 session 数据同生命周期）。
- **深度**：清单项 spawn 走同一深度上限（item 的子代理仍可再委派，帽不变）。

## 7. 决策点（待拍板）

- **D1 delegation 作为执行器**：v1 是否支持 item.backend（pi/claude-code）？
  建议：支持——字段先进，watcher 分支后补（delegation 是进程等待不是 turn，
  settle 钩子挂 delegation 终态事件即可）。
- **D2 agent 自建清单的启动门槛**：`create_subagent_plan` 是否需要用户确认
  才开始调度？建议：默认自动启动（agent 模式的意义就在自主）；设置
  `plan_orchestration.confirm_agent_plans` 开关，默认关。用户路径（split）
  保留确认。
- **D3 失败重试**：watcher 自动重试（max_retries）还是只报告、等主 agent
  amend？建议 v1 只报告（决策留在 agent；amend 复位 item → pending 即重试，
  attempts 计数防无限循环）。
- **D4 唤醒粒度**：逐条 / 去抖合流 / 仅清单完成。建议去抖合流（8s/30s，
  §4）；「仅完成」太迟（失败要等全清才能纠偏）。

## 8. 实施阶段

- **P1 数据层**：Plan 存储/状态机 + API（GET /plans/{sid}）+ `plan.updated`
  事件。可独立合入。
- **P2 Watcher**：调度（依赖+并发帽）+ settle 回写 + 重启恢复。
- **P3 唤醒闭环**：去抖合流注入 + `ginno_plan_update` 信封 + 前端 plan board
  （plan 卡升级、鸭子状态点）。
- **P4 收口**：split 确认接入 watcher、amend 工具、设置开关、delegation
  执行器（若 D1 通过）。

每阶段可独立验证：P1 后清单可手工建/查；P2 后清单自动跑但不唤醒；
P3 后闭环；P4 全量替换旧路径。
