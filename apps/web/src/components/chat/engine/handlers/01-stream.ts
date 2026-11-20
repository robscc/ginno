// 01-stream — 流帧事件（token/thinking/tool 帧、问卡、emit 帧、llm 可观测帧、
// workflow.emit）。等价迁移自 handle() switch 首段（批次 6-2）：case 体逐字
// 一致，fall-through 共享体还原为「多键注册同一函数」。
//
// ⚠️ 共享体警告（docs/notifications-audit-2026-11-14.md P0-1）：token.delta /
// thinking.delta 原本 fall through 到 tool.* 共享体，本体被 5 个事件执行——
// 任何「只对某一类事件成立」的副作用都不能放在共享函数里。2026-11-14 事故：
// ask_user 的 OS 通知曾被塞进共享体，delta 帧没有 ev.id → 去重键退化成
// `${sid}:${Date.now()}` 每帧唯一 → 每个 token 弹一条「等你回答」（单会话实测
// 1583 条）。事件专属副作用一律拆到自己的 handler（见 onQuestion）。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { adoptOrphanStream, mutateLive, stripFromLive } from "../liveBubble";
import { maybeNotifyAskUser } from "../notifyOut";

/** tool.* 三帧的共享体（也是 widget/ref/image.emit、llm.request/response、
 *  workflow.emit 的体——原 switch 里六者本就是同样的两条调用，各自独立 case
 *  分组，只是函数体相同；合并注册不改变行为）。 */
export function onStreamFrame(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  adoptOrphanStream(ctx, sid);
  mutateLive(ctx, sid, ev);
}

/** token/thinking delta：先做「重试恢复出流」的瞬态提示撤行（仅 delta 有，
 *  tool 帧没有——delta 专属副作用保留在专属 handler），再走共享体。 */
export function onDelta(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 重试恢复出流：瞬态 auto-retry 提示撤行（若随后又失败，新的 notice
  // 会重新落地）。只认本会话还有挂着的提示时才动 store。
  if (ctx.retryNoticeRef.current[sid]) {
    const prev = ctx.retryNoticeRef.current[sid];
    ctx.retryNoticeRef.current[sid] = null;
    stripFromLive(ctx, sid, prev ?? "");
  }
  onStreamFrame(ctx, sid, ev);
}

/** ask_user 问卡（Transcript block, not a ref-based prompt like
 *  permission.request: the card lives (and is merged by id) inside the live
 *  bubble's blocks）。通知是本 handler 专属副作用——绝不能并入共享体。 */
export function onQuestion(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  maybeNotifyAskUser(ctx, sid, ev);
  adoptOrphanStream(ctx, sid);
  mutateLive(ctx, sid, ev);
}
