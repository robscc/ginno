// engine/handle — 服务端事件 reducer（handle）及其 live 气泡辅助函数
// （markDelivered / adoptOrphanStream / ensureLive / mutateLive / stripFromLive，
// 自 useChatStreamEngine.ts 机械拆出，函数体逐字一致；原闭包引用改为经 ctx 取用）。

import { dismissLastwords, getWorkflowRun, resumeLastwords } from "@/lib/runtime";
import { notifyNative } from "@/lib/desktop";
import { applyPrivacy, osGateOpen } from "@/lib/notifyPrefs";
import { pushNotice, type NoticeLevel } from "@/lib/notice";
import { pushModPane, type ModPaneSnapshot } from "@/components/chat/mod/paneBus";
import type { ModBandGroup } from "@/components/chat/mod/ModBand";
import type { ModAskState } from "@/components/chat/mod/ModAskCard";
import { hasPendingTool, hasPendingLlmCall, foldSubagentResultBlocks, type Block } from "@/components/chat/blocks";
import type {
  ContextChange,
  ContextState,
  Goal,
  Plan,
  SessionMeta,
  SessionUsage,
  SubagentPlanEvent,
  SubagentSpawnEvent,
  SubagentStatusEvent,
} from "@/lib/types";
import { applyBlock, closePendingBlocks, mid, newTurnId, toolArgsPreview, type ChatMsg } from "../streamCore";
import type { EngineCtx } from "./ctx";
import { reconcileTurnFromHistory } from "./connect";
import { flushSteerQueue, recallSteers, settleRecalled } from "./steer";
import { pushDevSwapRun } from "@/lib/devswap/bus";
import { normalizeDevSwapRun } from "@/lib/devswap/types";
import { ReplyMergeWindow, isSessionAttentive, replyRateGate, shouldNotify, type MergeItem } from "@/lib/notifyHub";
import { decideTurnAdoption, mismatchReportBudget } from "@/lib/streamTurnGuard";

// ── OS 通知（docs/notifications-audit-2026-11-14.md）─────────────────────────
// 偏好闸门（总开关 ∧ 分类型 ∧ 勿扰 ∧ 隐私 ∧ 提示音）统一装在 desktop.ts 的
// notifyNative 出口里；本文件只负责两件事：判「用户是否正盯着」（isWatching）
// 与「同类通知怎么节流」（reply 走 30s 合并窗 + 60s 全局冷却，askUser/failure
// 即时弹不合并——等人/出事绝不节流）。

/** watching 三条件（应用可见 ∧ 工作区路由 ∧ 正盯该会话）——socket 回调闭包会
 *  过期，只读 ref/即时值。判据本体在 notifyHub.isSessionAttentive，与其余 6 个
 *  发通知的站点共用同一口径（P1-4）。 */
function isWatching(ctx: EngineCtx, sid: string): boolean {
  return isSessionAttentive(ctx.activeSidRef.current, sid);
}

/** 已通知过的 ask_user 问题 id（tool_call id，全局唯一）——重连重放不二次弹。 */
// 已通知过的 ask_user 问题 id（去重防「每次重连/重载都弹一遍等你回答」）。
// 2026-11-10：落 sessionStorage——问题 id 存在 checkpointer 里跨重启稳定，
// 但应用重启/webview 重载会清掉内存 Set，挂起未答的问题就会再弹一次
// （用户实录：ec0c5453 的挂起问题反复打扰）。sessionStorage 生命周期
// = 应用本次运行，退出即清，不越界持久。
const _NQI_KEY = "ginno:notified-question-ids";
function _loadNotifiedQuestionIds(): Set<string> {
  try {
    const raw = sessionStorage.getItem(_NQI_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}
const notifiedQuestionIds = _loadNotifiedQuestionIds();
function _saveNotifiedQuestionIds() {
  try {
    sessionStorage.setItem(_NQI_KEY, JSON.stringify([...notifiedQuestionIds]));
  } catch {
    /* 私密模式等写失败静默 */
  }
}

/** 去重集上限：超出按插入序淘汰最旧（JS Set 保序）。
 *  2026-11-14 改：旧实现是 `size > 1000 → clear()` 整表清空——delta 洪水把集
 *  灌满时会连**真实挂起问题**的去重记录一起抹掉，让 b088cc0 修的「重连重复弹」
 *  复发。逐条淘汰只丢最旧的，正在挂起的问题（最新）永远留着。 */
const _NQI_MAX = 1000;
function _pruneNotifiedQuestionIds() {
  while (notifiedQuestionIds.size > _NQI_MAX) {
    const oldest = notifiedQuestionIds.values().next().value;
    if (oldest === undefined) break;
    notifiedQuestionIds.delete(oldest);
  }
}

/** ask_user 的去重键。
 *
 *  正常路径是 tool_call id：ask_tools.py:359 把 `InjectedToolCallId` 放进中断
 *  载荷（`"id": tool_call_id or ""`），ws.py:286 的重放路径也带着它——id 在
 *  checkpoint / live 事件 / replay 三处稳定，正是前端合并幂等的基础。
 *
 *  id 为空是退化情形（工具被非 tool-call 路径调用等）。此时**不能**直接静默
 *  （那会漏掉真挂起的问题），也**不能**退回旧实现的 `${sid}:${Date.now()}`
 *  （每帧一把新钥匙 = 通知风暴根因）。改用「会话 + 问题文本」派生的稳定键：
 *  同一问题重放时文本不变 → 仍然去重；不同问题 → 各自一条。
 *
 *  注意本函数只可能被 `case "user.question"` 调到（见 handle() 里的共享 case
 *  体警告），delta/tool 帧已在结构上进不来，所以这里的兜底不会再被洪水灌满。 */
function _askDedupeKey(sid: string, ev: { [k: string]: unknown }): string {
  const rawId = typeof ev.id === "string" ? ev.id.trim() : "";
  if (rawId) return rawId;
  const subs = Array.isArray(ev.questions) ? (ev.questions as Array<{ question?: unknown }>) : [];
  const q = String(ev.question ?? "").trim() || String(subs[0]?.question ?? "").trim();
  return `q:${sid}:${q.slice(0, 120)}:${subs.length}`;
}

/** ask_user 挂起的 OS 通知（V2：最急的通知类，即时弹不合并——不看就永远卡着）。
 *
 *  **只允许 `case "user.question"` 调用**：见 handle() switch 里 tool.* 共享
 *  case 体的警告注释，与 docs/notifications-audit-2026-11-14.md P0-1。
 *
 *  去重（2026-11-05 真机回归）：WS 重连时服务端会把 parked 的挂起问题重发一遍
 *  （ws.py:244，给前端重建问题卡用，id 不变）——不按 id 去重的话每次重连/切会话
 *  都弹一遍「等你回答」（用户报：没有 ask_user 也弹）。
 *
 *  2026-11-14 加固（通知风暴根因）：去重键改走 `_askDedupeKey`——旧兜底
 *  `${sid}:${Date.now()}` 等于给每个 delta 帧发一把新钥匙，去重形同虚设。
 *  记 id 在闸门判定**之前**（与原实现同语义）：通知关着时也不会在开启后补弹。 */
function maybeNotifyAskUser(
  ctx: EngineCtx,
  sid: string,
  ev: { event: string; [k: string]: unknown },
) {
  const qid = _askDedupeKey(sid, ev);
  if (notifiedQuestionIds.has(qid)) return;
  notifiedQuestionIds.add(qid);
  _pruneNotifiedQuestionIds();
  _saveNotifiedQuestionIds();
  if (!shouldNotify("askUser", isWatching(ctx, sid))) return;
  const sTitle = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
  void notifyNative({
    kind: "session",
    notifyKind: "askUser",
    id: sid,
    title: `⏸ ${sTitle}`,
    body: ctx.i18nRef.current.tr("notify.askPending"),
  });
}

/** reply 合并条目：MergeItem + 多条时的计数文案（push 侧闭包捕获，
 *  i18nRef 延迟求值——ctx 长存所以安全）。 */
interface ReplyItem extends MergeItem {
  trN: (n: number) => string;
  /** flush 时**重新**判定「用户是否已经在看」。push 到 flush 隔着 30s，期间
   *  用户完全可能已经切进这个会话——那时再弹就是纯打扰（P1-3）。
   *  闭包捕获 ctx，与 trN 同理。 */
  attentive: () => boolean;
}

/** reply 类通知的**统一**节流出口（P1-3）。
 *
 *  修复前是两套互不相干的机制：`message.end` 走 30s 按会话合并窗、`linked.done`
 *  走 60s 全局冷却——而多会话并发完成时真正需要的是全局上限，偏偏主路径
 *  （message.end）没有。2026-11-10 为治刷屏加的 KindRateGate 只接到了
 *  linked.done 一路。现在两路都 push 进这同一个窗口，flush 时统一过全局冷却。
 *
 *  flush 时才判注意力与冷却（而非 push 时）：30s 窗口内用户的注意力会变，
 *  用过期判定决定现在要不要打扰是错的。 */
const replyMerge = new ReplyMergeWindow<ReplyItem>({
  onFlush: (sid, items) => {
    if (items.length === 0) return;
    // 用户已经切进这个会话了 → 不弹（他正在看）
    if (items[0].attentive()) return;
    // 60s 全局冷却：窗口内被吞掉的条数由下一次放行时带出（+N）
    const gate = replyRateGate.hit("reply");
    if (!gate.ok) return;
    const single = items.length === 1;
    const title = gate.suppressed > 0 ? `${items[0].title} (+${gate.suppressed})` : items[0].title;
    const body = single ? items[0].body : items[0].trN(items.length);
    const focusSession = () => {
      window.focus();
      // stable setter 语义见 desktop fallback —— 通过 CustomEvent 让
      // store 跳转（onFlush 里拿不到 ctx，走全局事件总线）。
      window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: sid }));
    };
    void notifyNative({
      kind: "session",
      notifyKind: "reply",
      id: sid,
      title,
      body,
    }).then((sent) => {
      if (sent) return;
      // sent=false 现在也包含「被偏好闸门拦下」（notifyNative 是唯一出口）。
      // 浏览器兜底必须自己再判一次，否则用户关掉了桌面提醒、dev 浏览器里
      // 反而照样弹 HTML5 通知。
      if (!osGateOpen("reply")) return;
      if (typeof Notification === "undefined") return;
      if (Notification.permission === "default") {
        try {
          void Notification.requestPermission();
        } catch {
          /* unsupported */
        }
        return;
      }
      if (Notification.permission !== "granted") return;
      try {
        const n = new Notification(title, { body: applyPrivacy(body) });
        n.onclick = () => {
          focusSession();
          n.close();
        };
      } catch {
        /* blocked/unsupported */
      }
    });
  },
});

/** 丢弃某个会话待发的 reply 合并通知（用户已经切进去了，不必再弹）。
 *
 *  `ReplyMergeWindow.cancel` 的注释从 V2 起就写着 "e.g. user switched to the
 *  session"，但直到本次修复**全仓库零调用点**——API 造好了没接线。由
 *  useChatStreamEngine 在 activeSessionId 变更的 effect 里调用。 */
export function cancelPendingReplyNotice(sid: string | null | undefined): void {
  if (sid) replyMerge.cancel(sid);
}


// AgentTeam blocked 跃迁去重（P1-18）：记录当前处于 blocked 的成员实例，
// 只在「非 blocked → blocked」时通知一次；离开 blocked 即移除，下次再卡可再弹。
const _teamBlockedSeen = new Set<string>();


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
 * 唯一的 turn_meta block——usage 事件累加 token，message.end 的 payload.meta
 * 定格（elapsedS 权威值替换 t0Ms）。历史投影版（reload）只有定格字段。 */
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
function stripFromLive(ctx: EngineCtx, sid: string, chunk: string) {
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
  switch (ev.event) {
    case "token.delta":
    case "thinking.delta":
      // 重试恢复出流：瞬态 auto-retry 提示撤行（若随后又失败，新的 notice
      // 会重新落地）。只认本会话还有挂着的提示时才动 store。
      if (ctx.retryNoticeRef.current[sid]) {
        const prev = ctx.retryNoticeRef.current[sid];
        ctx.retryNoticeRef.current[sid] = null;
        stripFromLive(ctx, sid, prev ?? "");
      }
    // fall through
    case "tool.start":
    case "tool.args":
    case "tool.end":
      // ⚠️ 共享 case 体：token.delta / thinking.delta 会 fall through 到这里，
      // 本体会被 **5 个事件**执行。任何「只对某一类事件成立」的副作用都不能放
      // 这里。2026-11-14 事故（docs/notifications-audit-2026-11-14.md P0-1）：
      // ask_user 的 OS 通知曾被塞进这个共享体，而 delta 帧没有 ev.id → 去重键
      // 退化成 `${sid}:${Date.now()}` 每帧唯一 → 每个 token 弹一条「等你回答」
      // （单会话实测 1583 条，shell.log 里 77% 是通知流量，40% 投递失败）。
      // 事件专属副作用一律拆到自己的 case 里（见下面 user.question）。
      adoptOrphanStream(ctx, sid);
      mutateLive(ctx, sid, ev);
      break;
    // Transcript block, not a ref-based prompt like permission.request: the
    // card lives (and is merged by id) inside the live bubble's blocks.
    case "user.question": {
      maybeNotifyAskUser(ctx, sid, ev);
      adoptOrphanStream(ctx, sid);
      mutateLive(ctx, sid, ev);
      break;
    }
    case "widget.emit":
    case "ref.emit":
    case "image.emit":
      adoptOrphanStream(ctx, sid);
      mutateLive(ctx, sid, ev);
      break;
    // LLM 请求级可观测（T3）：llm.request / llm.response 在 applyBlock 里按
    // (req_id, attempt) 配对合并成一条 llm_call 行并原地更新。与 tool 帧同
    // 范式（认领孤儿流 + mutateLive），但**独立 case 体**——不 fall through
    // 进上面 5 事件共享体（共享体副作用警告见 tool.* 注释）。
    case "llm.request":
    case "llm.response":
      adoptOrphanStream(ctx, sid);
      mutateLive(ctx, sid, ev);
      break;
    // workflow.emit：run 终态通知由 store/notifications.ts 统一发
    // （kind: workflow-run，先于本 V2 实现且带点击跳转 Workflows 面板）——
    // 此处不再重复弹（2026-11-05 触发点盘点：双通道同事件双弹）。
    case "workflow.emit":
      adoptOrphanStream(ctx, sid);
      mutateLive(ctx, sid, ev);
      break;
    case "turn.start": {
      markDelivered(ctx, sid);
      ctx.seenTurnStartRef.current[sid] = true;
      // 新 turn 清上一轮的 auto-retry 提示追踪（残留 chunk 属于旧气泡，
      // stripFromLive 找不到本来也无害，这里只是不让它跨 turn 存活）。
      ctx.retryNoticeRef.current[sid] = null;
      // 服务端起的 turn（子代理调度器的 brief/wake、goal 续跑）没有本地
      // attemptSend 帮忙置位 —— busy 必须在这里补。漏了它，一直开着的子会话
      // 视图 busy 恒为 false：running 中的输入走 invoke 而不是 steer，还被
      // runtime 排在 turn lock 后面悬到 turn 结束才变成新 turn（2026-10-10
      // 子会话 steering 失效）。attemptSend 已置位时重复写无害。
      ctx.busyBySessionRef.current[sid] = true;
      // Sidebar ordering (reactivated sessions float up): the runtime bumps
      // `updated` on every invoke, but only this socket knows it happened —
      // patch the store's copy so the day-group resort is live, not stale
      // until the next full reload.
      ctx.g.applySessionPatch(sid, { updated: Math.floor(Date.now() / 1000) });
      // authoritative agent for this turn (server-resolved, never null).
      // The server echoes the turn_id we sent (or mints one); adopt it as the
      // bubble's trace UUID so it matches the sidecar logs exactly.
      const srvTurn = ev.turn_id as string | undefined;
      const evAgent = (ev.agent_id as string) || null;
      // Headless (goal continuation) turns have NO client bubble yet — the
      // first token.delta would otherwise create one with a null agent and
      // render the generic "Agent". Prime the bubble here so the
      // server-provided agent name is kept (bug: continuation showed "Agent").
      if (evAgent) ctx.streamAgentRef.current[sid] = evAgent;
      const id = ctx.liveBySessionRef.current[sid] ?? ensureLive(ctx, sid);
      // turn-meta live 计时（turn 右下角耗时+token）：turn.start 是**所有**
      // 气泡来源都会经过的唯一事件——attemptSend 预建的空气泡占用了 liveId
      // （ensureLive 的新建分支被 `??` 短路，不会插 turn_meta）、ensureLive
      // 新建、孤儿认领都汇到这里。缺 meta 就补插，t0 取此刻（turn 真正开始，
      // 而非首个 usage 事件——那是模型首答完成时刻，慢网关下差几十秒）。
      // 已有则不动：interrupt resume 的续段保留原 t0，计时连续。
      {
        const lid = ctx.liveBySessionRef.current[sid];
        const m = lid ? (ctx.storeRef.current[sid] ?? []).find((x) => x.id === lid) : undefined;
        if (m && !m.blocks.some((b) => b.kind === "turn_meta")) {
          upsertTurnMeta(ctx, sid, { t0Ms: Date.now() });
        }
      }
      ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((msg) =>
        msg.id === id
          ? {
              ...msg,
              agentId: evAgent,
              agentName: (ev.name as string) || undefined,
              turnId: srvTurn || msg.turnId,
            }
          : msg,
      );
      // keep the user bubble's UUID in sync with the server's authoritative one
      if (srvTurn) {
        ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((msg, i, arr) =>
          msg.role === "user" && !msg.turnId && i === arr.length - 2
            ? { ...msg, turnId: srvTurn }
            : msg,
        );
      }
      // 渠道轮次回显（engine 端 echo_user 标记，turn.start 带 text/images）：
      // 桌面端没有 composer 的乐观气泡，IM 侧输入只能靠服务端回传才显示。
      // id 与 turnId 同用服务端 turnId——与 history 的 user 消息 id==turnId
      // 不变式（messages_ui.py）一致，后续 history 回灌天然去重；图片块与
      // history 的 multimodal 序列化同构（{kind:"image", url:dataURL}）。
      // 本地 composer 轮不带 text，且上面的同步已给乐观气泡打上 turnId，
      // 不会双插。
      const echoText = typeof ev.text === "string" ? ev.text : "";
      const echoImgs = Array.isArray(ev.images)
        ? ev.images.filter(
            (i: unknown): i is { data: string; media_type?: string } =>
              !!i && typeof (i as { data?: unknown }).data === "string",
          )
        : [];
      if (srvTurn && (echoText || echoImgs.length)) {
        const store = ctx.storeRef.current[sid] ?? [];
        if (!store.some((m) => m.role === "user" && m.turnId === srvTurn)) {
          // 信封折卡（<ginno_subagent_result>/<ginno_worker_result> 等）与
          // steer.absorbed 路径按 sessionId 互斥（2026-11-05 双卡修复：子代理
          // 回传先经 steer 注入折卡，wake turn echo 再折一次 → 同一结果
          // 两张卡；absorb 侧已有同款幂等，这里对齐——已存在同 sessionId 的
          // 结果卡时，echo 只保留非信封内容（纯文本渠道回显不受影响））。
          const echoBlocks = foldSubagentResultBlocks([
            ...(echoText ? [{ kind: "text" as const, text: echoText }] : []),
            ...echoImgs.map((i) => ({
              kind: "image" as const,
              url: `data:${i.media_type || "image/png"};base64,${i.data}`,
            })),
          ]).filter((b) => {
            const csid = (b as { sessionId?: string }).sessionId;
            if (!csid) return true;
            if (b.kind !== "subagent_result" && b.kind !== "worker_result") return true;
            return !store.some((m) =>
              m.blocks.some(
                (ob) =>
                  (ob.kind === "subagent_result" || ob.kind === "worker_result") &&
                  (ob as { sessionId?: string }).sessionId === csid,
              ),
            );
          });
          if (echoBlocks.length) {
            const echo: ChatMsg = {
              id: srvTurn,
              role: "user",
              blocks: echoBlocks,
              turnId: srvTurn,
            };
            const at = (() => {
              const liveIdx = store.findIndex((m) => m.id === id);
              return liveIdx === -1 ? store.length : liveIdx;
            })();
            const next = [...store];
            next.splice(at, 0, echo);
            ctx.storeRef.current[sid] = next;
          }
        }
      }
      break;
    }
    case "permission.request":
      ctx.permsRef.current[sid] = { tool: ev.tool as string, args: ev.args };
      break;
    case "version.propose":
      ctx.proposeRef.current[sid] = {
        workflow_id: ev.workflow_id as string,
        from_version: (ev.from_version as number) ?? 0,
        diff: (ev.diff as string) ?? "",
        rationale: (ev.rationale as string) ?? "",
      };
      break;
    case "todos.changed":
      ctx.g.reloadTodos();
      break;
    case "skills.changed":
      // A turn (install_skills tool, bash) or the Settings page mutated
      // ~/.ginno/skills — refresh the slash menu's skill list live.
      ctx.g.reloadSkills();
      break;
    case "memory.changed":
      // Memory refinery transition (auto-draft ready, applied, discarded) —
      // refresh the Memory tab badge; the panel itself refetches on open.
      ctx.g.reloadMemoryBadge();
      break;
    case "agents.changed":
      // Agent CRUD in Settings — keep the picker/mention list in sync.
      ctx.g.reloadAgents();
      break;
    case "session.model_changed": {
      // 模型热切回流（model-assignment-design.md §3.2）：服务端已原地换掉
      // 会话条目的模型对象，下一个 superstep 生效。发起端乐观更新过，这里
      // 兜底其它视图（回放页/另一窗口）并给一条轻提示。applySessionPatch
      // 是裸 spread，未携带的键不能以 undefined 进 patch（会清字段）。
      const patch: Partial<SessionMeta> = {};
      if (typeof ev.provider === "string") patch.provider = ev.provider;
      if (typeof ev.model === "string") patch.model = ev.model;
      // 思考档位随事件同步（thinking-effort-design §6）。
      const evEffort = (ev as Record<string, unknown>).thinking_effort;
      if (typeof evEffort === "string") {
        patch.thinking_effort = evEffort as SessionMeta["thinking_effort"];
      }
      if (patch.provider || patch.model || patch.thinking_effort) {
        ctx.g.applySessionPatch(sid, patch);
      }
      // 轻提示走**公共通知层**（notice-center-design.md）：旧实现借用
      // chat/mod 的右下角 toast 宿主，语义错位且 mods 未挂载时帧直接丢弃。
      // dedupeKey 按会话去重——多窗口/回放页收到同一事件只弹一条。
      // inbox:false（2026-11-08 用户反馈「切换模型没必要进通知面板」）：
      // 操作确认类反馈——toast 几秒即逝，不进通知中心不留档。
      {
        const { tr } = ctx.i18nRef.current;
        const key = patch.model ? "model.switchToast" : "model.switchToastNoModel";
        pushNotice("success", tr(key, patch.model ? { model: patch.model } : undefined), {
          source: "model",
          dedupeKey: `model:${sid}`,
          inbox: false,
        });
      }
      break;
    }
    case "workflows.changed":
      ctx.g.reloadWorkflows();
      ctx.g.reloadWorkflowRuns();
      break;
    case "synthesis.event":
      // Background summarization progressed — refresh the 总结 panel list
      // (started/attempt/finished all change what it shows). If THIS is the
      // case the summarize button is waiting on, resolve the wait (the ref
      // guard inside finishSynthesisWait dedupes vs the polling fallback).
      void ctx.g.reloadSynthesisCases();
      if (ev.kind === "finished" && ev.synthesis_id === ctx.sumPendingRef.current) {
        void ctx.finishSynthesisWait(ev.synthesis_id as string);
      }
      break;
    case "run.bind": {
      const runId = ev.run_id as string;
      // 侧栏伪条目即时出现（run view 方案）：in-chat 卡片走下面的拉取，
      // store 的全量运行列表也同步刷新，工作流面板/角标不用等 30s 轮询。
      void ctx.g.reloadWorkflowRuns();
      getWorkflowRun(runId).then((r) => {
        if (!r?.run) return;
        const list = ctx.runsBySessionRef.current[sid] ?? [];
        if (!list.some((x) => x.id === runId)) list.push(r.run);
        ctx.runsBySessionRef.current[sid] = [...list];
        ctx.syncDisplay(sid);
      });
      break;
    }
    case "run.event": {
      const runId = ev.run_id as string;
      const inner = (ev.payload ?? {}) as Record<string, unknown>;
      const list = ctx.runsBySessionRef.current[sid] ?? [];
      const run = list.find((x) => x.id === runId);
      // Live tool-call visibility (workflow-ux-redesign P1): show the
      // in-flight tool under the running step; results/exit clear it.
      const innerKind = inner.kind as string | undefined;
      if (innerKind === "tool_call") {
        const calls = (inner.calls as Array<{ name?: string; args?: unknown }>) ?? [];
        const latest = calls[calls.length - 1]; // batched calls: show the newest
        if (latest?.name) {
          ctx.g.notifyRunToolActivity(runId, {
            nodeId: (inner.node_id as string) ?? "",
            toolName: latest.name,
            argsPreview: toolArgsPreview(latest.args),
          });
        }
      } else if (
        innerKind === "tool_result" || innerKind === "node_exit" ||
        innerKind === "error" || innerKind === "done"
      ) {
        ctx.g.notifyRunToolActivity(runId, null);
      }
      if (run) {
        const nid = inner.node_id as string | undefined;
        const kind2 = inner.kind as string | undefined;
        // Mirror the server's per-event _touch_run: without this the in-chat
        // card's adaptive stuck check fires during long steps that DO emit
        // tool traffic (the 1.5s panel poll doesn't cover the chat list).
        run.updated = Date.now() / 1000;
        if (nid && (kind2 === "node_enter" || kind2 === "node_exit")) {
          // node_exit carries the step's real outcome — a failed step must not
          // render as done (green) in the live card.
          const stepStatus =
            kind2 === "node_enter" ? "running" : inner.status === "failed" ? "failed" : "done";
          run.steps = run.steps.map((s) => (s.id === nid ? { ...s, status: stepStatus } : s));
          ctx.runsBySessionRef.current[sid] = [...list];
        } else if (kind2 === "interrupt") {
          // A node suspended the graph (P1): stamp the payload so the card
          // renders immediately, without waiting for the reload round-trip.
          // nature: "human" (question card) vs "manual" (user pause, #14);
          // HumanNode events carry no nature and default to human. The step
          // flips to running (done on resume — except manual pauses, whose
          // step re-executes and settles via node_enter/exit).
          run.pending_interrupt = {
            ...(inner as object),
            kind: (inner.nature as string) || "human",
          } as typeof run.pending_interrupt;
          if (nid) run.steps = run.steps.map((s) => (s.id === nid ? { ...s, status: "running" } : s));
          ctx.runsBySessionRef.current[sid] = [...list];
        } else if (kind2 === "resume") {
          run.pending_interrupt = null;
          if (nid && inner.nature !== "manual") {
            run.steps = run.steps.map((s) => (s.id === nid ? { ...s, status: "done" } : s));
          }
          ctx.runsBySessionRef.current[sid] = [...list];
        } else if (kind2 === "error") {
          // Show the failure one beat before run.status lands, and stamp the
          // structured diagnostic so RunErrorBox renders without a lazy fetch.
          if (typeof inner.error === "string") run.error = inner.error;
          run.error_detail = {
            node_id: (inner.node_id as string | null) ?? null,
            traceback: (inner.traceback as string | undefined) ?? null,
          };
          ctx.runsBySessionRef.current[sid] = [...list];
        }
        ctx.syncDisplay(sid);
      }
      break;
    }
    case "run.status": {
      const runId = ev.run_id as string;
      const status = ev.status as string;
      const list = ctx.runsBySessionRef.current[sid] ?? [];
      const run = list.find((x) => x.id === runId);
      if (run) {
        run.status = status;
        if (typeof ev.error === "string") run.error = ev.error;
        // P1: the paused push carries WHY (human question); terminal states
        // and fresh resumes clear it.
        if (status === "paused") {
          run.pending_interrupt =
            (ev.pending_interrupt as typeof run.pending_interrupt) ?? run.pending_interrupt ?? null;
        } else {
          run.pending_interrupt = null;
        }
        ctx.runsBySessionRef.current[sid] = [...list];
      }
      ctx.syncDisplay(sid);
      ctx.g.reloadWorkflowRuns();
      break;
    }
    case "artifacts.changed":
      ctx.g.reloadArtifacts();
      ctx.g.reloadProjectArtifacts(); // @ 菜单的跨会话列表同源刷新
      break;
    case "code.changed":
      // The agent wrote or edited a file (design §4.7 S3). The event carries
      // the POST-WRITE version, which is what lets the code panel reuse its
      // conflict bar for a dirty buffer instead of inventing a second UI —
      // see the S3 brief §0.
      if (typeof ev.path === "string" && (ev.op === "write" || ev.op === "edit")) {
        ctx.g.notifyCodeChange({
          path: ev.path,
          op: ev.op as "write" | "edit",
          version: typeof ev.version === "string" ? ev.version : "",
          // Live-diff contract (code-panel-diff-design.md §6.3): the baseline
          // version the agent's write was anchored on; null when absent.
          baseVersion: typeof ev.base_version === "string" ? ev.base_version : null,
          turnId: typeof ev.turn_id === "string" ? ev.turn_id : undefined,
        });
      }
      break;
    case "preview.emit":
      // Agent produced a previewable file (e.g. analysis result) → open it.
      if (ev.open && ev.file_id) {
        ctx.g.openPreview({
          id: ev.file_id as string,
          name: (ev.name as string) || "result",
          path: (ev.path as string) || "",
          kind: ev.kind as string | undefined,
        });
      }
      ctx.g.reloadArtifacts();
      break;
    case "preview.invalidate":
      // A tracked file changed (tool wrote it / mtime watcher) → the
      // FileViewer refetches if that file is the one being viewed.
      if (ev.file_id) ctx.g.notifyPreviewInvalidate(ev.file_id as string);
      break;
    case "worker.waiting":
    case "worker.progress":
    case "worker.resumed": {
      // 等待可见化三态（2026-11-06「assign 后 5 分钟像卡死」；2026-11-08
      // 语义化）：waiting = 订阅建立（等待起点）；progress = digest 进度
      // 节选到达（**不是完成**）；resumed = 终态结果注入。文案全部带目标
      // 标题，用户一眼知道是哪个会话、发生了什么。
      {
        const title =
          (typeof ev.target_title === "string" && ev.target_title.trim()) ||
          ctx.g.sessions.find((x) => x.id === ev.target)?.title?.trim() ||
          "…";
        if (ev.event === "worker.waiting") {
          pushNotice(
            "info",
            ctx.i18nRef.current.tr("worker.waitingNotice", { title }),
            { dedupeKey: `${ev.event}-${ev.target}` },
          );
        } else if (ev.event === "worker.progress") {
          pushNotice(
            "info",
            ctx.i18nRef.current.tr("worker.progressNotice", { title }),
            { dedupeKey: `${ev.event}-${ev.target}` },
          );
        } else {
          pushNotice(
            "info",
            ctx.i18nRef.current.tr("worker.resumedNotice", { title }),
            { dedupeKey: `${ev.event}-${ev.target}` },
          );
        }
      }
      break;
    }
    case "linked.done": {
      // 订阅的会话 turn 完成（2026-11-06 闭环）：📡 前缀 OS 通知 + 点击直达
      // 目标会话。origin=schedule/subagent 的完成已有专属通道，服务端已过滤
      // （engine.py:1303 只放行 interactive/deliver），此处不再判。
      //
      // P1-3：改走与 message.end **同一个** replyMerge 窗口。修复前这一路单独
      // 用 60s 全局冷却、message.end 单独用 30s 按会话合并，两套机制互不相干
      // ——多会话并发完成时真正需要的全局上限偏偏不在主路径上。现在冷却统一
      // 在 flush 时施加，两路共享同一个计数（+N）。
      const targetSid = typeof ev.session_id === "string" ? ev.session_id : "";
      if (!targetSid) break;
      if (!shouldNotify("reply", isWatching(ctx, sid))) break;
      const title = ctx.g.sessions.find((x) => x.id === targetSid)?.title?.trim() || "…";
      const raw = typeof ev.text === "string" ? ev.text.trim() : "";
      // worker 语义修正：worker_status=working/waiting 说明它只是完成了一段
      // （还在干活/等下一段），文案不再说「回复完成」。
      const wst = typeof ev.worker_status === "string" ? ev.worker_status : "";
      const body =
        wst === "working"
          ? ctx.i18nRef.current.tr("notify.linkedTurnOngoing")
          : wst === "waiting"
            ? ctx.i18nRef.current.tr("notify.linkedTurnWaiting")
            : raw || ctx.i18nRef.current.tr("notify.replyCompleted");
      replyMerge.push(targetSid, {
        title: `📡 ${title}`,
        body,
        trN: (n) => ctx.i18nRef.current.tr("notify.nCompleted", { n }),
        // 保持既有语义：正盯着**自己**的会话时不弹（📡 chip 徽标已可见）。
        // 判的是本会话 sid，不是 targetSid——flush 时复检，30s 内切走/切回
        // 都以当下为准。
        attentive: () => isWatching(ctx, sid),
      });
      break;
    }
    case "steer.recalled": {
      // ↑ 取回对账（steering 取回协议 2026-11-05）：服务端真实删除集
      // 回传——命中的条目此刻才弹回 composer；未命中的已被吸收，照常注入。
      const ids = Array.isArray(ev.steer_ids)
        ? ev.steer_ids.map(String)
        : [];
      settleRecalled(ctx, sid, ids);
      break;
    }
    case "steer.accepted":
      // The server stashed the entry (design §3.1). It stays in the queue bar
      // until it is absorbed; this only flips it out of "sending" (the shared
      // hook owns that queue mutation).
      ctx.steerQ.handleEvent(ev, sid);
      break;
    case "steer.answered":
      // 服务端把这条 steer 当作 parked 问题的答案直接恢复了 turn:不入队、
      // 不会有注入带,条目收走即可(卡片回执由 tool.end 折叠)。
      ctx.steerQ.handleEvent(ev, sid);
      break;
    case "steer.absorbed": {
      // The steered message was DRAINED into state (the server acks at drain
      // time, not at superstep commit — see docs/steering-design.md): move it
      // out of the queue bar into the transcript as a band at the injection
      // point — appended to the live assistant bubble, because one turn is one
      // bubble (design §4.2/§4.3).
      const absorbedId = ev.steer_id as string | undefined;
      if (!absorbedId) break;
      // 系统递归预警（engine 步数护栏，steer_id 前缀区分——_is_user_steer
      // 同款判据）是模型侧指令：不以用户注入带外显，live 与重放（fold 的
      // steer 分支）一致隐藏。
      if (absorbedId.startsWith("recursion-warn-")) break;
      // 后台任务事件的 steer 注入（tasks/inject，origin="task"）：事件帧带
      // ginno_task_event 载荷——折成极薄 task_event 标记行，状态由 bg_run
      // 常驻 TaskCard 轮询呈现，绝不渲染 "[task finished]…" 原文带。
      if (ev.task && typeof ev.task === "object" && (ev.task as { task?: unknown }).task) {
        const tp = ev.task as { task: string; kind?: string; title?: string; exit?: number | null; dur?: string };
        const tevBlock: Block = {
          kind: "task_event",
          taskId: String(tp.task),
          ev: String(tp.kind || ""),
          title: tp.title ? String(tp.title) : undefined,
          exit: tp.exit ?? null,
          dur: tp.dur ? String(tp.dur) : undefined,
        };
        const liveMsgId0 = ctx.liveBySessionRef.current[sid];
        const store0 = ctx.storeRef.current[sid] ?? [];
        if (liveMsgId0 && store0.some((m) => m.id === liveMsgId0)) {
          ctx.storeRef.current[sid] = store0.map((m) =>
            m.id === liveMsgId0 ? { ...m, blocks: [...m.blocks, tevBlock] } : m,
          );
        } else {
          ctx.storeRef.current[sid] = [
            ...store0,
            { id: mid(), role: "system" as const, blocks: [tevBlock] },
          ];
        }
        ctx.syncDisplay(sid);
        break;
      }
      // Capture the entry BEFORE delegating — the hook drops it from the queue.
      const entry = ctx.steerQ.itemsFor(sid).find((i) => i.steerId === absorbedId);
      // 渠道注入的 steer（IM 侧 steer_channel_turn 入队，本地没有队列条目）：
      // 事件带原文（engine 的 absorbed payload 补了 text）——同样渲染注入带，
      // 桌面实时可见；无本地条目也无 text（旧服务端/机器注入）维持 no-op。
      const foreignText = !entry && typeof ev.text === "string" ? ev.text : "";
      if (!entry && !foreignText)
        break; // already dropped — a history reconcile got there first
      // Same timing/order as before: remove from the queue first, then render.
      if (entry) ctx.steerQ.handleEvent(ev, sid);
      // 运行中注入的信封统一折卡（复用 mapHistory 的 foldSubagentResultBlocks，
      // live/重放同一解析器天然对齐）：子代理结果 / worker 回传·digest·派活 /
      // 清单状态 / 旧版委托，entry（本地队列）与 foreignText（服务端直接
      // steer_enqueue：worker digest、子代理回传——本地无条目，原文只随
      // absorbed 事件带回）同路径。折不出信封（用户/渠道/团队普通文本）
      // 回落通用注入带。
      const effText = entry ? entry.text : foreignText;
      const foldedCards = foldSubagentResultBlocks([
        { kind: "text" as const, text: effText },
      ]).filter((b) => b.kind !== "text");
      if (foldedCards.length) {
        const store = ctx.storeRef.current[sid] ?? [];
        // 幂等：同一 child 的回传卡已在 store（absorb 事件重放/重连补发）
        // 不重复插；无 sessionId 的卡（plan/delegation）不做去重。
        const fresh = foldedCards.filter((b) => {
          const csid = (b as { sessionId?: string }).sessionId;
          if (!csid) return true;
          return !store.some((m) =>
            m.blocks.some(
              (ob) =>
                (ob.kind === "subagent_result" || ob.kind === "worker_result") &&
                (ob as { sessionId?: string }).sessionId === csid,
            ),
          );
        });
        if (fresh.length) {
          ctx.storeRef.current[sid] = [
            ...store,
            { id: mid(), role: "system" as const, blocks: fresh },
          ];
          ctx.syncDisplay(sid);
        }
        break;
      }
      const band: Block = {
        kind: "steer",
        text: entry ? entry.text : foreignText,
        steerId: absorbedId,
        injectedAt: Number(ev.injected_at) || Math.floor(Date.now() / 1000),
        // Attachments ride the band so the injection point shows what the
        // user actually sent. Images arrive as display URLs (data URL), files
        // as name/path chips — mirroring the replay shape the server emits.
        ...(entry?.images.length
          ? { images: entry.images.map((a) => ({ name: a.name, url: a.preview })) }
          : {}),
        ...(entry?.files.length
          ? {
              files: entry.files.map((f) => ({
                id: f.id,
                name: f.name,
                path: f.path,
                kind: f.kind,
              })),
            }
          : {}),
      };
      const liveMsgId = ctx.liveBySessionRef.current[sid];
      const store = ctx.storeRef.current[sid] ?? [];
      if (liveMsgId && store.some((m) => m.id === liveMsgId)) {
        ctx.storeRef.current[sid] = store.map((m) =>
          m.id === liveMsgId ? { ...m, blocks: [...m.blocks, band] } : m,
        );
      } else {
        // No live bubble of ours (adopted orphan / goal continuation): the
        // band lands as its own full-width row, and the next history
        // reconcile rebuilds the merged bubble from the checkpoint.
        ctx.storeRef.current[sid] = [
          ...store,
          { id: mid(), role: "user" as const, blocks: [band], turnId: entry?.turnId },
        ];
      }
      ctx.syncDisplay(sid);
      break;
    }
    case "delegate_update": {
      // P1 委托实时进度：运行中的系统行同 id 刷新；running=false 撤行
      //（结果卡由 subagent.status 通道补位，已有 dedup 与跳转）。
      const did = ev.delegation_id as string;
      if (!did) break;
      const liveId = `delegate-live-${did}`;
      const store0 = ctx.storeRef.current[sid] ?? [];
      const without = store0.filter((m) => m.id !== liveId);
      if (ev.running === false) {
        ctx.storeRef.current[sid] = without;
        // 显示同步必须走 syncDisplay(带当前会话守卫):委托运行期间本事件
        // 持续打在父会话 socket 上,若用户正在看别的会话(典型:运行中的
        // 子会话回放页),裸 setMessages 会把父会话消息刷进当前视图——
        // "subsession 自动刷新主 session 内容"即此。store 照常落,切回
        // 父会话时由 syncDisplay 统一呈现。
        ctx.syncDisplay(sid);
      } else {
        const nxt = [
          ...without,
          {
            id: liveId,
            role: "system" as const,
            blocks: [
              {
                kind: "text",
                text: `🤖 ${ev.backend ?? "delegation"} · ${ev.text ?? "working…"}`,
              } as Block,
            ],
          },
        ];
        ctx.storeRef.current[sid] = nxt;
        ctx.syncDisplay(sid);
      }
      break;
    }
    case "session.state_changed": {
      // P1 状态机（2026-11-11）：compacting → spinner + 输入锁定；
      // idle → 解除。running/paused 由既有 turn 事件驱动，此处只补
      // compacting/swapping 两个新态的 UI 映射。
      const stSid = String(ev.session_id ?? "");
      if (!stSid) break;
      const st = String(ev.state ?? "");
      if (st === "compacting") {
        ctx.compactingBySessionRef.current[stSid] = true;
      } else if (st === "idle") {
        ctx.compactingBySessionRef.current[stSid] = false;
      }
      ctx.syncDisplay(stSid);
      break;
    }
    case "runtime.devswap_progress": {
      // dev-swap 热切换进度（docs/dev-swap-progress-design.md §2.2）：sidecar
      // 广播的阶段事件推入模块级总线，DevSwapCard / session 状态行订阅呈现。
      // 不 markDelivered——这不是本会话 turn 的帧，只是路过本 socket。
      const run = normalizeDevSwapRun(ev);
      if (run) pushDevSwapRun(run);
      break;
    }
    case "runtime.lastwords_pending": {
      // 遗言恢复确认（2026-11-16 交互补齐）：旧实现只投一条通知、点击仅跳
      // 会话，靠注入带里的「请从中断处继续」让 agent 自己动——用户没有按钮
      // 可点（「遗言好像没触发过」的直接原因之一）。现在通知卡片即决策面：
      // 「恢复」真调 /api/lastwords/resume，「忽略」调同端点 dismiss:true；
      // 常驻（duration:0）等用户决策，绝不 6s 自动消失。点卡片本体仍跳会话
      // （actionMeta 保留）。后端契约：人工会话 + 任意 reason + notify/auto
      // 都发本事件；payload 带 head（请求摘要）与 bg_interrupted（被重启杀掉
      // 的后台任务数）——文案必须呈现，用户才知道要恢复的是什么。
      const lpSid = String(ev.session_id ?? "");
      if (!lpSid) break;
      {
        const { tr } = ctx.i18nRef.current;
        const tools = Number(ev.tool_calls ?? 0);
        const secs = Math.max(0, Math.round(Number(ev.elapsed_s ?? 0)));
        const mm = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
        // head / user_request_head 双键兼容（后端两个都发）；摘要缺失回落
        // 占位文案，不让卡片出现空引号。用 || 而非 ??：head 为空串时也取备用键。
        const head =
          String(ev.head || ev.user_request_head || "").trim() ||
          tr("notice.lastwords-no-head");
        const bgInterrupted = Number(ev.bg_interrupted ?? 0);
        const text = tr("notice.lastwords-pending", { head, tools, elapsed: mm, bgInterrupted });
        const pendingKey = `lastwords-pending:${lpSid}`;
        const jumpMeta = { kind: "session" as const, id: lpSid };
        // 反馈复用 pending dedupeKey：收件箱条目原位刷新成结果文案，不残留
        // 过期的「要恢复吗」；失败卡带重试按钮（错误不吞、可再点）。
        const doResume = () => {
          void (async () => {
            try {
              const r = await resumeLastwords(lpSid);
              if (r?.ok) {
                pushNotice("success", tr("notice.lastwords-resume-ok"), {
                  source: "general",
                  kind: "lastwords",
                  dedupeKey: pendingKey,
                  actionMeta: jumpMeta,
                });
              } else if (r?.error === "no_pending") {
                // 条目已过期/已处置（TTL 或别处已确认）——明确告知，不重试。
                pushNotice("warn", tr("notice.lastwords-resume-expired"), {
                  source: "general",
                  kind: "lastwords",
                  dedupeKey: pendingKey,
                  actionMeta: jumpMeta,
                });
              } else {
                pushNotice(
                  "error",
                  tr("notice.lastwords-resume-failed", { error: String(r?.error ?? "unknown") }),
                  {
                    source: "general",
                    kind: "lastwords",
                    duration: 0,
                    dedupeKey: pendingKey,
                    actionMeta: jumpMeta,
                    action: { label: tr("notice.lastwords-resume-btn"), onClick: doResume },
                  },
                );
              }
            } catch (e) {
              pushNotice(
                "error",
                tr("notice.lastwords-resume-failed", {
                  error: e instanceof Error ? e.message : String(e),
                }),
                {
                  source: "general",
                  kind: "lastwords",
                  duration: 0,
                  dedupeKey: pendingKey,
                  actionMeta: jumpMeta,
                  action: { label: tr("notice.lastwords-resume-btn"), onClick: doResume },
                },
              );
            }
          })();
        };
        const doDismiss = () => {
          void (async () => {
            try {
              await dismissLastwords(lpSid);
              pushNotice("info", tr("notice.lastwords-dismissed"), {
                source: "general",
                kind: "lastwords",
                dedupeKey: pendingKey,
                actionMeta: jumpMeta,
              });
            } catch (e) {
              pushNotice(
                "error",
                tr("notice.lastwords-dismiss-failed", {
                  error: e instanceof Error ? e.message : String(e),
                }),
                {
                  source: "general",
                  kind: "lastwords",
                  duration: 0,
                  dedupeKey: pendingKey,
                  actionMeta: jumpMeta,
                  action: { label: tr("notice.lastwords-dismiss-btn"), onClick: doDismiss },
                },
              );
            }
          })();
        };
        pushNotice("info", text, {
          source: "general",
          kind: "lastwords",
          countInBadge: true,
          dedupeKey: pendingKey,
          duration: 0,
          actionMeta: jumpMeta,
          action: { label: tr("notice.lastwords-resume-btn"), onClick: doResume },
          secondaryAction: { label: tr("notice.lastwords-dismiss-btn"), onClick: doDismiss },
        });
        // 同步发 OS 通知。待人确认的恢复属于高优先级：不走 reply 的合并窗与
        // 60s 冷却门（那是给「完成」类刷屏用的），直接即时弹。
        // P1-5：但仍必须过偏好闸门——修复前这一路连总开关都不看，用户关掉
        // 「启用桌面提醒」它照样弹。归类 askUser（等人确认），闸门由
        // notifyNative 出口统一施加。
        void notifyNative({
          kind: "session",
          notifyKind: "askUser",
          id: lpSid,
          title: `⚡ ${tr("notice.lastwords-pending-title")}`,
          body: text,
        });
      }
      break;
    }
    case "runtime.lastwords_restored": {
      // 遗言恢复回执（docs/lastwords-design.md P2 通知中心待恢复列表）：
      // 新 sidecar 启动扫描后广播——投一条收件箱条目（kind:"lastwords"，
      // countInBadge:true，dedupeKey 按会话去重），action 跳回原会话。
      // 不 markDelivered——非本会话 turn 的帧。
      const lwSid = String(ev.session_id ?? "");
      if (!lwSid) break;
      {
        const { tr } = ctx.i18nRef.current;
        pushNotice("info", tr("notice.lastwordsRestored"), {
          source: "general",
          kind: "lastwords",
          countInBadge: true,
          dedupeKey: `lastwords:${lwSid}`,
          actionMeta: { kind: "session", id: lwSid },
        });
      }
      break;
    }
    case "runtime.dev_swap_pending": {
      // 开发热切换（docs/runtime-hot-swap-design.md §3.4）：CLI 已把 symlink
      // 切到新 bundle。2026-11-05 用户需求：切换需用户确认——不再自动 invoke
      // finish_dev_swap，改为点亮确认条（DevSwapCard 确认区，TopBar 常驻可见）。
      // 「暂不」= 不重启，老 sidecar 继续跑，下次重启自然吃到新 bundle。
      // G1（2026-11-11）：auto=true = 企微侧已 y 确认——跳过弹框直接 invoke
      // （L2 低延迟路径；壳 watcher L3/L4 兜 webview 不在的情况）。
      markDelivered(ctx, sid);
      void (async () => {
        const { setDevSwapPendingConfirm } = await import("@/lib/devswap/bus");
        if (ev.auto === true) {
          setDevSwapPendingConfirm(null);
          try {
            const { invoke } = await import("@tauri-apps/api/core");
            await invoke("finish_dev_swap");
          } catch {
            /* invoke 失败（非桌面/webview 不可用）→ 壳 watcher 兜底 */
          }
          return;
        }
        setDevSwapPendingConfirm("pending");
      })();
      break;
    }
    case "deliver.report": {
      // @session 投递回执（at-session-design §5.1）：系统提示条，非气泡——
      // 各目标状态 started/queued/missing/no_body + 「附件未随投递」注明。
      const targets: Array<{ label?: string; state?: string }> = Array.isArray(ev.targets)
        ? ev.targets
        : [];
      const stateText = (st: string, label: string) => {
        if (st === "started") return ctx.i18nRef.current.tr("deliver.started", { label });
        if (st === "queued") return ctx.i18nRef.current.tr("deliver.queued", { label });
        if (st === "missing") return ctx.i18nRef.current.tr("deliver.missing", { label });
        return ctx.i18nRef.current.tr("deliver.noBody", { label });
      };
      const lines = targets.map((t) => stateText(String(t.state), String(t.label ?? "")));
      if (ev.context_skipped) lines.push(ctx.i18nRef.current.tr("deliver.ctxSkipped"));
      markDelivered(ctx, sid);
      // notice 同款：live 气泡内一段 system 文本（无 graph turn，非 delta 流）。
      ensureLive(ctx, sid, typeof ev.turn_id === "string" ? ev.turn_id : undefined);
      mutateLive(ctx, sid, {
        event: "token.delta",
        content: lines.join("\n"),
      });
      break;
    }
    case "notice":
      // Built-in command reply (e.g. /help): no graph turn ran, so the server
      // pushes the rendered text directly into the live bubble as one delta.
      // 事件契约：带 i18n_key 时落地即翻译，否则原样直显 message。
      markDelivered(ctx, sid);
      let _txt = ctx.i18nRef.current.evText(ev, (ev.message as string) || "");
      // 瞬态模型错误提示（auto_retry / model_error_backoff）——包成 code
      // span + ⚠️，气泡里呈现为琥珀色等宽小条而不是混入正文的普通文本
      // （用户反馈样式不显眼）。model_error_backoff 于 2026-11-08 并入：
      // 此前它走通用分支，GLM 429 退避横幅被拼进消息开头且重试成功后
      // 不撤（用户截图实录），多轮退避还会堆多行。
      if (
        (ev.i18n_key as string) === "stream.auto_retry" ||
        (ev.i18n_key as string) === "stream.model_error_backoff"
      ) {
        // 原位更新（2026-10-11 用户反馈：此前每次 append，N 轮重试堆 N 行）：
        // 新一轮提示替换上一轮；文本里带分隔符整体进出，替换/撤行都精确。
        const prev = ctx.retryNoticeRef.current[sid];
        if (prev) stripFromLive(ctx, sid, prev);
        const live = (ctx.storeRef.current[sid] ?? []).find(
          (m) => m.id === ctx.liveBySessionRef.current[sid],
        );
        const lastB = (live?.blocks[live.blocks.length - 1] ?? null) as {
          kind?: string;
          text?: string;
        } | null;
        const sep =
          lastB && (lastB.kind === "text" || lastB.kind === "thinking") && lastB.text
            ? "\n\n"
            : "";
        const chunk = `${sep}\`⚠️ ${_txt}\``;
        ctx.retryNoticeRef.current[sid] = chunk;
        mutateLive(ctx, sid, { event: "token.delta", content: chunk });
        break;
      }
      mutateLive(ctx, sid, {
        event: "token.delta",
        content: _txt,
      });
      break;
    case "goal.updated":
      // Live goal snapshot (created/status/accounting) → TopBar chip.
      ctx.g.notifyGoal(sid, (ev.goal as Goal) ?? null);
      break;
    case "team.changed":
      // AgentTeam 聚合状态回流：@ 投递菜单可见范围/总览卡都吃 store.teams。
      if (ev.team) ctx.g.notifyTeamUpdated(ev.team as never);
      // P1-18：成员**转入** blocked → 通知（设计里最有价值的一态：它卡住了，
      // 需要用户去代答）。只在状态跃迁时弹，不是每次 team.changed 都弹。
      {
        const t = ev.team as { id?: string; member_rows?: Array<{ instance_id?: string; display_name?: string; status?: string }> } | undefined;
        const rows = t?.member_rows ?? [];
        for (const r of rows) {
          const iid = String(r.instance_id || "");
          if (!iid) continue;
          if (r.status === "blocked") {
            if (!_teamBlockedSeen.has(iid)) {
              _teamBlockedSeen.add(iid);
              const body = ctx.i18nRef.current.tr("team.notifyBlocked", {
                name: r.display_name || iid,
              });
              pushNotice("warn", body, { dedupeKey: `team-blocked-${iid}` });
              if (shouldNotify("team", isWatching(ctx, sid))) {
                const title = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
                // 新契约（main 7c8fe8a 闸门收口）：notifyNative 是唯一出口，
                // 内部施加 osGateOpen(notifyKind) → applyPrivacy → soundFor。
                // 调用点只给原文 body + notifyKind，不再自己裁隐私/推提示音。
                // 外层 shouldNotify 保留——main 注释明写这是有意的双保险。
                void notifyNative({
                  kind: "session",
                  notifyKind: "team",
                  id: sid,
                  title: `⛔ ${title}`,
                  body,
                });
              }
            }
          } else {
            _teamBlockedSeen.delete(iid);
          }
        }
      }
      break;
    case "plan.updated":
      // 任务清单实时刷（plan-orchestration §2）：watcher 每次落盘推整张
      // 清单快照；store 按 plan_id upsert，PlanBoard 重渲染。
      if (ev.plan) ctx.g.notifyPlanUpdated(ev.plan as Plan);
      break;
    case "goal.archived":
      // Lifecycle finish (2026-11-16): complete goal auto-archived at turn
      // end — record moved to goals-history.jsonl, chip clears like cleared.
    case "goal.cleared":
      ctx.g.notifyGoal(sid, null);
      break;
    case "session_title":
      // Auto-title from the first user message (runtime _touch_session_title):
      // sidebar + TopBar rename live without a sessions reload.
      ctx.g.applySessionPatch(sid, { title: (ev.title as string) ?? "", title_auto: false });
      break;
    case "session.context":
      // Mount set changed server-side (context-folders-design.md): /mount
      // command or another client ran PUT /sessions/{id}/context. Patch the
      // store so the TopBar chip re-renders without a full reload.
      ctx.g.applySessionPatch(sid, {
        context_folders: ((ev.context_folders as string[]) ?? []),
        primary_folder: (ev.primary_folder as string | null) ?? null,
      });
      break;
    case "subagent.spawned": {
      // 子代理生命周期事件（契约 2：广播到父 session 与子 session 的所有
      // socket）。Store 侧 upsert 会话列表的子会话行；只有当本会话就是
      // 发起父时，transcript 里追加一张发起卡片（子 socket 收到同帧但不渲染）。
      ctx.g.notifySubagentSpawned(ev as unknown as SubagentSpawnEvent);
      // delegation 的 spawned 只为侧栏建行服务:transcript 不加发起卡——
      // 委托的实时进度/结果卡由 delegate_update + subagent.status 通道负责,
      // 叠加发起卡会双渲染。
      if (ev.parent_session_id === sid && ev.type !== "delegation") {
        ctx.storeRef.current[sid] = [
          ...(ctx.storeRef.current[sid] ?? []),
          {
            id: mid(),
            role: "system" as const,
            blocks: [
              {
                kind: "subagent_spawn",
                sessionId: ev.session_id as string,
                goal: (ev.goal as string) || "",
                constraints: (ev.constraints as string) || "",
                acceptance: (ev.acceptance as string) || "",
                title: (ev.title as string) || "",
                depth: (ev.depth as number) ?? 0,
                origin: (ev.origin as "user" | "agent") || "agent",
                spawnedAt: Math.floor(Date.now() / 1000),
              },
            ],
          },
        ];
      }
      break;
    }
    case "subagent.status": {
      // 状态机推进（running/waiting/done/failed/stopped）：store 的子会话行
      // 即时换挡（侧栏状态点、卡片状态共用）。终态且本会话是发起父时追加
      // 结果卡片——stopped 按契约不注入（用户/级联亲手杀的），跳过。
      ctx.g.notifySubagentStatus(ev as unknown as SubagentStatusEvent);
      const st = ev.status as string;
      if (
        ev.parent_session_id === sid &&
        (st === "done" || st === "failed")
      ) {
        const childId = ev.session_id as string;
        const store = ctx.storeRef.current[sid] ?? [];
        // 去重：同一子会话的结果卡片只出现一次（注入消息的 steer.absorbed
        // 路径可能已经先渲染过）。
        const already = store.some((m) =>
          m.blocks.some((b) => b.kind === "subagent_result" && b.sessionId === childId),
        );
        if (!already) {
          ctx.storeRef.current[sid] = [
            ...store,
            {
              id: mid(),
              role: "system" as const,
              blocks: [
                {
                  kind: "subagent_result",
                  sessionId: childId,
                  summary: (ev.result_summary as string) || "",
                  error: (ev.error as string) || undefined,
                },
              ],
            },
          ];
        }
      }
      break;
    }
    case "subagent.plan": {
      // 拆分方案（P2 共享契约 1）：/subagent 拆分 的 LLM decompose 结果，
      // 广播到当前 session 的 socket。折成系统行里的 subagent_plan 卡片；
      // 未确认前再次到来（契约 3 的「覆盖旧 plan」）→ 就地替换还在 pending
      // 的旧卡片，已决定过的卡片保留作为记录。
      const plan = ev as unknown as SubagentPlanEvent;
      const store = ctx.storeRef.current[sid] ?? [];
      const newBlock: Block = {
        kind: "subagent_plan",
        planId: plan.plan_id,
        task: plan.task || "",
        subtasks: Array.isArray(plan.subtasks) ? plan.subtasks : [],
        status: "pending",
      };
      const pendingIdx = store.findIndex((m) =>
        m.blocks.some((b) => b.kind === "subagent_plan" && b.status === "pending"),
      );
      if (pendingIdx >= 0) {
        ctx.storeRef.current[sid] = store.map((m, i) =>
          i === pendingIdx ? { ...m, blocks: [newBlock] } : m,
        );
      } else {
        ctx.storeRef.current[sid] = [
          ...store,
          { id: mid(), role: "system" as const, blocks: [newBlock] },
        ];
      }
      ctx.syncDisplay(sid);
      break;
    }
    case "subagent.plan.cancelled": {
      // 拆分方案被取消（P3 共享契约 5）：另一个窗口发了 subagent.plan.cancel，
      // runtime 广播回执——把本窗口里该 plan 还在 pending 的卡片翻成已取消态。
      // 本地发起的取消已在 decideSubagentPlan 里乐观定格，这里幂等（只动 pending）。
      const cancelledPlanId = ev.plan_id as string | undefined;
      if (!cancelledPlanId) break;
      ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) => ({
        ...m,
        blocks: m.blocks.map((b) =>
          b.kind === "subagent_plan" && b.planId === cancelledPlanId && b.status === "pending"
            ? { ...b, status: "cancelled" as const }
            : b,
        ),
      }));
      break;
    }
    case "context.updated": {
      // WorldState change announcement (world-state-plan §7). Chip display
      // level table: environment-only changes (date rollover) stay SILENT in
      // the UI; everything else gets a centered context row.
      const changes = ((ev.changes as ContextChange[]) || []).filter(Boolean);
      const visible = changes.filter((c) => c.section !== "environment");
      if (!visible.length) break; // environment-only (date rollover) = silent
      // 事件契约（i18n-design.md §3）：单条 change 可带 i18n_key+params，
      // 透传给 ContextBlocks 渲染时翻译；旧 runtime 只有成品 summary，直显。
      const rows = visible.map((c): Block => {
        const extra = c as { i18n_key?: unknown; params?: unknown };
        return {
          kind: "context",
          text: c.summary,
          ...(typeof extra.i18n_key === "string"
            ? {
                i18n_key: extra.i18n_key,
                ...(extra.params != null ? { params: extra.params as Record<string, string | number> } : {}),
              }
            : {}),
        };
      });
      ctx.storeRef.current[sid] = [
        ...(ctx.storeRef.current[sid] ?? []),
        { id: mid(), role: "system" as const, blocks: rows },
      ];
      ctx.syncDisplay(sid);
      break;
    }
    case "context.microcompacted": {
      // Stale tool outputs cleared to placeholders (E2.5) — always visible.
      const n = Number(ev.cleared_tool_outputs ?? 0);
      ctx.storeRef.current[sid] = [
        ...(ctx.storeRef.current[sid] ?? []),
        {
          id: mid(),
          role: "system" as const,
          blocks: [
            {
              kind: "context",
              // 契约：key 命中渲染时翻译（chat.context.microcompacted）；
              // text 是旧 runtime / key 未命中时的英文兜底（与历史版本一致）。
              text: `Cleared ${n} older tool outputs to save context; call the tools again to re-fetch when needed.`,
              i18n_key: "chat.context.microcompacted",
              params: { n },
            },
          ],
        },
      ];
      ctx.syncDisplay(sid);
      break;
    }
    case "context.compacting": {
      // E3 总结 LLM 调用前发出的瞬态状态:live 气泡状态行
      // 从「正在思考…」切到「压缩中…」。不落 store,不进历史。
      // 2026-11-12 payload 带 {stage,pct}（summarizing=8 / writing=92），
      // 中间段由渲染层时间缓动推进——Claude Code 同款进度条体验。
      const pl = ev as { stage?: string; pct?: number; predicted_s?: number | null };
      if (pl.stage === "done") {
        // 终态（2026-11-12 卡死修复）：compact 是后台任务，结束后若无
        // 后续事件，compacting 镜像永不清 → Composer 永久 readOnly。
        ctx.compactingBySessionRef.current[sid] = false;
        ctx.compactStageRef.current[sid] = null;
        ctx.syncDisplay(sid);
        break;
      }
      ctx.compactingBySessionRef.current[sid] = true;
      ctx.compactStageRef.current[sid] = {
        stage: typeof pl.stage === "string" ? pl.stage : "summarizing",
        predicted_s:
          typeof pl.predicted_s === "number" && pl.predicted_s > 0
            ? pl.predicted_s
            : undefined,
        pct: typeof pl.pct === "number" ? pl.pct : 8,
      };
      ctx.syncDisplay(sid);
      break;
    }
    case "context.compacted": {
      // History compaction announcement (E3) — always visible.
      const n = Number(ev.compacted_messages ?? 0);
      ctx.storeRef.current[sid] = [
        ...(ctx.storeRef.current[sid] ?? []),
        {
          id: mid(),
          role: "system" as const,
          blocks: [
            {
              kind: "context",
              text: `Conversation compacted: ${n} older messages were replaced with a summary; recent messages are kept verbatim.`,
              i18n_key: "chat.context.compacted",
              params: { n },
            },
          ],
        },
      ];
      ctx.syncDisplay(sid);
      break;
    }
    case "history.cleared": {
      // /clear 已把 checkpoint 的 messages 清空：本地 transcript 整体丢弃，
      // 流式状态一并复位（ensureLive 会在空 store 上为紧随其后的 /clear
      // 收据 notice 新开一个气泡，作为新对话的第一条）。
      ctx.storeRef.current[sid] = [];
      ctx.liveBySessionRef.current[sid] = null;
      ctx.streamAgentRef.current[sid] = null;
      ctx.busyBySessionRef.current[sid] = false;
      ctx.orphanStreamRef.current[sid] = false;
      ctx.retryNoticeRef.current[sid] = null;
      ctx.syncDisplay(sid);
      break;
    }
    case "usage": {
      // Per-call usage (D2) → TopBar via callback. We forward the TURN
      // increment, not the event's `session` accumulator: that is the
      // runtime's in-memory per-session counter (this session only, wiped on
      // sidecar restart), while the TopBar seeds from GET /usage's
      // persistent whole-tree total — applying the accumulator wholesale made
      // the counter JUMP BACKWARD on every turn (2026-10-31 drift fix; the
      // consumer in AppShell adds the turn onto the fetched base).
      // usage 事件的 turn 增量与圆环更新只作用于**当前活跃会话**（P0，
      // 2026-11-07 混用修复）：TopBar 的 usageDelta/contextState 是全局
      // 单值，而后台会话（定时 task / 并行委托 / 仍在跑的长 turn）一样
      // 产 usage 帧——不过滤会把别的会话的占用覆盖到当前会话的圆环上
      // （「81% 在跳」「多个 session 混用」的根因）。turn-meta 累加仍按
      // 事件所属 sid 落到对应会话的气泡上，不受此守卫影响。
      const active = ctx.activeSidRef.current;
      const t = ev.turn as Partial<SessionUsage> | undefined;
      if (t && typeof t.input_tokens === "number" && sid === active) {
        ctx.onUsageChange?.({
          input_tokens: t.input_tokens ?? 0,
          output_tokens: t.output_tokens ?? 0,
          cache_read_tokens: t.cache_read_tokens ?? 0,
          cache_creation_tokens: t.cache_creation_tokens ?? 0,
          calls: 1,
        });
        // turn-meta live（turn 右下角）：本 turn 多次调用逐次累加。
        const _lid = ctx.liveBySessionRef.current[sid];
        const _cur = (
          _lid &&
          (ctx.storeRef.current[sid] ?? [])
            .find((m) => m.id === _lid)
            ?.blocks.find((b) => b.kind === "turn_meta")
        ) as Extract<Block, { kind: "turn_meta" }> | undefined;
        upsertTurnMeta(ctx, sid, {
          inputTokens: (_cur?.inputTokens ?? 0) + (t.input_tokens ?? 0),
          outputTokens: (_cur?.outputTokens ?? 0) + (t.output_tokens ?? 0),
        });
      }
      // 上下文占用（TopBar 圆环）：当前状态，直接替换——仅当前活跃会话
      // 的帧（见上面的 active 守卫）。
      const c = ev.context as ContextState | undefined;
      if (c && typeof c.percent === "number" && sid === active) {
        ctx.onContextChange?.(c);
      }
      break;
    }
    case "turn.state": {
      // Answer to the post-reconnect probe: is a turn still running (or
      // parked at an interrupt) for this session?
      if (ctx.reconcileTimerRef.current[sid]) {
        clearTimeout(ctx.reconcileTimerRef.current[sid]!);
        ctx.reconcileTimerRef.current[sid] = null;
      }
      // P5（2026-11-16）：探针报告摘要飞行中 → 恢复「压缩中」镜像。
      // 断线期间 context.compacting 帧丢失，重连后没有这条恢复分支的话
      // 一个还在压缩的 turn 会显示为普通 running（进度条消失）。
      if (ev.compacting === true && !ctx.compactingBySessionRef.current[sid]) {
        ctx.compactingBySessionRef.current[sid] = true;
        ctx.compactStageRef.current[sid] = { stage: "summarizing", pct: 8 };
        ctx.syncDisplay(sid);
      }
      if (ctx.serverRunningRef.current[sid] !== !!ev.running) {
        ctx.serverRunningRef.current[sid] = !!ev.running;
        // Mirror into React state — this is what makes a history-rebuilt
        // pending question card interactive after a reload (questionLive).
        ctx.syncDisplay(sid);
      }
      if (ev.running) break; // the broadcast stream resumes on this socket
      // ...unless our newest send is NEWER than the probe this answers. The
      // answer can only describe the session as it was when the server
      // handled the probe; reconciling a send that postdates it reads a
      // /history with no checkpoint yet and stamps the bubble 「未送达」 on a
      // turn that is running fine (first message of a new session: the socket
      // is opened by the send, probe at t0, invoke at t0+13ms). Wait for the
      // stream instead — it always ends in message.end or error, both of
      // which reconcile a genuinely dead turn.
      if ((ctx.sendSeqRef.current[sid] ?? 0) > (ctx.probeSeqRef.current[sid] ?? 0)) break;
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
      break;
    }
    case "message.end": {
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
      break;
    }
    case "turn.stopped": {
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
        break;
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
      break;
    }
    case "error": {
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
        break;
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
      break;
    }
    // ─── AgentTeam 回帖（agent-team-design.md §5.2 步骤 6）──────────────────
    // 成员在自己的 session 跑完，产出以回帖写回主会话 checkpoint，并广播本事件。
    // 这里把它作为一条**完成的** assistant 消息追加到主会话视图——否则用户要手动
    // reload 才看得到（checkpoint 写入不触发主会话的实时流）。
    //
    // 去重：用服务端的 message_id 作 ChatMsg.id。它与 history reload 时
    // mapHistory 读到的 AIMessage.id 相同，所以「实时追加」与「之后 reload」
    // 指向同一条，不会双渲染。
    case "team.delivered": {
      // AgentTeam 投递回执（backlog P0-1 + P1-1）。此前全仓库零 handler：
      // runtime 发完就 continue，前端 busy 恒真 → composer 永久锁死。
      // runtime 侧已补 message.end，这里做防御性收口 + 回执可视化。
      //
      // 三态语义严格区分（§5.3.3 投递原子性）：
      //   delivered → 清 sending
      //   stalled   → **也已发送**，只是 turn 未在窗口内起跑。绝不标 failed：
      //               标 failed 会给重试按钮，一点就是重复投递
      //   failed    → 确定没发（blocked 原子拒绝一个字都没发 / 成员不存在），
      //               标 failed 允许重试才是对的
      const receipt = ((ev.receipt as string) || "delivered") as
        "delivered" | "stalled" | "failed";
      const meta = {
        receipt,
        channel: (ev.channel as string) || "",
        reason: (ev.reason as string) || "",
        error: (ev.error as string) || "",
        displayName: (ev.display_name as string) || (ev.agent_id as string) || "",
        memberSessionId: (ev.session_id as string) || "",
        teamId: (ev.team_id as string) || "",
        instanceId: (ev.instance_id as string) || "",
      };
      // 空占位收口用的 liveId：必须在下面置 null **之前**捕获。
      const endedLiveId = ctx.liveBySessionRef.current[sid] ?? null;
      // turnId 兜底判据（不能只靠 liveId）：广场投递是异步的，上一条的
      // message.end 可能**晚于**下一条的发送到达——它会把 liveBySessionRef
      // 清成 null，而那时引用已经指向新一条的占位了。此时只按 liveId 过滤
      // 会拿到 null → 新占位漏删 → 又一个「(空回复)」（2026-11-15 e2e 实测
      // 偶发：第一条 @ 投递 + 第二条紧跟，第二条的空气泡时有时无）。
      // 客户端 turnId 经 invoke.turn_id 原样传给服务端并回显（ws.py:350），
      // 所以 ev.turn_id 与气泡 turnId 是同一把钥匙，跨事件顺序稳定。
      const endTurn = (ev.turn_id as string) || null;
      const list = ctx.storeRef.current[sid] ?? [];
      // 挂到最后一条用户消息上（回执属于「我发的那条」，不是新气泡）。
      let lastUser = -1;
      for (let i = list.length - 1; i >= 0; i--) {
        if (list[i].role === "user") { lastUser = i; break; }
      }
      if (lastUser >= 0) {
        const next = [...list];
        const prev = next[lastUser];
        // 多目标投递：同一用户消息可能收到 N 条回执，合并成数组语义——
        // 这里保留最严重的一条（failed > stalled > delivered），避免后到的
        // delivered 覆盖掉先前的 blocked 拒绝。
        const rank = { failed: 2, stalled: 1, delivered: 0 } as const;
        const keep = !prev.teamDelivery
          || rank[receipt] >= rank[prev.teamDelivery.receipt]
          ? meta : prev.teamDelivery;
        next[lastUser] = {
          ...prev,
          teamDelivery: keep,
          ...(receipt === "failed"
            ? { status: "failed" as const, failReason: meta.error || meta.reason || "delivery failed" }
            : { status: undefined, failReason: undefined }),
        };
        ctx.storeRef.current[sid] = next;
      } else if (receipt === "failed") {
        markDelivered(ctx, sid);
      } else {
        markDelivered(ctx, sid);
      }
      // 防御性收口：即使 message.end 因故未到，也不让 composer 卡死。
      ctx.busyBySessionRef.current[sid] = false;
      ctx.serverRunningRef.current[sid] = false;
      ctx.liveBySessionRef.current[sid] = null;
      // 丢弃空占位气泡（与 turn.stopped / error 同一范式，那两处的注释写的
      // 就是同一个「(空回复)」症状——唯独投递分支漏了这道收口）。
      //
      // 广场投递**不起主会话 turn**（ws.py 的 AgentTeam 路由分支只发
      // team.delivered + message.end），所以 ChatStream.attemptSend 乐观插入的
      // `blocks: []` 占位永远等不到流事件。此前只清 liveBySessionRef，空壳仍
      // 留在 store 里 → message.end 的 stampTurnTs 按 turnId 兜底命中它并盖上
      // ts → 渲染成「(空回复)」+「刚刚」，永久留在转录里（2026-11-15 用户截图：
      // 每发一条必现一个）。成员真正的产出稍后走 team.reply 回帖卡，与它无关。
      //
      // 只删 blocks 为空的那一个：空团队分支的 notice 提示、以及任何已流出的
      // 正文都在这个气泡里，删过头会把合法内容一起抹掉。
      if (endedLiveId || endTurn) {
        ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).filter(
          (m) =>
            !(
              m.role === "assistant" &&
              m.blocks.length === 0 &&
              (m.id === endedLiveId || (!!endTurn && m.turnId === endTurn))
            ),
        );
      }
      ctx.syncDisplay(sid);
      break;
    }
    case "team.reply": {
      const msgId = (ev.message_id as string) || mid();
      const store = ctx.storeRef.current[sid] ?? [];
      if (store.some((m) => m.id === msgId)) break; // 已存在（reload 先到）
      const text = (ev.text as string) || "";
      ctx.storeRef.current[sid] = [
        ...store,
        {
          id: msgId,
          role: "assistant" as const,
          agentId: (ev.agent_id as string) || null,
          blocks: [{ kind: "text", text } as Block],
          teamReply: {
            team_id: (ev.team_id as string) || "",
            instance_id: (ev.instance_id as string) || "",
            member_session_id: (ev.member_session_id as string) || "",
            display_name: (ev.display_name as string) || "",
            truncated: Boolean(ev.truncated),
          },
        },
      ];
      ctx.syncDisplay(sid);
      // P1-18：成员回帖 → 通知中心 + OS 通知（此前 team.reply 零通知，
      // 用户离开广场就不知道成员干完了）。走与 reply 同一套门：
      // 总开关 ∧ kinds.team ∧ 不在盯 ∧ 不在 DnD。
      {
        const watching = isWatching(ctx, sid);
        const name = (ev.display_name as string) || (ev.agent_id as string) || "";
        const body = ctx.i18nRef.current.tr("team.notifyReply", { name });
        pushNotice("info", body, { dedupeKey: `team-reply-${ev.instance_id}` });
        if (shouldNotify("team", watching)) {
          const title = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
          // 新契约同上：唯一出口内部裁隐私/推提示音，这里只给原文 + notifyKind。
          void notifyNative({
            kind: "session",
            notifyKind: "team",
            id: sid,
            title: `👥 ${title}`,
            body,
          });
        }
      }
      break;
    }
    // ─── Claude Code Mods(claude-code-mods-design.md §7)──────────────────────
    // 归属守卫:frame_session 检查已在 socket onmessage 里统一做过,这里的
    // sid 即归属会话,无需再防串线。
    case "mod.bands": {
      // 整树替换(§7.1 per-mod:每 mod 一段,generation 按 mod 递增,过期
      // press 由 broker 侧按 mod 校验忽略)。旧帧形状(单树 {generation,
      // tree, mod})兜底成一段,树空则清空。
      const groups: ModBandGroup[] = [];
      if (Array.isArray(ev.mods)) {
        for (const entry of ev.mods) {
          if (!entry || typeof entry !== "object") continue;
          const tree = Array.isArray(entry.tree) ? (entry.tree as ModBandGroup["tree"]) : [];
          if (tree.length === 0) continue; // 空树 mod 不占位
          groups.push({
            mod: typeof entry.mod === "string" ? entry.mod : undefined,
            generation: Number(entry.generation) || 0,
            tree,
          });
        }
      } else if (Array.isArray(ev.tree)) {
        const tree = ev.tree as ModBandGroup["tree"];
        if (tree.length > 0) {
          groups.push({
            mod: typeof ev.mod === "string" ? ev.mod : undefined,
            generation: Number(ev.generation) || 0,
            tree,
          });
        }
      }
      ctx.setModBands((prev) => ({ ...prev, [sid]: groups }));
      break;
    }
    case "mod.toast": {
      // 收编进全局 notice 总线（notice-unification-design.md §4）：ModToastHost
      // 已退役，右下角统一容器渲染。事件契约不变：带 i18n_key 时落地翻译
      // (evText)，否则成品文本原样直显（broker toast / mod 自己的文案）。
      // 帧里没有稳定的 mod id 可做 dedupeKey，不传——保留多条并存语义
      //（NOTICE_MAX=3 堆叠），避免不同文案被 TTL 去重合并。
      const level: NoticeLevel = ev.level === "warn" || ev.level === "error" ? ev.level : "info";
      pushNotice(level, ctx.i18nRef.current.evText(ev, String(ev.text ?? "")), { source: "mod" });
      break;
    }
    case "mod.state.changed": {
      // mod 装载/启停/配置变化:设置页若开着就重拉;没有对应的 store 动作,
      // 页面不在时该事件自然无处消费,静默。
      try {
        window.dispatchEvent(new CustomEvent("ginno:mods-state-changed"));
      } catch {
        /* 非浏览器环境(SSG) */
      }
      break;
    }
    case "mod.prompt.fill": {
      // $.prompt.fill(mods ops):接管 composer 草稿(fill 语义=覆盖,与委派卡
      // 的追加预填不同,走专用事件,ChatStream 侧替换落字)。
      const text = String(ev.text ?? "");
      if (!text) break;
      try {
        window.dispatchEvent(
          new CustomEvent("ginno:mod-fill-input", { detail: { sessionId: sid, text } }),
        );
      } catch {
        /* 非浏览器环境(SSG) */
      }
      break;
    }
    case "mod.prompt.suggest": {
      // $.prompt.suggest(mods ops):幽灵建议的 v1 落地——composer 为空才
      // 落字(建议语义),非空丢弃不打扰。ChatStream 侧裁决空与否。
      const text = String(ev.text ?? "");
      if (!text) break;
      try {
        window.dispatchEvent(
          new CustomEvent("ginno:suggest-input", { detail: { sessionId: sid, text } }),
        );
      } catch {
        /* 非浏览器环境(SSG) */
      }
      break;
    }
    case "mod.ask": {
      // $.ui.ask(§7.3):问题浮层,按 sid 存最新一条。choices 可为字符串,
      // 也兼容 {label}/{value} 对象形状;自由输入恒可用。
      const rawChoices = Array.isArray(ev.choices) ? ev.choices : [];
      const ask: ModAskState = {
        id: String(ev.id ?? ""),
        mod: typeof ev.mod === "string" ? ev.mod : undefined,
        message: String(ev.message ?? ""),
        choices: rawChoices
          .map((c) =>
            typeof c === "string"
              ? c
              : c && typeof c === "object"
                ? String((c as { label?: unknown; value?: unknown }).label ??
                  (c as { value?: unknown }).value ?? "")
                : "",
          )
          .filter((c) => c !== ""),
      };
      if (!ask.id) break; // 无法回发的问题直接丢弃
      ctx.setModAsks((prev) => ({ ...prev, [sid]: ask }));
      break;
    }
    // mod.pane.*(§7.4,P2):右栏 pane 区,经 paneBus 交给 ModPaneHost;
    // 本分支只做转发,归属(会话切换移除)由宿主按 sid 自行守卫。
    case "mod.pane.open":
    case "mod.pane.update":
    case "mod.pane.close": {
      const kind = ev.event === "mod.pane.open" ? "open" : ev.event === "mod.pane.update" ? "update" : "close";
      pushModPane(sid, kind, {
        id: String(ev.id ?? ""),
        title: typeof ev.title === "string" ? ev.title : String(ev.id ?? ""),
        mod: typeof ev.mod === "string" ? ev.mod : undefined,
        generation: Number(ev.generation) || 0,
        tree: Array.isArray(ev.tree) ? (ev.tree as ModPaneSnapshot["tree"]) : [],
      });
      break;
    }
  }
  ctx.syncDisplay(sid);
}
