// 13-turn-end — turn 生命周期收口（探针应答 / message.end / turn.stopped /
// error）。等价迁移自 handle() switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { hasPendingTool, hasPendingLlmCall } from "@/components/chat/blocks";
import { closePendingBlocks, mid } from "../../streamCore";
import { reconcileTurnFromHistory } from "../connect";
import { flushSteerQueue, recallSteers } from "../steer";
import {
  finalizeTurnMetaLocal,
  mutateLive,
  adoptOrphanStream,
  markDelivered,
  upsertTurnMeta,
} from "../liveBubble";
import { isWatching, replyMerge } from "../notifyOut";
import { notifyNative } from "@/lib/desktop";
import { shouldNotify } from "@/lib/notifyHub";
import { onContextCompacting } from "./11-context";

export function onTurnState(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // Answer to the post-reconnect probe: is a turn still running (or
  // parked at an interrupt) for this session?
  if (ctx.reconcileTimerRef.current[sid]) {
    clearTimeout(ctx.reconcileTimerRef.current[sid]!);
    ctx.reconcileTimerRef.current[sid] = null;
  }
  onContextCompacting(ctx, sid, ev);
  if (ctx.serverRunningRef.current[sid] !== !!ev.running) {
    ctx.serverRunningRef.current[sid] = !!ev.running;
    // Mirror into React state — this is what makes a history-rebuilt
    // pending question card interactive after a reload (questionLive).
    ctx.syncDisplay(sid);
  }
  if (ev.running) {
    if (ev.reasoning_summaries || ev.reasoning_summary) {
      adoptOrphanStream(ctx, sid);
      mutateLive(ctx, sid, ev);
    }
    return;
  }
  // ...unless our newest send is NEWER than the probe this answers. The
  // answer can only describe the session as it was when the server
  // handled the probe; reconciling a send that postdates it reads a
  // /history with no checkpoint yet and stamps the bubble 「未送达」 on a
  // turn that is running fine (first message of a new session: the socket
  // is opened by the send, probe at t0, invoke at t0+13ms). Wait for the
  // stream instead — it always ends in message.end or error, both of
  // which reconcile a genuinely dead turn.
  if ((ctx.sendSeqRef.current[sid] ?? 0) > (ctx.probeSeqRef.current[sid] ?? 0)) return;
  // Not running: rebuild from persisted history ONLY when this client
  // believed a turn was in flight (the probe now fires on every open —
  // an idle reconnect must not replace the loaded store). This is also
  // what flips a pending question card of a genuinely dead turn into
  // the checkpoint's healed "(interrupted)" → skipped replay.
  if (
    ctx.liveBySessionRef.current[sid] ||
    ctx.busyBySessionRef.current[sid] ||
    ctx.orphanStreamRef.current[sid]
  ) {
    // 断线期间 turn 已收口：这里等价于 message.end 的 orphan 分支——
    // reconcile 清掉已吸收条目后，剩余队列头提升为下一 turn（2026-10-30
    // 之前这里不 flush，同样的卡队列换了一扇门而已）。
    void reconcileTurnFromHistory(ctx, sid).then(() =>
      flushSteerQueue(ctx, sid),
    );
  }
}

export function onMessageEnd(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  if ((ev.reasoning_summaries || ev.reasoning_summary) && ctx.liveBySessionRef.current[sid]) {
    mutateLive(ctx, sid, ev);
  }
  markDelivered(ctx, sid);
  const wasOrphan = !!ctx.orphanStreamRef.current[sid];
  // turn 收口要盖时间戳的气泡：清理 liveId 之前先捕获（收口后按 id /
  // turnId 在 store 里找它）。
  const endedLiveId = ctx.liveBySessionRef.current[sid] ?? null;
  const endTurn = (ev.turn_id as string) || null;
  ctx.orphanStreamRef.current[sid] = false;
  ctx.adoptedTurnRef.current[sid] = false;
  ctx.liveBySessionRef.current[sid] = null;
  ctx.streamAgentRef.current[sid] = null;
  ctx.busyBySessionRef.current[sid] = false;
  ctx.serverRunningRef.current[sid] = false;
  // Turn 收口盖时间戳（2026-10-29 用户反馈：1h 后气泡仍显示「刚刚」）：
  // 完成态的相对时间以收口时刻计（不用气泡创建时刻——长 turn 一结束就会
  // 显示"N分钟前"）。orphan 重建后的气泡 id 已变，靠 turnId 兜底命中。
  // 历史重放（reload）没有时间戳源，标签侧干脆不渲染。
  const stampTurnTs = () => {
    ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) =>
      m.role === "assistant" &&
      m.ts === undefined &&
      (m.id === endedLiveId || (!!endTurn && m.turnId === endTurn))
        ? { ...m, ts: Date.now() / 1000 }
        : m,
    );
  };
  // turn-meta 定格：engine 的权威值（elapsed_s/tokens）替换 live 计时版
  // （删 t0Ms 停跳）。老 sidecar 无 meta 时保留 live 计时的最后读数。
  {
    const meta = ev.meta as { elapsed_s?: number; input_tokens?: number; output_tokens?: number } | undefined;
    if (meta && typeof meta.elapsed_s === "number") {
      const keep = endedLiveId;
      if (keep) {
        ctx.liveBySessionRef.current[sid] = keep; // upsert 以 liveId 定位
        upsertTurnMeta(
          ctx,
          sid,
          {
            elapsedS: meta.elapsed_s,
            ...(typeof meta.input_tokens === "number" ? { inputTokens: meta.input_tokens } : {}),
            ...(typeof meta.output_tokens === "number" ? { outputTokens: meta.output_tokens } : {}),
          },
          { finalize: true },
        );
        ctx.liveBySessionRef.current[sid] = null;
      }
    } else {
      // 老 sidecar 无 meta：live 计时的最后读数定格（不删 t0 会永远自跳）。
      finalizeTurnMetaLocal(ctx, sid, endedLiveId);
    }
  }
  // Turn done → desktop notification unless the user is watching this
  // exact session right now (visible ∧ workspace route ∧ active session).
  // Socket callbacks capture stale closures (sockets outlive session
  // switches) — read refs / live values only. The session title may be
  // stale too (rename after connect); cosmetic, accepted.
  {
    const watching = isWatching(ctx, sid);
    // 侧栏未读小圆点：没在盯这个会话时 turn 跑完 → 标记未读（与上面的
    // 桌面通知同一 watching 判定，但不受通知开关影响——点是无声的）。
    // 正在看则上报已读：服务端在 turn 收口时已写 last_finished，不压掉
    // 的话下一次 reloadSessions 会把眼前这个会话点亮。
    if (!watching) ctx.g.markSessionUnread(sid);
    else ctx.g.clearSessionUnread(sid);
    // V2：reply 通知过 shouldNotify 门（总开关 ∧ kinds.reply ∧ DnD）后
    // 进 30s 合并窗口——多条后台完成合成一条（防 workflow 逐步刷屏）。
    // origin 门（2026-11-05 通知触发点盘点）：schedule/subagent 起的
    // headless turn 完成已有专属通道（schedule-run / workflow-run store
    // 侧通知），这里跳过防双弹——未读点保留（侧栏仍亮）。
    const turnOrigin = typeof ev.origin === "string" ? ev.origin : "interactive";
    if (shouldNotify("reply", watching) && turnOrigin === "interactive") {
      const title = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
      const raw = typeof ev.text === "string" ? ev.text.trim() : "";
      const body = raw || ctx.i18nRef.current.tr("notify.replyCompleted");
      replyMerge.push(sid, {
        title,
        body,
        trN: (n) => ctx.i18nRef.current.tr("notify.nCompleted", { n }),
        // flush 时（30s 后）复检：用户这期间切进来了就不弹（P1-3）
        attentive: () => isWatching(ctx, sid),
      });
    }
  }
  // The turn ended with steer entries still unacknowledged: the oldest
  // becomes the next turn (design §3.3). On the orphan path the history
  // reconcile must run FIRST — it drops entries the server already
  // absorbed (exactly-once, §3.3) and heals the split bubble — and THEN
  // the queue head, if anything is left, is promoted into the next turn.
  // Before 2026-10-30 the orphan branch skipped the flush entirely, so
  // steers queued into an adopted stream stayed "queued" forever (the
  // second-steer freeze report); the timestamp stamps ride the same
  // callback because reconcile replaces the store the sync stamp would
  // have landed in.
  if (wasOrphan) {
    void reconcileTurnFromHistory(ctx, sid).then(() => {
      stampTurnTs();
      flushSteerQueue(ctx, sid);
    });
  } else {
    stampTurnTs();
    flushSteerQueue(ctx, sid);
  }
}

export function onTurnStopped(ctx: EngineCtx, sid: string) {
  // User pressed stop: the server abandoned the in-flight step and
  // healed the persisted state; everything already streamed is kept.
  // Close out the live stream like message.end, force-close pending
  // tool blocks like the error handler — but no error card and no
  // "turn done" notification (the turn didn't complete).
  markDelivered(ctx, sid);
  const wasOrphan = !!ctx.orphanStreamRef.current[sid];
  ctx.orphanStreamRef.current[sid] = false;
  ctx.adoptedTurnRef.current[sid] = false;
  const liveMsgId = ctx.liveBySessionRef.current[sid];
  ctx.liveBySessionRef.current[sid] = null;
  ctx.streamAgentRef.current[sid] = null;
  ctx.busyBySessionRef.current[sid] = false;
  ctx.serverRunningRef.current[sid] = false;
  // turn-meta 定格：停表（无服务端权威值，本地 t0 折算；服务端 stopped
  // 分支不写 turns.jsonl，reload 后该轮无小字——与「停止非完成」语义一致）。
  finalizeTurnMetaLocal(ctx, sid, liveMsgId);
  // ⏹ means stop, so what was queued for absorption is handed back to the
  // composer rather than discarded or auto-sent (design §4.1). Done here,
  // in the event, so a stop from another tab recalls on this one too.
  recallSteers(ctx, sid);
  // Stop also clears a parked prompt (server heals those too) — every
  // tab of the session leaves the running state together.
  ctx.permsRef.current[sid] = null;
  ctx.proposeRef.current[sid] = null;
  ctx.setPermission(null);
  ctx.setPropose(null);
  if (wasOrphan) {
    reconcileTurnFromHistory(ctx, sid);
    return;
  }
  const list = ctx.storeRef.current[sid] ?? [];
  ctx.storeRef.current[sid] = list
    // An empty live bubble (stopped before the first token) would
    // render as a confusing "（空回复）" — drop it; streamed text stays.
    .filter((m) => !(m.id === liveMsgId && m.blocks.length === 0))
    // Close out any in-flight tool blocks (and pending question cards —
    // the backend heals a parked ask_user to "(interrupted)" too, which
    // replay reads as skipped) so `running` unsticks. pending 的 llm_call
    // 行同治（停在模型调用中途、工具还没起跑的气泡也要收口）。
    .map((msg) =>
      hasPendingTool(msg.blocks) || hasPendingLlmCall(msg.blocks)
        ? { ...msg, blocks: closePendingBlocks(msg.blocks) }
        : msg,
    );
}

export function onError(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // The turn reached the server (it is the run, not the delivery, that
  // failed) → the user bubble counts as delivered; the failure becomes
  // a dedicated error card with a retry action.
  markDelivered(ctx, sid);
  // turn 以失败告终也算「运行结束」：没在盯的会话点未读小圆点（重试入口
  // 在错误卡上，别让用户错过）；正盯着则上报已读压掉服务端的 last_finished。
  if (ctx.g.watchingSession(sid)) ctx.g.clearSessionUnread(sid);
  else ctx.g.markSessionUnread(sid);
  // 失败即时通知（不合并）——⚠️ 前缀，Basso 故障音由 notifyKind:"failure"
  // 在 notifyNative 出口统一推导，一眼分清出事/干完。
  if (shouldNotify("failure", isWatching(ctx, sid))) {
    const sTitle = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
    const detail =
      ctx.i18nRef.current.evText(ev, String(ev.message || "")) ||
      ctx.i18nRef.current.tr("error.unknown");
    void notifyNative({
      kind: "session",
      notifyKind: "failure",
      id: sid,
      title: `⚠️ ${sTitle}`,
      body: detail,
    });
  }
  const liveMsgId = ctx.liveBySessionRef.current[sid];
  ctx.liveBySessionRef.current[sid] = null;
  ctx.streamAgentRef.current[sid] = null;
  ctx.busyBySessionRef.current[sid] = false;
  ctx.serverRunningRef.current[sid] = false;
  // turn-meta 定格：失败气泡保留在转录里（failed 红边卡），秒表停跳。
  finalizeTurnMetaLocal(ctx, sid, liveMsgId);
  // Orphaned turn that failed: reconcile from history instead of
  // building a card on the split store — mapHistory re-surfaces the
  // persisted last_error as a proper error card with retry.
  if (ctx.orphanStreamRef.current[sid]) {
    ctx.orphanStreamRef.current[sid] = false;
    ctx.adoptedTurnRef.current[sid] = false;
    ctx.adoptedTurnRef.current[sid] = false;
    reconcileTurnFromHistory(ctx, sid);
    return;
  }
  const list = ctx.storeRef.current[sid] ?? [];
  const liveBubble = list.find((m) => m.id === liveMsgId);
  // Retry payload: the originating user turn's snapshot (live turns
  // always carry one). Absent → the card renders without a retry button.
  const lastUser = [...list].reverse().find((m) => m.role === "user" && m.sendPayload);
  ctx.storeRef.current[sid] = [
    ...list
      // An empty live bubble (error before any token) would render as a
      // confusing "（空回复）" right above the error card — drop it.
      .filter((m) => !(m.id === liveMsgId && m.blocks.length === 0))
      // Close out any in-flight tool blocks as interrupted so `running` unsticks,
      // and mark the live bubble as `failed` so the UI can show a visual
      // indicator (red border + "回复中断" label) instead of looking like
      // a normal completed assistant reply.
      .map((msg) => {
        const marked = msg.id === liveMsgId ? { ...msg, failed: true } : msg;
        if (!hasPendingTool(marked.blocks) && !hasPendingLlmCall(marked.blocks)) return marked;
        return { ...marked, blocks: closePendingBlocks(marked.blocks) };
      }),
    {
      id: mid(),
      role: "assistant" as const,
      // 事件契约（i18n-design.md §3）：error 事件可带 i18n_key+params，
      // 落地即翻译；未命中/缺失回退服务端成品 message，再退「未知错误」。
      blocks: [
        {
          kind: "text",
          text:
            ctx.i18nRef.current.evText(ev, String(ev.message || "")) ||
            ctx.i18nRef.current.tr("error.unknown"),
        },
      ],
      turnId: liveBubble?.turnId,
      error: true,
      sendPayload: lastUser?.sendPayload,
      sourceMsgId: lastUser?.id,
    },
  ];
  // The turn died with entries still unacknowledged → the oldest becomes
  // the next turn, same rule as a normal end (design §3.3).
  flushSteerQueue(ctx, sid);
}
