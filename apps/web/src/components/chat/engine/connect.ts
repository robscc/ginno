// engine/connect — socket 就绪 promise、历史映射与重连对账（自
// useChatStreamEngine.ts 机械拆出，函数体逐字一致；原闭包引用改为经 ctx 取用）。

import { getSessionHistory } from "@/lib/runtime";
import {
  foldSubagentResultBlocks,
  type Block,
} from "@/components/chat/blocks";
import { mid, payloadFromBlocks, type ChatMsg } from "../streamCore";
import type { EngineCtx } from "./ctx";
import { dropAbsorbedSteers } from "./steer";

export function armSocketReady(ctx: EngineCtx, sid: string) {
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  promise.catch(() => {}); // closes without a waiter are normal
  ctx.socketReadyRef.current[sid] = { promise, resolve, reject };
}

/** Resolves true once the sid's socket is OPEN; false on timeout/close. */
export function waitForSocketOpen(ctx: EngineCtx, sid: string, timeoutMs = 8000): Promise<boolean> {
  const sock = ctx.socketsRef.current[sid];
  if (sock?.readyState === WebSocket.OPEN) return Promise.resolve(true);
  const entry = ctx.socketReadyRef.current[sid];
  if (!entry) return Promise.resolve(false);
  return Promise.race([
    entry.promise.then(
      () => true,
      () => false,
    ),
    new Promise<boolean>((res) => setTimeout(() => res(false), timeoutMs)),
  ]);
}

// Abandon an in-flight turn after a socket drop that the server cannot
// answer for (legacy fallback): the user bubble keeps its retry payload.
export function abandonLiveTurn(ctx: EngineCtx, sid: string) {
  ctx.liveBySessionRef.current[sid] = null;
  ctx.streamAgentRef.current[sid] = null;
  ctx.busyBySessionRef.current[sid] = false;
  ctx.serverRunningRef.current[sid] = false;
  ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) =>
    m.role === "user" && m.status === "sending"
      ? // failReason 存稳定 key（渲染时经 chat.sendFailed.* 翻译），避免把
        // 成品文案烤进状态——语言切换后历史气泡仍能跟上。
        { ...m, status: "failed" as const, failReason: "connLost" }
      : m,
  );
  ctx.syncDisplay(sid);
}

// Map a /history response into chat bubbles (shared by the initial load and
// the post-reconnect reconciliation).
export function mapHistory(
  ctx: EngineCtx,
  res: {
    messages?: Array<{
      id?: string;
      role: ChatMsg["role"];
      blocks: Block[];
      agentId?: string | null;
      turnId?: string;
      // AgentTeam 回帖标记（messages_ui 序列化，§5.2 要点 2）。
      teamReply?: import("@/lib/types").TeamReplyMeta | null;
    }>;
    last_error?: {
    message?: string; turn_id?: string;
    i18n_key?: string; params?: Record<string, string | number>;
  } | null;
  },
): ChatMsg[] {
  // 服务端只给 user 消息带 turnId；assistant 消息继承其前一条 user 消息的
  // turnId——中途接入的实时流要靠它认领「历史里本轮已存在的半截气泡」
  // （否则同一轮渲染成两块，2026-10-01 子会话渲染问题）。
  let carryTurn: string | undefined;
  const mapped: ChatMsg[] = (res.messages ?? []).map((m) => {
    const own = m.turnId ?? (m.role === "user" ? m.id : undefined);
    if (m.role === "user" && own) carryTurn = own;
    return {
      id: m.id ?? mid(),
      role: m.role,
      // 运行时注入的子代理结果（契约 3：<ginno_subagent_result> 包裹的
      // HumanMessage）在重放时折成结果卡片块，避免原始标签以文本气泡出现。
      blocks: foldSubagentResultBlocks(m.blocks),
      agentId: m.agentId,
      teamReply: m.teamReply ?? null,
      turnId: own ?? (m.role === "assistant" ? carryTurn : undefined),
      // Rebuild the retry payload for history user bubbles too — without
      // it, a retry that fails again would produce an error card with no
      // payload (no retry button), and the error handler's "last user with
      // payload" lookup would come up empty.
      sendPayload:
        m.role === "user"
          ? payloadFromBlocks(m.blocks, m.agentId ?? ctx.session?.agent_id ?? null)
          : undefined,
    };
  });
  // Re-surface a persisted turn failure as an error card (with retry)
  // so the last error survives reloads and route/session switches.
  const err = res.last_error;
  if (err?.message) {
    const lastUser = [...mapped].reverse().find((m) => m.role === "user");
    mapped.push({
      id: mid(),
      role: "assistant",
      // 与 live 错误卡同语义:带 key 落地翻译(快照),未命中回退英文 message。
      blocks: [
        { kind: "text", text: ctx.i18nRef.current.evText(err, err.message) },
      ],
      turnId: err.turn_id ?? lastUser?.turnId,
      error: true,
      sendPayload: lastUser
        ? payloadFromBlocks(lastUser.blocks, lastUser.agentId ?? ctx.session?.agent_id ?? null)
        : undefined,
      sourceMsgId: lastUser?.id,
    });
  }
  return mapped;
}

// The server says no turn is running for this session (post-reconnect
// turn_state probe): the stream will not resume. Reload persisted history —
// a turn that FINISHED while we were disconnected is fully restored from
// the checkpoint. A user bubble still "sending" that never reached the
// graph survives as a failed bubble with its retry payload.
export function reconcileTurnFromHistory(ctx: EngineCtx, sid: string) {
  // We believed a turn was in flight and the server says it's gone → it
  // finished while we were disconnected: no message.end ever reached us, so
  // light the sidebar unread dot here too (message.end's mark is the same
  // call — idempotent). Watching guard lives inside markSessionUnread.
  ctx.g.markSessionUnread(sid);
  getSessionHistory(sid).then((res) => {
    // Skill blocks (history-replayed slash turns) normalize back to the
    // "/name request" text the live bubble carries, or the two never match
    // and a phantom "undelivered" duplicate appears.
    const textOf = (m: ChatMsg) =>
      m.blocks
        .map((b) =>
          b.kind === "text"
            ? b.text
            : b.kind === "skill"
              ? (b.text ? `/${b.name} ${b.text}` : `/${b.name}`)
              : "",
        )
        .join("\n")
        .replace(/\s+/g, " ")
        .trim();
    const pending = (ctx.storeRef.current[sid] ?? []).filter(
      (m) => m.role === "user" && m.status === "sending",
    );
    const mapped = mapHistory(ctx, res ?? {});
    // A steer_id present in history was absorbed even if the ack was lost to
    // the socket drop that caused this reconcile — drop those entries instead
    // of re-sending them (design §3.3, exactly-once).
    dropAbsorbedSteers(ctx, sid, mapped);
    for (const p of pending) {
      const delivered = mapped.some(
        (m) => m.role === "user" && textOf(m) === textOf(p),
      );
      if (!delivered) {
        mapped.push({ ...p, status: "failed" as const, failReason: "connLost" });
      }
    }
    ctx.storeRef.current[sid] = mapped;
    ctx.liveBySessionRef.current[sid] = null;
    ctx.streamAgentRef.current[sid] = null;
    ctx.busyBySessionRef.current[sid] = false;
    ctx.syncDisplay(sid);
  });
}
