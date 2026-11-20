// engine/liveBubble — live 气泡辅助函数（markDelivered / adoptOrphanStream /
// ensureLive / upsertTurnMeta / finalizeTurnMetaLocal / mutateLive /
// stripFromLive），自 handle.ts 机械拆出（批次 6-2），函数体逐字一致；
// 原闭包引用改为经 ctx 取用。handle.ts re-export 保持外部 import 面不变。

import { applyBlock, mid, newTurnId } from "../streamCore";
import type { EngineCtx } from "./ctx";
import { decideTurnAdoption, mismatchReportBudget } from "@/lib/streamTurnGuard";

// Delivery confirmed (the server started or finished the turn) → clear the
// "sending" marker off user bubbles.
export function markDelivered(ctx: EngineCtx, sid: string) {
  const list = ctx.storeRef.current[sid];
  if (!list?.some((m) => m.status === "sending")) return;
  ctx.storeRef.current[sid] = list.map((m) =>
    m.status === "sending" ? { ...m, status: undefined, failReason: undefined } : m,
  );
}

// The server is streaming a turn this component instance never saw start
// (remount mid-turn after navigating away). Mark the stream orphaned so the
// split self-heals on message.end, and backfill the session's agent so the
// continuation bubble's header shows the right name instead of "Agent".
export function adoptOrphanStream(ctx: EngineCtx, sid: string) {
  if (ctx.seenTurnStartRef.current[sid] || ctx.orphanStreamRef.current[sid]) return;
  ctx.orphanStreamRef.current[sid] = true;
  ctx.busyBySessionRef.current[sid] = true;
  if (!ctx.streamAgentRef.current[sid]) ctx.streamAgentRef.current[sid] = ctx.session?.agent_id ?? null;
}

export function ensureLive(ctx: EngineCtx, sid: string, evTurn?: string): string {
  const existing = ctx.liveBySessionRef.current[sid];
  if (existing) return existing;
  // 承接本轮已存在的气泡（2026-10-01 子会话渲染成两块）：socket 在 turn 中途
  // 才连上时，历史重放里已经有这一轮的半截气泡（带正确的 agent 名与 turnId），
  // 若此时新建气泡，同一轮会渲染成「历史半截 + 无 agent 的实时块」两块。流事件
  // 应当认领承载同一 turnId 的气泡——没有 turnId 可比时才新建。
  if (evTurn) {
    const rows = ctx.storeRef.current[sid] ?? [];
    for (let i = rows.length - 1; i >= 0; i--) {
      const m = rows[i];
      if (m.role === "assistant" && m.turnId === evTurn) {
        ctx.liveBySessionRef.current[sid] = m.id;
        if (!ctx.streamAgentRef.current[sid]) ctx.streamAgentRef.current[sid] = m.agentId ?? null;
        return m.id;
      }
    }
  }
  const id = mid();
  ctx.storeRef.current[sid] = [
    ...(ctx.storeRef.current[sid] ?? []),
    {
      id,
      role: "assistant",
      blocks: [{ kind: "turn_meta", t0Ms: Date.now() }],
      // Turn 结束后 streamAgentRef 已被清空——晚到的事件（如 dev-swap 构建结果
      // 数分钟后才回的 notice）走到这条新建路径，agentId=null 会把气泡头兜底成
      // "Agent"。回填会话默认 agent，与 adoptOrphanStream 的同名修复同理。
      agentId: ctx.streamAgentRef.current[sid] ?? ctx.session?.agent_id ?? null,
      turnId: evTurn || newTurnId(),
    },
  ];
  ctx.liveBySessionRef.current[sid] = id;
  return id;
}

/** turn-meta（turn 右下角耗时+token）的 live upsert：在 live 气泡尾部维护
 *  唯一的 turn_meta block——usage 事件累加 token，message.end 的 payload.meta
 *  定格（elapsedS 权威值替换 t0Ms）。历史投影版（reload）只有定格字段。 */
export function upsertTurnMeta(
  ctx: EngineCtx,
  sid: string,
  patch: Partial<{ elapsedS: number; t0Ms: number; inputTokens: number; outputTokens: number }>,
  opts: { finalize?: boolean } = {},
) {
  const id = ctx.liveBySessionRef.current[sid];
  if (!id) return;
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) => {
    if (m.id !== id) return m;
    const blocks = [...m.blocks];
    const i = blocks.findIndex((b) => b.kind === "turn_meta");
    const base =
      i >= 0
        ? (blocks[i] as { kind: "turn_meta"; elapsedS?: number; t0Ms?: number; inputTokens?: number; outputTokens?: number })
        : { kind: "turn_meta" as const, t0Ms: Date.now() };
    const next = { ...base, ...patch };
    if (opts.finalize) delete next.t0Ms;
    if (i >= 0) blocks[i] = next;
    else blocks.push(next);
    return { ...m, blocks };
  });
}

/** turn 中止/失败（turn.stopped / error / 老侧 message.end 无 meta）时定格
 * turn-meta：无服务端权威值，用本地 t0Ms 算 elapsedS 后删 t0 停跳——否则
 * 气泡已停止流但秒表永远每秒自增。 */
export function finalizeTurnMetaLocal(ctx: EngineCtx, sid: string, liveId: string | null) {
  if (!liveId) return;
  const bubble = (ctx.storeRef.current[sid] ?? []).find((m) => m.id === liveId);
  const tm = bubble?.blocks.find(
    (b) => b.kind === "turn_meta",
  ) as { kind: "turn_meta"; t0Ms?: number } | undefined;
  if (!tm || typeof tm.t0Ms !== "number") return;
  const was = ctx.liveBySessionRef.current[sid];
  ctx.liveBySessionRef.current[sid] = liveId; // upsert 以 liveId 定位
  upsertTurnMeta(
    ctx,
    sid,
    { elapsedS: Math.max(0, Math.round((Date.now() - tm.t0Ms) / 1000)) },
    { finalize: true },
  );
  ctx.liveBySessionRef.current[sid] = was ?? null;
}

export function mutateLive(ctx: EngineCtx, sid: string, ev: { event: string; [k: string]: unknown }) {
  const id = ensureLive(ctx, sid, ev.turn_id as string | undefined);
  // 气泡归属体检（2026-10-01 串线排查）：流事件带的 turn_id 必须与它落入的
  // 气泡 turnId 一致；不一致说明内容串进了别轮的气泡。release webview 没有
  // 可读控制台，所以同时上报服务端日志（client_diag）留证。
  const evTurn = ev.turn_id as string | undefined;
  let bubble = (ctx.storeRef.current[sid] ?? []).find((m) => m.id === id);
  // 孤儿流认领（2026-10-01 串线排查）：客户端在 turn 进行中途才连上 socket
  // （子会话常态——spawn 后隔几十秒才被打开），拿不到 turn.start，气泡带着
  // 本地占位 turnId。首个带 turn_id 的事件到来时认领服务端 id，否则该会话的
  // 每个事件都对不上气泡、（更要紧）steer 帧会带一个服务端不认的 turn_id。
  //
  // 认领泛化（2026-11-06 mismatch 洪水）：sidecar 重启/resume 会在**正常
  // 连接中**生成新 turn_id——旧 bubble 不重建，此后每帧失配（16aef0a3
  // 实录 17 分钟 10,051 条 WARNING）。会话内 turn 串行、不存在真并发串线，
  // 漂移即认领：live bubble 的 turnId 跟随服务端（孤儿标记只是初始来源
  // 之一，不再是唯一通道）。判定在 lib/streamTurnGuard（纯函数可测）。
  if (
    evTurn &&
    bubble &&
    decideTurnAdoption({
      bubbleTurn: bubble.turnId,
      evTurn,
      orphan: !!ctx.orphanStreamRef.current[sid],
      adopted: !!ctx.adoptedTurnRef.current[sid],
    }) === "adopt"
  ) {
    ctx.adoptedTurnRef.current[sid] = true;
    ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) =>
      m.id === id ? { ...m, turnId: evTurn } : m,
    );
    bubble = { ...bubble, turnId: evTurn };
  }
  if (evTurn && bubble?.turnId && evTurn !== bubble.turnId) {
    // 上报限频（streamTurnGuard）：同一 (会话, bubble, ev) 漂移对终生一次
    // ——认领修复后此分支几乎不再触发；残留触发也只报一次，诊断洪水
    // 不再淹没真错误。
    if (
      mismatchReportBudget({ sid, bubble: bubble.turnId, ev: evTurn })
    ) {
      const detail = {
        event: ev.event,
        ev_turn: evTurn,
        bubble_turn: bubble.turnId,
        session: sid,
        head: String((ev.content as string) ?? "").slice(0, 40),
      };
      console.warn("[ginno] stream event landed in another turn's bubble", detail);
      try {
        ctx.socketsRef.current[sid]?.send(
          JSON.stringify({ type: "client_diag", diag_kind: "bubble_turn_mismatch", detail }),
        );
      } catch { /* socket may be closing */ }
    }
  }
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((msg) =>
    msg.id === id ? { ...msg, blocks: applyBlock(msg.blocks, ev) } : msg,
  );
}

// 从 live 气泡的文本块里精确摘掉一段此前 append 的瞬态文本（auto-retry
// 提示的原位替换/撤行）。只在「最近一个含该文本的 text 块」上动手；摘空
// 的块整块移除，不留空段落。找不到（气泡已被 history 重建等）则静默。
export function stripFromLive(ctx: EngineCtx, sid: string, chunk: string) {
  const id = ctx.liveBySessionRef.current[sid];
  if (!id || !chunk) return;
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((msg) => {
    if (msg.id !== id) return msg;
    for (let i = msg.blocks.length - 1; i >= 0; i--) {
      const b = msg.blocks[i];
      if (b.kind === "text" && typeof b.text === "string" && b.text.includes(chunk)) {
        const newText = b.text.replace(chunk, "");
        const blocks = newText ? msg.blocks.slice() : msg.blocks.filter((_, j) => j !== i);
        if (newText) blocks[i] = { kind: "text", text: newText };
        return { ...msg, blocks };
      }
    }
    return msg;
  });
}
