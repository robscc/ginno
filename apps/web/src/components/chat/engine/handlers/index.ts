// handlers/ — handle() 大 switch 的分组注册表（批次 6-2 等价迁移，替代缺失的
// 6-1 基建由本分支一并补齐）。15 个分组模块按原 switch 的 case 顺序**连续切段**
// （组间不交叉），本文件集中登记：事件名 → handler。
//
// 验收基准：handleDispatch.test.ts ①层的事件全集枚举快照以本文件为提取源
// （["event", handler] 条目顺序 = 原 switch case 顺序）。
// handler 签名统一 (ctx, sid, ev)：共享状态（store refs / 通知出口 / 调度动作）
// 全部经 EngineCtx 传递；模块自身只允许模块级单例（与拆分前同生命周期）。

import type { EventHandler } from "./types";
import * as stream from "./01-stream";
import * as turnStart from "./02-turn-start";
import * as storeReload from "./03-store-reload";
import * as runs from "./04-runs";
import * as workers from "./05-workers";
import * as steerEvents from "./06-steer";
import * as runtimeEvents from "./07-runtime-events";
import * as inline from "./08-inline";
import * as goalTeam from "./09-goal-team";
import * as subagent from "./10-subagent";
import * as contextEvents from "./11-context";
import * as usage from "./12-usage";
import * as turnEnd from "./13-turn-end";
import * as teamReplies from "./14-team-replies";
import * as mods from "./15-mods";

export const EVENT_HANDLERS: ReadonlyMap<string, EventHandler> = new Map<string, EventHandler>([
  // ── 01 stream 帧（token/thinking 前置撤行 + tool 共享体；问卡独立通知）──
  ["token.delta", stream.onDelta],
  ["thinking.delta", stream.onDelta],
  ["tool.start", stream.onStreamFrame],
  ["tool.args", stream.onStreamFrame],
  ["tool.end", stream.onStreamFrame],
  ["user.question", stream.onQuestion],
  ["widget.emit", stream.onStreamFrame],
  ["ref.emit", stream.onStreamFrame],
  ["image.emit", stream.onStreamFrame],
  // llm.* 独立注册（不进 tool 5 事件共享体——notify 风暴教训，体同 plain 帧）
  ["llm.request", stream.onStreamFrame],
  ["llm.response", stream.onStreamFrame],
  // workflow.emit：run 终态通知由 store/notifications.ts 统一发，此处只落块
  ["workflow.emit", stream.onStreamFrame],
  // ── 02 turn 起点 ──
  ["turn.start", turnStart.onTurnStart],
  // ── 03 弹层请求与 store reload ──
  ["permission.request", storeReload.onPermissionRequest],
  ["version.propose", storeReload.onVersionPropose],
  ["todos.changed", storeReload.onTodosChanged],
  ["skills.changed", storeReload.onSkillsChanged],
  ["memory.changed", storeReload.onMemoryChanged],
  ["agents.changed", storeReload.onAgentsChanged],
  ["session.model_changed", storeReload.onSessionModelChanged],
  ["workflows.changed", storeReload.onWorkflowsChanged],
  ["synthesis.event", storeReload.onSynthesisEvent],
  // ── 04 workflow 运行卡与产物/代码/预览 ──
  ["run.bind", runs.onRunBind],
  ["run.event", runs.onRunEvent],
  ["run.status", runs.onRunStatus],
  ["artifacts.changed", runs.onArtifactsChanged],
  ["code.changed", runs.onCodeChanged],
  ["preview.emit", runs.onPreviewEmit],
  ["preview.invalidate", runs.onPreviewInvalidate],
  // ── 05 worker_bus 等待可见化与订阅完成 ──
  ["worker.waiting", workers.onWorkerNotice],
  ["worker.progress", workers.onWorkerNotice],
  ["worker.resumed", workers.onWorkerNotice],
  ["linked.done", workers.onLinkedDone],
  // ── 06 steering 协议与委托进度 ──
  ["steer.recalled", steerEvents.onSteerRecalled],
  ["steer.accepted", steerEvents.onSteerAccepted],
  ["steer.answered", steerEvents.onSteerAnswered],
  ["steer.absorbed", steerEvents.onSteerAbsorbed],
  ["delegate_update", steerEvents.onDelegateUpdate],
  // ── 07 会话状态机与 sidecar 运行时事件 ──
  ["session.state_changed", runtimeEvents.onSessionStateChanged],
  ["runtime.devswap_progress", runtimeEvents.onDevswapProgress],
  ["runtime.lastwords_pending", runtimeEvents.onLastwordsPending],
  ["runtime.lastwords_restored", runtimeEvents.onLastwordsRestored],
  ["runtime.dev_swap_pending", runtimeEvents.onDevSwapPending],
  // ── 08 无 graph turn 的内联文本 ──
  ["deliver.report", inline.onDeliverReport],
  ["notice", inline.onNotice],
  // ── 09 goal/plan/team 聚合与会话元数据 ──
  ["goal.updated", goalTeam.onGoalUpdated],
  ["team.changed", goalTeam.onTeamChanged],
  ["plan.updated", goalTeam.onPlanUpdated],
  ["goal.archived", goalTeam.onGoalCleared],
  ["goal.cleared", goalTeam.onGoalCleared],
  ["session_title", goalTeam.onSessionTitle],
  ["session.context", goalTeam.onSessionContext],
  // ── 10 子代理生命周期 ──
  ["subagent.spawned", subagent.onSubagentSpawned],
  ["subagent.status", subagent.onSubagentStatus],
  ["subagent.plan", subagent.onSubagentPlan],
  ["subagent.plan.cancelled", subagent.onSubagentPlanCancelled],
  // ── 11 上下文公告与清屏 ──
  ["context.updated", contextEvents.onContextUpdated],
  ["context.microcompacted", contextEvents.onContextMicrocompacted],
  ["context.compacting", contextEvents.onContextCompacting],
  ["context.compacted", contextEvents.onContextCompacted],
  ["history.cleared", contextEvents.onHistoryCleared],
  // ── 12 token 用量 ──
  ["usage", usage.onUsage],
  // ── 13 turn 收口（探针/end/stopped/error）──
  ["turn.state", turnEnd.onTurnState],
  ["message.end", turnEnd.onMessageEnd],
  ["turn.stopped", turnEnd.onTurnStopped],
  ["error", turnEnd.onError],
  // ── 14 AgentTeam 回执与回帖 ──
  ["team.delivered", teamReplies.onTeamDelivered],
  ["team.reply", teamReplies.onTeamReply],
  // ── 15 Claude Code Mods ──
  ["mod.bands", mods.onModBands],
  ["mod.toast", mods.onModToast],
  ["mod.state.changed", mods.onModStateChanged],
  ["mod.prompt.fill", mods.onModPromptFill],
  ["mod.prompt.suggest", mods.onModPromptSuggest],
  ["mod.ask", mods.onModAsk],
  ["mod.pane.open", mods.onModPane],
  ["mod.pane.update", mods.onModPane],
  ["mod.pane.close", mods.onModPane],
]);
