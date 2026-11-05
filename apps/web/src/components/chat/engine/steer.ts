// engine/steer — mid-turn steering 队列管线（docs/steering-design.md，自
// useChatStreamEngine.ts 机械拆出，函数体逐字一致；原闭包引用改为经 ctx 取用）。

import type { Attachment, ChatMsg, FileAttachment } from "../streamCore";
import type { SteerItem } from "@/lib/steerQueue";
import type { EngineCtx } from "./ctx";

// ─── Mid-turn steering (docs/steering-design.md) ──────────────────────────────
/** The running turn's id for a session (the steer rides it). Empty string
 *  when unknown — the server then falls back to its own running-turn id,
 *  which is the only correct answer for a turn nobody here invoked (an
 *  adopted orphan, or a goal continuation). */
export function liveTurnIdFor(ctx: EngineCtx, sid: string): string {
  const live = ctx.liveBySessionRef.current[sid];
  const msg = live ? (ctx.storeRef.current[sid] ?? []).find((m) => m.id === live) : undefined;
  return msg?.turnId ?? "";
}

/** Drop one queued entry (the ✕ on a queue-bar row). */
export function dropSteer(ctx: EngineCtx, sid: string, steerId: string) {
  ctx.steerQ.remove(sid, [steerId]);
}

export function enqueueSteer(
  ctx: EngineCtx,
  sid: string,
  text: string,
  agentId: string | null,
  images: Attachment[] = [],
  files: FileAttachment[] = [],
) {
  ctx.steerQ.enqueue({
    sessionId: sid,
    turnId: liveTurnIdFor(ctx, sid),
    text,
    images,
    files,
    agentId,
    send: steerFrameSender(ctx, sid),
  });
}

/** Raw-frame sender for a session's socket, handed to the shared queue (which
 *  owns the frame shape and swallows a dead socket so the entry stays queued).
 *  Each window passes its own socket here — the pin has its own. */
export function steerFrameSender(ctx: EngineCtx, sid: string): (frame: unknown) => void {
  return (frame) => {
    ctx.socketsRef.current[sid]?.send(JSON.stringify(frame));
  };
}

/** Re-send every queued entry as `steer` — done right before we resume a
 *  parked turn, so an entry stashed in the segment that parked is not lost.
 *  Safe to repeat: server-side enqueue replaces by steer_id (design §3.2). */
export function requeueSteersOnResume(ctx: EngineCtx, sid: string) {
  ctx.steerQ.onResume(sid, steerFrameSender(ctx, sid));
}

/** 把取回的条目恢复进 composer（文本按序置顶 + 附件/文件回归）。
 *  本地未发送与服务端已撤回（steer.recalled）共用。 */
export function restoreSteerItems(ctx: EngineCtx, sid: string, items: SteerItem[]): void {
  if (!items.length) return;
  const recalled = items.map((i) => i.text).join("\n");
  const recalledImgs = items.flatMap((i) => i.images);
  const recalledFiles = items.flatMap((i) => i.files);
  if (sid !== ctx.curSessionIdRef.current) {
    const d = ctx.draftCacheRef.current[sid] ?? { input: "", attachments: [] };
    ctx.draftCacheRef.current[sid] = {
      ...d,
      input: d.input ? `${recalled}\n${d.input}` : recalled,
      attachments: [...recalledImgs, ...d.attachments],
      files: [...recalledFiles, ...(d.files ?? [])],
    };
    return;
  }
  ctx.setInput((cur) => (cur ? `${recalled}\n${cur}` : recalled));
  if (recalledImgs.length) ctx.setAttachments((a) => [...recalledImgs, ...a]);
  if (recalledFiles.length) ctx.setFileAttachments((f) => [...recalledFiles, ...f]);
}

/** ↑ 取回的「已发送」条目暂存：发 steer.recall 后等服务端 steer.recalled
 *  对账——真实删除集里有的才弹回（竞态下已被吸收的那条照常注入，不重复）。 */
const recallPending = new Map<string, SteerItem[]>();

/** ⏹ / ↑ 取回（2026-11-05 协议升级）：未发送条目立即弹回；已发送的必须
 *  走服务端撤回（steer.recall → steer.recalled 确认 → 才弹回）——此前
 *  只清本地队列，服务端 stash 里的照常被工具边界吸收（用户实录「按了
 *  uparrow 还是注入进去了」）。 */
export function recallSteers(ctx: EngineCtx, sid: string): void {
  const items = ctx.steerQ.recall(sid);
  if (!items.length) return;
  const sent = items.filter((i) => i.steerId);
  const local = items.filter((i) => !i.steerId);
  restoreSteerItems(ctx, sid, local);
  if (sent.length) {
    steerFrameSender(ctx, sid)({
      type: "steer.recall",
      steer_ids: sent.map((i) => i.steerId),
    });
    recallPending.set(
      sid,
      [...(recallPending.get(sid) ?? []), ...sent],
    );
  }
}

/** steer.recalled 事件到账：对账真实删除集，弹回其中命中者。 */
export function settleRecalled(ctx: EngineCtx, sid: string, ids: Array<string>): void {
  const pend = recallPending.get(sid) ?? [];
  if (!pend.length) return;
  const hit = new Set(ids);
  const restored = pend.filter((i) => i.steerId && hit.has(i.steerId));
  const keep = pend.filter((i) => !i.steerId || !hit.has(i.steerId));
  // 未命中的（已被吸收）不再弹回；也没有继续挂账的意义（服务端已删或已吸收）
  if (keep.length) recallPending.set(sid, keep);
  else recallPending.delete(sid);
  if (restored.length) restoreSteerItems(ctx, sid, restored);
}

/** The turn ended with entries still unacknowledged: the OLDEST becomes the
 *  next turn — Claude Code's rule verbatim ("when the turn ends with messages
 *  still queued, Claude Code sends only the oldest as the next turn"). The
 *  rest stay queued and follow the same rule on that turn's end (design §3.3). */
export function flushSteerQueue(ctx: EngineCtx, sid: string) {
  // Pop the oldest out of the queue (the shared hook owns the ref write), then
  // promote it to the next turn here — turning it into a turn is this window's
  // concern, not the queue's.
  const head = ctx.steerQ.takeOldest(sid);
  if (!head) return;
  ctx.attemptSend(sid, {
    text: head.text,
    // The entry's attachments ride the invoke that becomes the next turn —
    // dropping them here is the other half of the silent-loss bug.
    images: head.images,
    files: head.files,
    mentions: [],
    agentId: head.agentId,
  });
}

/** History is authoritative: a steer_id it carries was absorbed even if the
 *  ack was lost to a socket drop, so drop those entries instead of re-sending
 *  them. This is what keeps delivery exactly-once (design §3.3). The history
 *  scan stays here (it reads this window's mapped bubbles); the hook does the
 *  queue write. */
export function dropAbsorbedSteers(ctx: EngineCtx, sid: string, mapped: ChatMsg[]) {
  const absorbed = new Set<string>();
  for (const m of mapped) {
    for (const b of m.blocks) {
      if (b.kind === "steer" && b.steerId) absorbed.add(b.steerId);
    }
  }
  if (absorbed.size) ctx.steerQ.remove(sid, absorbed);
}
