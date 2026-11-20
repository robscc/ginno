// engine/handle — 服务端事件 reducer 薄入口（批次 6-2 拆分）。
// 原 2217 行大 switch 已按事件域等价迁移到 handlers/ 分组模块
// （01-stream … 15-mods，连续切段、组间不交叉），handlers/index.ts 的
// EVENT_HANDLERS 注册表按原 case 顺序集中登记——事件全集的验收基准是
// handleDispatch.test.ts ①层枚举快照。
// 本入口只做三件事：compacting 收尾前置、查表分发、末尾 syncDisplay 兜底
// （未知事件也走 syncDisplay，与拆分前 switch 无 default 的行为一致）。
// live 气泡 helper（liveBubble.ts）与通知出口（notifyOut.ts）经 re-export
// 保持外部 import 面不变（useChatStreamEngine 与测试均从本文件取符号）。

export {
  markDelivered,
  adoptOrphanStream,
  ensureLive,
  upsertTurnMeta,
  finalizeTurnMetaLocal,
  mutateLive,
} from "./liveBubble";
export { cancelPendingReplyNotice } from "./notifyOut";

import type { EngineCtx } from "./ctx";
import { EVENT_HANDLERS } from "./handlers";

export function handle(ctx: EngineCtx, sid: string, ev: { event: string; [k: string]: unknown }) {
  // 「压缩中…」收尾：压缩期间该会话不会有其他流事件,所以除置位事件本身
  // 之外的任何事件到达 = 压缩已结束(成功→context.compacted/流恢复;失败
  // →turn 照常继续,compaction 从不当 blocker)。含 turn.stopped/error。
  // 例外(P5,2026-11-16)：turn.state 携带 compacting=true 时是重连探针
  // 在报告「摘要还在飞」——恰恰要保住镜像,不能当结束信号清掉。
  const isCompactingProbe =
    ev.event === "turn.state" && ev.compacting === true;
  if (ev.event !== "context.compacting" && !isCompactingProbe && ctx.compactingBySessionRef.current[sid]) {
    ctx.compactingBySessionRef.current[sid] = false;
    ctx.compactStageRef.current[sid] = null;
    ctx.syncDisplay(sid);
  }
  const handler = EVENT_HANDLERS.get(ev.event);
  if (handler) handler(ctx, sid, ev);
  ctx.syncDisplay(sid);
}
