// engine/steer — mid-turn steering 队列管线（docs/steering-design.md，自
// useChatStreamEngine.ts 机械拆出，函数体逐字一致；原闭包引用改为经 ctx 取用）。

import type { Attachment, ChatMsg, FileAttachment } from "../streamCore";
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

/** ⏹ means stop: hand what is still queued back to the composer — multi-line,
 *  in order, ABOVE whatever is already typed (the ↑ recall's placement) —
 *  rather than silently discarding what the user wrote (design §4.1). */
export function recallSteers(ctx: EngineCtx, sid: string) {
  const items = ctx.steerQ.recall(sid);
  if (!items.length) return;
  const recalled = items.map((i) => i.text).join("\n");
  // Attachments come back too — a recalled steer must not lose its pictures
  // or file chips (the whole reason they are kept on the entry, brief §5.1).
  const recalledImgs = items.flatMap((i) => i.images);
  const recalledFiles = items.flatMap((i) => i.files);
  if (sid !== ctx.curSessionIdRef.current) {
    // Background session: park it in that session's draft, so switching back
    // shows it in the composer instead of dropping it on the floor.
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
