// 04-runs — workflow 运行卡与产物/代码/预览事件。等价迁移自 handle() switch
// （批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { getWorkflowRun } from "@/lib/runtime";
import { toolArgsPreview } from "../../streamCore";

export function onRunBind(ctx: EngineCtx, sid: string, ev: ServerEvent) {
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
}

export function onRunEvent(ctx: EngineCtx, sid: string, ev: ServerEvent) {
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
}

export function onRunStatus(ctx: EngineCtx, sid: string, ev: ServerEvent) {
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
}

export function onArtifactsChanged(ctx: EngineCtx, sid: string) {
  ctx.g.reloadArtifacts();
  ctx.g.reloadProjectArtifacts(); // @ 菜单的跨会话列表同源刷新
}

export function onCodeChanged(ctx: EngineCtx, sid: string, ev: ServerEvent) {
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
}

export function onPreviewEmit(ctx: EngineCtx, sid: string, ev: ServerEvent) {
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
}

export function onPreviewInvalidate(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // A tracked file changed (tool wrote it / mtime watcher) → the
  // FileViewer refetches if that file is the one being viewed.
  if (ev.file_id) ctx.g.notifyPreviewInvalidate(ev.file_id as string);
}
