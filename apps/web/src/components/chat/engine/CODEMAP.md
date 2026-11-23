# CODEMAP — handle() 事件分发全量盘点（77 事件 × 15 模块）

> 对象：`apps/web/src/components/chat/engine/`。改造前 handle.ts 2217 行、
> 77 个 `case "` 标签（`grep -c` 计 79 = 77 标签 + 2 处注释提及）；
> 批次 6-2 拆为 40 行薄入口 + `handlers/` 15 个分组模块（连续切段，
> 事件序 = 原 case 序），行为零变化（93 契约用例 + 1729 全量 + token 级对照）。
> 本表是 handleDispatch.test.ts ②③层的人类可读版。
>
> 表格口径：**处理语义** = 事件到达后 reducer 做什么（一行）；
> **状态副作用** = 写哪些状态面（store 气泡 / refs / ctx.g.* 回调 /
> notice·OS 通知 / 模块级总线）。未列 = 无。

## 分组总览（15 模块 · 77 事件）

| 模块 | 事件数 | 语义边界 |
|---|---|---|
| handlers/01-stream.ts | 12 | 流式帧 → live 气泡块（token/thinking 撤行、tool 共享体、问卡独立通知） |
| handlers/02-turn-start.ts | 1 | turn 起点/探针回答/权限徽标 |
| handlers/03-store-reload.ts | 9 | 会话条目与 store 重载回流 |
| handlers/04-runs.ts | 7 | workflow run 实时镜像 |
| handlers/05-workers.ts | 4 | worker 三态公告 |
| handlers/06-steer.ts | 5 | 运行中注入四态 |
| handlers/07-runtime-events.ts | 5 | dev-swap 热切换 + 遗言恢复（跨会话广播） |
| handlers/08-inline.ts | 2 | 内联弹层与审批数据源 |
| handlers/09-goal-team.ts | 7 | goal 归档/清除 + AgentTeam 聚合 |
| handlers/10-subagent.ts | 4 | 子代理生命周期 + 委托进度 |
| handlers/11-context.ts | 5 | 上下文窗口状态（WorldState 公告 + 压缩三态） |
| handlers/12-usage.ts | 1 | 用量与模型计量 |
| handlers/13-turn-end.ts | 4 | turn 正常收口/停止/失败/清空（组内事件最多） |
| handlers/14-team-replies.ts | 2 | AgentTeam 回执/回帖 |
| handlers/15-mods.ts | 9 | mods 兼容层事件 |

## 01-stream（12）— handlers/01-stream.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| token.delta | 文本增量落 live 气泡；恢复出流时撤 auto-retry 瞬态提示 | store 气泡 text 块；retryNoticeRef |
| thinking.delta | 思考增量落 live 气泡（与 token.delta 共享撤行+帧体） | store 气泡 thinking 块；retryNoticeRef |
| tool.start | 工具调用开跑：pending tool 块（与 args/end 共享帧体） | store 气泡 tool 块（pending） |
| tool.args | 工具参数预览（与 start/end 共享帧体） | store 气泡 tool 块 argsPreview |
| tool.args_delta | pending 参数累计快照，以调用 ID 更新 | store 气泡 tool 块 `argsStream` |
| tool.end | 工具调用收口：内容回填、pending 清除 | store 气泡 tool 块（完成态） |
| user.question | ask_user 挂起卡（独立 case 体，P0-1 结构防线） + OS 通知（去重后即时弹） | store 气泡 question 块；notifyNative(askUser)；sessionStorage 去重集 |
| widget.emit | widget 块合并进 live 气泡（三键一体：widget/ref/image） | store 气泡对应块 |
| ref.emit | 引用块（三键一体） | store 气泡 ref 块 |
| image.emit | 图片块（三键一体） | store 气泡 image 块 |
| llm.request | llm_call 行 pending（与 response 共享体，按 req_id+attempt 配对） | store 气泡 llm_call 块 |
| llm.response | llm_call 行原地完成 | store 气泡 llm_call 块 |
| workflow.emit | workflow 卡片帧（终态通知由 store 侧负责，不重复弹） | store 气泡 workflow 块 |

## 02-turn-start（1）— handlers/02-turn-start.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| turn.start | 起点全量初始化：busy 置位、agent/turnId 采纳、turn-meta t0、乐观气泡 turnId 同步、渠道轮次回显（echo_user 折卡防重） | busy/seenTurnStart/streamAgent refs；store 气泡元数据；applySessionPatch(updated)；turn_meta 块 |

## 03-store-reload（9）— handlers/03-store-reload.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| permission.request | 工具审批弹层数据源（React 同步在 engine 层读 ref） | permsRef[sid] |
| version.propose | workflow 版本提案弹层数据源 | proposeRef[sid] |
| todos.changed | 刷 TODO 面板 | ctx.g.reloadTodos |
| skills.changed | 刷 slash 菜单技能表 | ctx.g.reloadSkills |
| memory.changed | 刷 Memory 角标 | ctx.g.reloadMemoryBadge |
| agents.changed | 刷 picker/mention 列表 | ctx.g.reloadAgents |
| session.model_changed | 模型热切回流：patch 会话条目 + 轻提示 toast（不进收件箱） | applySessionPatch(provider/model/thinking_effort)；pushNotice(model) |
| workflows.changed | 刷 workflow 定义 + 运行列表 | ctx.g.reloadWorkflows/reloadWorkflowRuns |
| synthesis.event | 刷总结面板；命中挂起等待即 resolve | ctx.g.reloadSynthesisCases；ctx.finishSynthesisWait |

## 04-runs（7）— handlers/04-runs.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| run.bind | run 卡首次出现：拉全量 + 侧栏伪条目 | getWorkflowRun→runsBySessionRef；reloadWorkflowRuns |
| run.event | 逐事件镜像：tool_call 活跃可见 / node 状态 / interrupt·resume·error 即时盖章 / _touch_run | runsBySessionRef（steps/pending_interrupt/error_detail）；notifyRunToolActivity |
| run.status | 终态换挡（paused 携带 interrupt 缘由）+ 列表刷新 | runsBySessionRef；reloadWorkflowRuns |
| artifacts.changed | 刷会话内 + 跨会话产物列表 | ctx.g.reloadArtifacts/reloadProjectArtifacts |
| code.changed | 代码面板版本基准回流（write/edit 带后置版本） | ctx.g.notifyCodeChange |
| preview.emit | 可预览产物：打开预览 + 刷列表 | ctx.g.openPreview；reloadArtifacts |
| preview.invalidate | 被盯文件变更：FileViewer 重取 | ctx.g.notifyPreviewInvalidate |

## 05-workers（4）— handlers/05-workers.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| worker.waiting | 等待可见化三态之一：订阅建立（三键一体，体按 ev.event 分支） | pushNotice(info, dedupeKey 按事件+目标) |
| worker.progress | digest 进度节选到达（非完成） | pushNotice(info) |
| worker.resumed | 终态结果注入 | pushNotice(info) |
| linked.done | 订阅会话 turn 完成：📡 前缀进 reply 合并窗（与 message.end 同窗同冷却） | replyMerge.push(targetSid)；shouldNotify 门 |

## 06-steer（5）— handlers/06-steer.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| steer.recalled | ↑ 取回对账：命中的条目弹回 composer | settleRecalled |
| steer.accepted | 服务端收讫：条目翻出 sending（与 answered 体一致，合并注册） | steerQ.handleEvent |
| steer.answered | 当作 parked 答案直接恢复 turn：条目收走 | steerQ.handleEvent |
| steer.absorbed | 注入带落地三通路：task_event 折行 / 信封折卡（sessionId 幂等）/ 通用 steer 带（含附件）；recursion-warn 隐藏 | steerQ.handleEvent；store task_event·subagent_result·steer 块；syncDisplay |
| delegate_update | 委托实时进度系统行：同 id 刷新，running=false 撤行 | store delegate-live-* 行；syncDisplay |

## 07-runtime-events（5）— handlers/07-runtime-events.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| session.state_changed | P1 状态机：compacting 置位 / idle 解除（按事件内 session_id） | compactingBySessionRef；syncDisplay |
| runtime.devswap_progress | 热切换阶段事件推模块级总线（非本会话帧，不 markDelivered） | pushDevSwapRun |
| runtime.lastwords_pending | 遗言恢复决策卡（常驻 + 恢复/忽略按钮真调 API）+ OS 通知（askUser 类即时弹） | pushNotice(kind:lastwords,duration:0)；notifyNative(askUser)；resume/dismissLastwords |
| runtime.lastwords_restored | 恢复回执收件箱条目（跳回原会话） | pushNotice(kind:lastwords) |
| runtime.dev_swap_pending | 热切换确认条点亮（auto=true 直接 invoke finish_dev_swap） | setDevSwapPendingConfirm；markDelivered；invoke(finish_dev_swap) |

## 08-inline（2）— handlers/08-inline.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| deliver.report | @session 投递回执：系统提示条（目标状态行 + 附件注明） | store 气泡 text 块；markDelivered |
| notice | 内建命令回执落 live 气泡一段 delta；auto_retry/model_error_backoff 瞬态提示原位替换（分隔符精确进出） | store 气泡 text 块；retryNoticeRef；markDelivered |

## 09-goal-team（7）— handlers/09-goal-team.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| goal.updated | goal 快照 → TopBar chip | ctx.g.notifyGoal |
| team.changed | 聚合状态回流；成员转入 blocked 时通知一次（跃迁去重） | ctx.g.notifyTeamUpdated；pushNotice(warn)；notifyNative(team)；_teamBlockedSeen |
| plan.updated | 任务清单快照 upsert | ctx.g.notifyPlanUpdated |
| goal.archived | 完成归档：chip 清空（fall-through 与 cleared 共享体，注册表两键一函数） | ctx.g.notifyGoal(null) |
| goal.cleared | goal 清除：chip 清空 | ctx.g.notifyGoal(null) |
| session_title | 自动标题回流 | applySessionPatch(title/title_auto=false) |
| session.context | 挂载集变化回流 | applySessionPatch(context_folders/primary_folder) |

## 10-subagent（4）— handlers/10-subagent.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| subagent.spawned | 子会话行 upsert；本会话是发起父时追加发起卡（delegation 型不加卡） | ctx.g.notifySubagentSpawned；store subagent_spawn 块 |
| subagent.status | 子会话行状态换挡；终态（done/failed）且是发起父时补结果卡（按 sessionId 去重） | ctx.g.notifySubagentStatus；store subagent_result 块 |
| subagent.plan | 拆分方案卡：pending 卡就地替换，否则新系统行 | store subagent_plan 块 |
| subagent.plan.cancelled | 另一窗口取消：pending 卡翻 cancelled（幂等） | store subagent_plan 块状态 |

## 11-context（5）— handlers/11-context.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| context.updated | WorldState 公告：非 environment 变化落居中 context 行（可带 i18n） | store context 块；syncDisplay |
| context.microcompacted | 旧工具输出清占位公告（恒可见） | store context 块 |
| context.compacting | 压缩瞬态镜像：{stage,pct,predicted_s} 置位 / stage=done 自清（与 handle() 前置段协议耦合） | compactingBySessionRef；compactStageRef |
| context.compacted | 压缩完成公告（恒可见） | store context 块 |
| history.cleared | /clear：本地 transcript 整体丢弃 + 流状态复位 | store=[]；live/streamAgent/busy/orphan/retryNotice refs |

## 12-usage（1）— handlers/12-usage.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| usage | 逐调用 usage：turn 增量累加 turn-meta（按事件 sid）+ TopBar 圆环（仅活跃会话，防多会话混用） | turn_meta tokens；onUsageChange/onContextChange（active 守卫） |

## 13-turn-end（4）— handlers/13-turn-end.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| turn.state | 重连探针回答：running 镜像、compacting 探针例外、死 turn 的 history 对账 + steer 队列提升 | serverRunningRef；compacting 镜像（探针）；reconcileTurnFromHistory + flushSteerQueue |
| message.end | 正常收口：清流状态、盖收口时间戳、turn-meta 定格（meta 权威值/本地兜底）、未读点 + reply 合并窗（origin 门）、orphan 对账 + steer 队列提升 | live/busy/orphan/adopted/serverRunning refs；store ts/turn_meta；markSessionUnread·clearSessionUnread；replyMerge.push |
| turn.stopped | 手动停止：像 end 收流 + 像 error 收口 pending 块，但无错误卡无完成通知；steer 队列弹回 composer；清 parked 提示 | 同上 refs；permsRef/proposeRef 置 null；setPermission/setPropose(null)；closePendingBlocks；recallSteers |
| error | 失败收口：失败即时通知（不合并）、live 气泡标 failed、pending 块收口、错误卡（含重试快照 sendPayload）、steer 队列提升 | 同上 refs；notifyNative(failure)；markSessionUnread；store error 卡 |

## 14-team-replies（2）— handlers/14-team-replies.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| team.delivered | 投递回执三态（delivered/stalled/failed 严格区分，多回执保留最严重）+ 防御性收口 + 空占位气泡丢弃（turnId 兜底） | 用户消息 teamDelivery/status；busy/serverRunning/live refs；store 过滤空气泡；markDelivered |
| team.reply | 成员回帖：完成态 assistant 消息（message_id 去重）+ 通知（与 reply 同门） | store 回帖消息（teamReply 元数据）；pushNotice(info)；notifyNative(team) |

## 15-mods（9）— handlers/15-mods.ts

| 事件 | 处理语义 | 状态副作用 |
|---|---|---|
| mod.bands | 命令带整树替换（per-mod 分段 + 旧单树形状兜底） | ctx.setModBands |
| mod.toast | 收编全局 notice 总线（无 dedupeKey，多条并存） | pushNotice(source:mod) |
| mod.state.changed | 设置页重拉信号 | window CustomEvent ginno:mods-state-changed |
| mod.prompt.fill | 接管 composer 草稿（覆盖语义） | window CustomEvent ginno:mod-fill-input |
| mod.prompt.suggest | 幽灵建议（composer 空才落字） | window CustomEvent ginno:suggest-input |
| mod.ask | 问题浮层（choices 兼容 string/{label}/{value}） | ctx.setModAsks |
| mod.pane.open | 右栏 pane 开（三键一体，体按 ev.event 分支） | pushModPane |
| mod.pane.update | pane 更新 | pushModPane |
| mod.pane.close | pane 关 | pushModPane |

