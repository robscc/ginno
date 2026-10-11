// engine/actions — 权限/提案/问答/重试等 socket 动作与 mods UI 回发
// （自 useChatStreamEngine.ts 机械拆出，函数体逐字一致；原闭包引用改为经 ctx 取用）。

import {
  subagentPlanCancelFrame,
  subagentPlanConfirmFrame,
} from "@/lib/runtime";
import type { SubagentPlanSubtask } from "@/lib/types";
import type { EngineCtx } from "./ctx";
import { requeueSteersOnResume } from "./steer";

/** Click on the red ❗: resend the exact payload with a fresh turn id (a
 * retry is a genuinely new turn). Operates on the failed bubble IN PLACE —
 * the bubble flips back to "sending" and the response slots in right after
 * it; no duplicate message is appended. */
export function retryFailed(ctx: EngineCtx, msgId: string) {
  const sid = ctx.curSessionIdRef.current;
  if (!sid || ctx.busyBySessionRef.current[sid]) return; // one turn at a time
  const list = ctx.storeRef.current[sid] ?? [];
  const msg = list.find((m) => m.id === msgId);
  if (!msg || msg.role !== "user" || msg.status !== "failed" || !msg.sendPayload) return;
  ctx.attemptSend(sid, msg.sendPayload, msg.id);
}

/** Pull the failed payload back into the composer (text, images, files,
 * mentions, target agent) and drop the bubble — content is never lost. */
export function editResend(ctx: EngineCtx, msgId: string) {
  const sid = ctx.curSessionIdRef.current;
  if (!sid) return;
  const msg = (ctx.storeRef.current[sid] ?? []).find((m) => m.id === msgId);
  if (!msg || msg.status !== "failed" || !msg.sendPayload) return;
  const p = msg.sendPayload;
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).filter((m) => m.id !== msgId);
  ctx.setInput(p.text);
  ctx.setAttachments(p.images);
  ctx.setFileAttachments(p.files);
  ctx.setTarget(p.agentId);
  ctx.mentionsRef.current[sid] = p.mentions;
  ctx.syncDisplay(sid);
  requestAnimationFrame(() => {
    const el = ctx.textareaRef.current;
    if (el) {
      el.focus();
      el.setSelectionRange(p.text.length, p.text.length);
    }
    ctx.recomputeMenu(p.text);
  });
}

export function dismissFailed(ctx: EngineCtx, msgId: string) {
  const sid = ctx.curSessionIdRef.current;
  if (!sid) return;
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).filter(
    (m) => !(m.id === msgId && m.status === "failed"),
  );
  ctx.syncDisplay(sid);
}

/** Retry on a turn-error card: keep the error card in place as a record of
 * the failure, then append a brand-new user bubble + assistant placeholder
 * at the tail of the chat. The new turn gets a fresh turnId so the server
 * treats it as a new attempt (no checkpoint dedup collision). */
export function retryError(ctx: EngineCtx, msgId: string) {
  const sid = ctx.curSessionIdRef.current;
  if (!sid || ctx.busyBySessionRef.current[sid]) return; // one turn at a time
  const list = ctx.storeRef.current[sid] ?? [];
  const card = list.find((m) => m.id === msgId);
  if (!card?.error || !card.sendPayload) return;
  // Keep the error card — don't remove it. Just start a fresh turn at the bottom.
  ctx.attemptSend(sid, card.sendPayload);
}

/** Retry from checkpoint: resume the failed turn from its latest checkpoint
 * instead of re-executing from the start. Preserves tool calls and
 * intermediate results. */
export function retryFromCheckpoint(ctx: EngineCtx, msgId: string) {
  const sid = ctx.curSessionIdRef.current;
  if (!sid || ctx.busyBySessionRef.current[sid]) return;
  const list = ctx.storeRef.current[sid] ?? [];
  const card = list.find((m) => m.id === msgId);
  if (!card?.error) return;
  // Remove the error card
  ctx.storeRef.current[sid] = list.filter((m) => m.id !== msgId);
  ctx.syncDisplay(sid);
  // Mark as busy
  ctx.busyBySessionRef.current[sid] = true;
  const turnId = card.turnId || crypto.randomUUID();
  const sock = ctx.socketsRef.current[sid];
  if (!sock || sock.readyState !== WebSocket.OPEN) {
    ctx.busyBySessionRef.current[sid] = false;
    return;
  }
  try {
    sock.send(
      JSON.stringify({
        type: "retry_from_checkpoint",
        turn_id: turnId,
      }),
    );
  } catch {
    ctx.busyBySessionRef.current[sid] = false;
  }
}

export function respond(ctx: EngineCtx, decision: "allow" | "deny") {
  const sid = ctx.curSessionIdRef.current;
  if (!sid) return;
  // Anything still in the steer queue belongs to this turn: re-send it as
  // `steer` BEFORE the resume, so the resumed segment absorbs it (design
  // §3.4). Idempotent by steer_id, so a duplicate can't inject it twice.
  requeueSteersOnResume(ctx, sid);
  try {
    ctx.socketsRef.current[sid]?.send(JSON.stringify({ type: "permission_response", decision }));
  } catch {
    /* socket gone — reconnect re-emits the prompt if still pending */
  }
  ctx.permsRef.current[sid] = null;
  ctx.setPermission(null);
}

export function stopTurn(ctx: EngineCtx) {
  // Hard-stop the running turn (server abandons the in-flight step, keeps
  // what already streamed, heals state). Idempotent server-side; a stopped
  // turn ends with a turn.stopped broadcast that clears `running`.
  const sid = ctx.curSessionIdRef.current;
  if (!sid) return;
  try {
    ctx.socketsRef.current[sid]?.send(JSON.stringify({ type: "stop" }));
  } catch {
    /* socket gone — reconnect reconciles via turn_state */
  }
}

/** 拆分方案卡片的决定（P2 共享契约 2）：上行 confirm/cancel 帧到该 plan 所在
 *  会话的 socket，并把 store 里对应卡片的 status 定格为回执态。confirm 带
 *  用户编辑过的 subtasks（缺省 = 原样采纳），runtime 逐个 spawn 后经既有
 *  subagent.spawned 事件回流。socket 不必是当前展示的会话——按 planId 反查
 *  所在 store，用户在卡片存活期间切走会话再点仍能发对地方。 */
export function decideSubagentPlan(
  ctx: EngineCtx,
  planId: string,
  decision: "confirm" | "cancel",
  subtasks?: SubagentPlanSubtask[],
) {
  let sid: string | null = null;
  for (const [k, list] of Object.entries(ctx.storeRef.current)) {
    if (list.some((m) => m.blocks.some((b) => b.kind === "subagent_plan" && b.planId === planId))) {
      sid = k;
      break;
    }
  }
  if (!sid) return;
  let sent = false;
  try {
    const sock = ctx.socketsRef.current[sid];
    if (sock && sock.readyState === WebSocket.OPEN) {
      sock.send(
        JSON.stringify(
          decision === "confirm"
            ? subagentPlanConfirmFrame(planId, subtasks)
            : subagentPlanCancelFrame(planId),
        ),
      );
      sent = true;
    }
  } catch {
    /* socket flipped CLOSING/CLOSED between check and send */
  }
  // 帧没发出去就不定格：卡片留在 pending，重连后可再点一次——静默丢一次
  // confirm 等于整批子任务凭空消失，比卡片晚一拍定格严重得多。
  if (!sent) return;
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) => ({
    ...m,
    blocks: m.blocks.map((b) =>
      b.kind === "subagent_plan" && b.planId === planId && b.status === "pending"
        ? { ...b, status: decision === "confirm" ? ("confirmed" as const) : ("cancelled" as const) }
        : b,
    ),
  }));
  ctx.syncDisplay(sid);
}

export function respondPropose(ctx: EngineCtx, decision: "allow" | "deny") {
  const sid = ctx.curSessionIdRef.current;
  if (!sid) return;
  // Reuses the permission_response channel; the server resumes the proposal
  // interrupt with {decision}, and the propose_edit tool applies on allow.
  requeueSteersOnResume(ctx, sid);
  try {
    ctx.socketsRef.current[sid]?.send(JSON.stringify({ type: "permission_response", decision }));
  } catch {
    /* socket gone — reconnect re-emits version.propose if still pending */
  }
  const p = ctx.propose;
  ctx.proposeRef.current[sid] = null;
  ctx.setPropose(null);
  // P0 polish: the card unmounts on decide, so leave a short receipt line
  // ("已应用 · v3 → 新版本" / "已拒绝") where the card used to be.
  if (p) {
    ctx.setProposeResult({ decision, workflowId: p.workflow_id, fromVersion: p.from_version });
    window.setTimeout(() => ctx.setProposeResult(null), 4000);
  }
}

// Answer a parked ask_user card. The server ignores the message unless the
// session is actually parked on a user_question interrupt (stale/duplicate
// answers are dropped there, not here). The optimistic fold collapses EVERY
// pending copy of the card by id — a post-reload re-emit can briefly have
// the history card and a live one on screen; the authoritative receipt still
// arrives via the tool's tool.end.
export function answerQuestion(ctx: EngineCtx, id: string, answer: string, optionIndex: number | null, skip: boolean) {
  const sid = ctx.curSessionIdRef.current;
  if (!sid) return;
  // Same rule as the permission path: queued entries belong to this turn, so
  // re-send them as `steer` before the answer resumes it (design §3.4).
  requeueSteersOnResume(ctx, sid);
  try {
    ctx.socketsRef.current[sid]?.send(
      JSON.stringify({ type: "user_answer", answer, option_index: optionIndex, skip }),
    );
  } catch {
    /* socket gone — reconnect re-emits user.question if still parked */
  }
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) =>
    m.blocks.some((b) => b.kind === "question" && b.id === id && b.status === "pending")
      ? {
          ...m,
          blocks: m.blocks.map((b) =>
            b.kind === "question" && b.id === id && b.status === "pending"
              ? skip
                ? { ...b, status: "skipped" as const }
                : { ...b, status: "answered" as const, answer, optionIndex }
              : b,
          ),
        }
      : m,
  );
  ctx.syncDisplay(sid);
}

// Band 按钮 press 回发(§7.1):后端/broker 未就绪时静默——按钮是「禁用
// 样式但可点」,发送失败不该打扰用户;过期代际由 broker 侧忽略。
// value 仅 pane 的 Input/Select 提交携带(随帧多余字段,broker 按需取用)。
export function sendModUiPress(ctx: EngineCtx, sid: string, generation: number, actionId: string, value?: string) {
  const sock = ctx.socketsRef.current[sid];
  if (!sock || sock.readyState !== WebSocket.OPEN) return;
  try {
    sock.send(JSON.stringify(value === undefined ? { type: "mod.ui.press", generation, actionId } : { type: "mod.ui.press", generation, actionId, value }));
  } catch {
    /* socket closing */
  }
}

// $.ui.ask 回答回发(§7.3):乐观清卡——即使 broker 侧已超时/孤儿,回答
// 被丢弃也比让用户对着一张死卡片重试好;卡没了问题自然消失。
export function sendModUiAnswer(ctx: EngineCtx, sid: string, id: string, value: string) {
  ctx.setModAsks((prev) => (prev[sid] ? { ...prev, [sid]: null } : prev));
  const sock = ctx.socketsRef.current[sid];
  if (!sock || sock.readyState !== WebSocket.OPEN) return;
  try {
    sock.send(JSON.stringify({ type: "mod.ui.answer", id, value }));
  } catch {
    /* socket closing */
  }
}
