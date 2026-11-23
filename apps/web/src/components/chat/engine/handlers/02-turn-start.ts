// 02-turn-start — turn 生命周期起点。等价迁移自 handle() switch（批次 6-2），
// case 体逐字一致（break 移除，块作用域保持）。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { omitRepeatedResults } from "../../blocks/subagentParse";
import { foldSubagentResultBlocks, type Block } from "@/components/chat/blocks";
import type { ChatMsg } from "../../streamCore";
import { mid } from "../../streamCore";
import { ensureLive, markDelivered, upsertTurnMeta } from "../liveBubble";

export function onTurnStart(ctx: EngineCtx, sid: string, ev: ServerEvent) {
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
  if (srvTurn && Array.isArray(ev.task_events)) {
    const blocks: Block[] = ev.task_events.flatMap((task) =>
      task && typeof task.task === "string" ? [{
        kind: "task_event" as const,
        taskId: task.task,
        ev: String(task.kind || ""),
        title: String(task.title || ""),
        exit: task.exit ?? null,
        dur: String(task.dur || ""),
      }] : [],
    );
    const eventId = `${srvTurn}-task-events`;
    const store = ctx.storeRef.current[sid] ?? [];
    if (blocks.length && !store.some((msg) => msg.id === eventId)) {
      ctx.storeRef.current[sid] = store.flatMap((msg) =>
        msg.id === id ? [{ id: eventId, role: "system" as const, blocks }, msg] : [msg],
      );
    }
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
      const echoBlocks = omitRepeatedResults(foldSubagentResultBlocks([
        ...(echoText ? [{ kind: "text" as const, text: echoText }] : []),
        ...echoImgs.map((i) => ({
          kind: "image" as const,
          url: `data:${i.media_type || "image/png"};base64,${i.data}`,
        })),
      ]), store.flatMap((m) => m.blocks));
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
}
