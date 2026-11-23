// 06-steer — steering 队列协议与委托实时进度。等价迁移自 handle() switch
// （批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { omitRepeatedResults } from "../../blocks/subagentParse";
import { foldSubagentResultBlocks, type Block } from "@/components/chat/blocks";
import { mid } from "../../streamCore";
import { settleRecalled } from "../steer";

export function onSteerRecalled(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // ↑ 取回对账（steering 取回协议 2026-11-05）：服务端真实删除集
  // 回传——命中的条目此刻才弹回 composer；未命中的已被吸收，照常注入。
  const ids = Array.isArray(ev.steer_ids)
    ? ev.steer_ids.map(String)
    : [];
  settleRecalled(ctx, sid, ids);
}

export function onSteerAccepted(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // The server stashed the entry (design §3.1). It stays in the queue bar
  // until it is absorbed; this only flips it out of "sending" (the shared
  // hook owns that queue mutation).
  ctx.steerQ.handleEvent(ev, sid);
}

export function onSteerAnswered(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 服务端把这条 steer 当作 parked 问题的答案直接恢复了 turn:不入队、
  // 不会有注入带,条目收走即可(卡片回执由 tool.end 折叠)。
  ctx.steerQ.handleEvent(ev, sid);
}

export function onSteerAbsorbed(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // The steered message was DRAINED into state (the server acks at drain
  // time, not at superstep commit — see docs/steering-design.md): move it
  // out of the queue bar into the transcript as a band at the injection
  // point — appended to the live assistant bubble, because one turn is one
  // bubble (design §4.2/§4.3).
  const absorbedId = ev.steer_id as string | undefined;
  if (!absorbedId) return;
  // 系统递归预警（engine 步数护栏，steer_id 前缀区分——_is_user_steer
  // 同款判据）是模型侧指令：不以用户注入带外显，live 与重放（fold 的
  // steer 分支）一致隐藏。
  if (absorbedId.startsWith("recursion-warn-")) return;
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
    return;
  }
  // Capture the entry BEFORE delegating — the hook drops it from the queue.
  const entry = ctx.steerQ.itemsFor(sid).find((i) => i.steerId === absorbedId);
  // 渠道注入的 steer（IM 侧 steer_channel_turn 入队，本地没有队列条目）：
  // 事件带原文（engine 的 absorbed payload 补了 text）——同样渲染注入带，
  // 桌面实时可见；无本地条目也无 text（旧服务端/机器注入）维持 no-op。
  const foreignText = !entry && typeof ev.text === "string" ? ev.text : "";
  if (!entry && !foreignText)
    return; // already dropped — a history reconcile got there first
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
    const fresh = omitRepeatedResults(foldedCards, store.flatMap((m) => m.blocks));
    if (fresh.length) {
      ctx.storeRef.current[sid] = [
        ...store,
        { id: mid(), role: "system" as const, blocks: fresh },
      ];
      ctx.syncDisplay(sid);
    }
    return;
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
}

export function onDelegateUpdate(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // P1 委托实时进度：运行中的系统行同 id 刷新；running=false 撤行
  //（结果卡由 subagent.status 通道补位，已有 dedup 与跳转）。
  const did = ev.delegation_id as string;
  if (!did) return;
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
}
