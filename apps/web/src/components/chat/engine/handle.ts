// engine/handle — 服务端事件 reducer（handle）及其 live 气泡辅助函数
// （markDelivered / adoptOrphanStream / ensureLive / mutateLive / stripFromLive，
// 自 useChatStreamEngine.ts 机械拆出，函数体逐字一致；原闭包引用改为经 ctx 取用）。

import { getWorkflowRun } from "@/lib/runtime";
import { notifyNative } from "@/lib/desktop";
import { notifyPrefs } from "@/lib/notifyPrefs";
import { pushNotice } from "@/lib/notice";
import { pushModToast, type ModToastLevel } from "@/components/chat/mod/ModToastHost";
import { pushModPane, type ModPaneSnapshot } from "@/components/chat/mod/paneBus";
import type { ModBandGroup } from "@/components/chat/mod/ModBand";
import type { ModAskState } from "@/components/chat/mod/ModAskCard";
import { hasPendingTool, parseSubagentResult, type Block } from "@/components/chat/blocks";
import type {
  ContextChange,
  Goal,
  Plan,
  SessionUsage,
  SubagentPlanEvent,
  SubagentSpawnEvent,
  SubagentStatusEvent,
} from "@/lib/types";
import { applyBlock, closePendingBlocks, mid, newTurnId, toolArgsPreview } from "../streamCore";
import type { EngineCtx } from "./ctx";
import { reconcileTurnFromHistory } from "./connect";
import { flushSteerQueue, recallSteers } from "./steer";

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
    { id, role: "assistant", blocks: [], agentId: ctx.streamAgentRef.current[sid], turnId: evTurn || newTurnId() },
  ];
  ctx.liveBySessionRef.current[sid] = id;
  return id;
}

export function mutateLive(ctx: EngineCtx, sid: string, ev: { event: string; [k: string]: unknown }) {
  const id = ensureLive(ctx, sid, ev.turn_id as string | undefined);
  // 气泡归属体检（2026-10-01 串线排查）：流事件带的 turn_id 必须与它落入的
  // 气泡 turnId 一致；不一致说明内容串进了别轮的气泡。release webview 没有
  // 可读控制台，所以同时上报服务端日志（client_diag）留证。
  const evTurn = ev.turn_id as string | undefined;
  const bubble = (ctx.storeRef.current[sid] ?? []).find((m) => m.id === id);
  // 孤儿流认领（2026-10-01 串线排查）：客户端在 turn 进行中途才连上 socket
  // （子会话常态——spawn 后隔几十秒才被打开），拿不到 turn.start，气泡带着
  // 本地占位 turnId。首个带 turn_id 的事件到来时认领服务端 id，否则该会话的
  // 每个事件都对不上气泡、（更要紧）steer 帧会带一个服务端不认的 turn_id。
  if (evTurn && bubble && bubble.turnId !== evTurn) {
    if (ctx.orphanStreamRef.current[sid] && !ctx.adoptedTurnRef.current[sid]) {
      ctx.adoptedTurnRef.current[sid] = true;
      ctx.storeRef.current[sid] = (ctx.storeRef.current[sid] ?? []).map((m) =>
        m.id === id ? { ...m, turnId: evTurn } : m,
      );
    }
  }
  if (evTurn && bubble?.turnId && evTurn !== bubble.turnId && !ctx.orphanStreamRef.current[sid]) {
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
  if (ev.event !== "context.compacting" && ctx.compactingBySessionRef.current[sid]) {
    ctx.compactingBySessionRef.current[sid] = false;
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
    // Transcript block, not a ref-based prompt like permission.request: the
    // card lives (and is merged by id) inside the live bubble's blocks.
    case "user.question":
    case "widget.emit":
    case "ref.emit":
    case "image.emit":
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
      const patch: Partial<{ provider: string; model: string }> = {};
      if (typeof ev.provider === "string") patch.provider = ev.provider;
      if (typeof ev.model === "string") patch.model = ev.model;
      if (patch.provider || patch.model) ctx.g.applySessionPatch(sid, patch);
      // 轻提示走**公共通知层**（notice-center-design.md）：旧实现借用
      // chat/mod 的右下角 toast 宿主，语义错位且 mods 未挂载时帧直接丢弃。
      // dedupeKey 按会话去重——多窗口/回放页收到同一事件只弹一条。
      {
        const { tr } = ctx.i18nRef.current;
        const key = patch.model ? "model.switchToast" : "model.switchToastNoModel";
        pushNotice("success", tr(key, patch.model ? { model: patch.model } : undefined), {
          source: "model",
          dedupeKey: `model:${sid}`,
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
      // SheetViewer refetches if that file is the one being viewed.
      if (ev.file_id) ctx.g.notifyPreviewInvalidate(ev.file_id as string);
      break;
    case "steer.accepted":
      // The server stashed the entry (design §3.1). It stays in the queue bar
      // until it is absorbed; this only flips it out of "sending" (the shared
      // hook owns that queue mutation).
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
      // Capture the entry BEFORE delegating — the hook drops it from the queue.
      const entry = ctx.steerQ.itemsFor(sid).find((i) => i.steerId === absorbedId);
      if (!entry) break; // already dropped — a history reconcile got there first
      // Same timing/order as before: remove from the queue first, then render.
      ctx.steerQ.handleEvent(ev, sid);
      // runtime 注入的子代理结果走同一条 steer 通道（契约 3）：折成结果卡片，
      // 而不是把 <ginno_subagent_result> 原始标签渲染成一条注入带。
      const injectedResult = parseSubagentResult(entry.text);
      if (injectedResult) {
        const store = ctx.storeRef.current[sid] ?? [];
        const already = store.some((m) =>
          m.blocks.some(
            (b) =>
              b.kind === "subagent_result" && b.sessionId === injectedResult.sessionId,
          ),
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
                  sessionId: injectedResult.sessionId,
                  goal: injectedResult.goal,
                  summary: injectedResult.summary,
                },
              ],
            },
          ];
        }
        ctx.syncDisplay(sid);
        break;
      }
      const band: Block = {
        kind: "steer",
        text: entry.text,
        steerId: entry.steerId,
        injectedAt: Number(ev.injected_at) || Math.floor(Date.now() / 1000),
        // Attachments ride the band so the injection point shows what the
        // user actually sent. Images arrive as display URLs (data URL), files
        // as name/path chips — mirroring the replay shape the server emits.
        ...(entry.images.length
          ? { images: entry.images.map((a) => ({ name: a.name, url: a.preview })) }
          : {}),
        ...(entry.files.length
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
          { id: mid(), role: "user" as const, blocks: [band], turnId: entry.turnId },
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
    case "notice":
      // Built-in command reply (e.g. /help): no graph turn ran, so the server
      // pushes the rendered text directly into the live bubble as one delta.
      // 事件契约：带 i18n_key 时落地即翻译，否则原样直显 message。
      markDelivered(ctx, sid);
      let _txt = ctx.i18nRef.current.evText(ev, (ev.message as string) || "");
      // auto-retry 是瞬态系统提示——包成 code span + ⚠️，气泡里呈现为
      // 琥珀色等宽小条而不是混入正文的普通文本（用户反馈样式不显眼）。
      if ((ev.i18n_key as string) === "stream.auto_retry") {
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
      break;
    case "plan.updated":
      // 任务清单实时刷（plan-orchestration §2）：watcher 每次落盘推整张
      // 清单快照；store 按 plan_id upsert，PlanBoard 重渲染。
      if (ev.plan) ctx.g.notifyPlanUpdated(ev.plan as Plan);
      break;
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
      // E3 总结 LLM 调用前发出的瞬态状态(无 payload):live 气泡状态行
      // 从「正在思考…」切到「压缩中…」。不落 store,不进历史。
      ctx.compactingBySessionRef.current[sid] = true;
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
      // Session-cumulative model usage (D2) → TopBar counter via callback.
      const s = ev.session as SessionUsage | undefined;
      if (s && typeof s.input_tokens === "number") ctx.onUsageChange?.(s);
      break;
    }
    case "turn.state": {
      // Answer to the post-reconnect probe: is a turn still running (or
      // parked at an interrupt) for this session?
      if (ctx.reconcileTimerRef.current[sid]) {
        clearTimeout(ctx.reconcileTimerRef.current[sid]!);
        ctx.reconcileTimerRef.current[sid] = null;
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
        reconcileTurnFromHistory(ctx, sid);
      }
      break;
    }
    case "message.end": {
      markDelivered(ctx, sid);
      const wasOrphan = !!ctx.orphanStreamRef.current[sid];
      ctx.orphanStreamRef.current[sid] = false;
      ctx.adoptedTurnRef.current[sid] = false;
      ctx.liveBySessionRef.current[sid] = null;
      ctx.streamAgentRef.current[sid] = null;
      ctx.busyBySessionRef.current[sid] = false;
      ctx.serverRunningRef.current[sid] = false;
      // Orphaned continuation (remount mid-turn): the visible store holds a
      // history-rendered partial bubble PLUS a second live section. The
      // persisted history renders the whole turn as ONE merged bubble (with
      // the right agent name) — rebuild from it to heal the split.
      if (wasOrphan) reconcileTurnFromHistory(ctx, sid);
      // Turn done → desktop notification unless the user is watching this
      // exact session right now (visible ∧ workspace route ∧ active session).
      // Socket callbacks capture stale closures (sockets outlive session
      // switches) — read refs / live values only. The session title may be
      // stale too (rename after connect); cosmetic, accepted.
      {
        // Settings → Notifications (settings.json; sync cache — see
        // lib/notifyPrefs.ts for why this isn't React state).
        const np = notifyPrefs();
        const watching =
          document.visibilityState === "visible" &&
          window.location.pathname === "/" &&
          ctx.activeSidRef.current === sid;
        if (np.enabled && !watching) {
          const title = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
          const raw = typeof ev.text === "string" ? ev.text.trim() : "";
          const body = raw || ctx.i18nRef.current.tr("notify.replyCompleted");
          void notifyNative({
            kind: "session",
            id: sid,
            title,
            body,
            sound: np.sound ? np.soundName : undefined,
          }).then((sent) => {
            if (sent) return;
            // Plain-browser dev fallback — WKWebView has no Notification API,
            // so inside the packaged app this branch is a silent no-op.
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
              const n = new Notification(title, { body });
              n.onclick = () => {
                window.focus();
                ctx.g.setActiveSession(sid); // stable setter — stale closure safe
                window.dispatchEvent(
                  new CustomEvent("ginno:focus-latest", { detail: sid }),
                );
                n.close();
              };
            } catch {
              /* blocked/unsupported */
            }
          });
        }
      }
      // The turn ended with steer entries still unacknowledged: the oldest
      // becomes the next turn (design §3.3). Skipped on the orphan path above
      // — its history reconcile owns that decision, and flushing here could
      // re-send an entry the reconcile is about to see as absorbed.
      if (!wasOrphan) flushSteerQueue(ctx, sid);
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
        // replay reads as skipped) so `running` unsticks.
        .map((msg) =>
          hasPendingTool(msg.blocks)
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
      const liveMsgId = ctx.liveBySessionRef.current[sid];
      ctx.liveBySessionRef.current[sid] = null;
      ctx.streamAgentRef.current[sid] = null;
      ctx.busyBySessionRef.current[sid] = false;
      ctx.serverRunningRef.current[sid] = false;
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
            if (!hasPendingTool(marked.blocks)) return marked;
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
      const level: ModToastLevel = ev.level === "warn" || ev.level === "error" ? ev.level : "info";
      // 事件契约:带 i18n_key 时落地翻译(evText),否则成品文本原样直显
      // (broker toast / mod 自己的文案是成品字符串)。
      pushModToast(level, ctx.i18nRef.current.evText(ev, String(ev.text ?? "")));
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
