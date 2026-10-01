"use client";

// useSummarizeFlow — 「总结成流程」状态机（自 ChatStream.tsx 机械拆出，
// 行为零变化）：summarize 草稿/等待/报错状态、后台 synthesis 等待
// （WS 帧 + 2s 轮询兜底）、localStorage 草稿（24h TTL）的读写，
// 以及创建工作流 / 创建并运行 / 进入 dev 会话三条出口。
//
// 纯结构搬移：函数体逐字一致；g/session/ref 与 syncDisplay、
// runsBySessionRef 经 deps 注入。

import { useEffect, useMemo, useState } from "react";
import {
  createWorkflow,
  getSynthesisCase,
  summarizeSessionToDsl,
  triggerWorkflowRun,
} from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { SessionMeta, WorkflowRun } from "@/lib/types";
import { SUMMARIZE_DRAFT_KEY, readSummarizeDraft } from "./streamCore";

export interface SummarizeFlowDeps {
  g: ReturnType<typeof useGinno>;
  session: SessionMeta | null;
  sumPendingRef: { current: string | null };
  runsBySessionRef: { current: Record<string, WorkflowRun[]> };
  syncDisplay: (sid: string) => void;
}

export function useSummarizeFlow(deps: SummarizeFlowDeps) {
  const { g, session, sumPendingRef, runsBySessionRef, syncDisplay } = deps;

  // 「总结成流程」draft + busy state + inline failure reason (modal stays open)
  const [summarize, setSummarize] = useState<Record<string, unknown> | null>(null);
  const [sumBusy, setSumBusy]     = useState<"create" | "run" | "dev" | null>(null);
  const [sumErr, setSumErr]       = useState<string | null>(null);
  // Create-only success receipt: keeps the modal open with an explicit
  // "已创建 <name>" confirmation (so the user never wonders whether the
  // workflow was added). 创建并运行 closes and the run card animates in instead.
  const [sumCreated, setSumCreated] = useState<string | null>(null);
  // S1: summarize API call in flight + which session the draft came from (the
  // modal's retry button and header label need both).
  const [sumLoading, setSumLoading] = useState(false);
  const [sumSource, setSumSource] = useState<{ id: string; label: string } | null>(null);
  // quality-plan §3.1: synthesis case id for outcome backfill (adoption/first-run).
  const [sumSynthesisId, setSumSynthesisId] = useState<string | null>(null);
  // The synthesis case the UI is waiting on (background summarization). The WS
  // synthesis.event(finished) frame and the 2s polling fallback both resolve
  // through finishSynthesisWait; the ref mirror lets the WS handler and the
  // idempotency guard read it synchronously.
  const [sumPendingId, setSumPendingId] = useState<string | null>(null);
  const [sumMenuOpen, setSumMenuOpen] = useState(false);
  // S5: trace range — null = full session; 5/10/20 = last N messages.
  const [sumLastN, setSumLastN] = useState<number | null>(null);
  // S6: bump to re-read the localStorage draft (after restore/delete/save). The
  // draft is exposed as an OPT-IN row in the summarize dropdown — it must never
  // block a fresh summarize (a leftover draft from another session used to wedge
  // the button and prevent creating any workflow).
  const [draftTick, setDraftTick] = useState(0);
  // Re-read the localStorage draft when it may have changed (open/save/delete).
  const savedDraft = useMemo(
    () => (sumMenuOpen ? readSummarizeDraft() : null),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [sumMenuOpen, draftTick],
  );
  // ─── 闭环 (design A): 总结成流程 + 对话内运行块控制 ─────────────────────────
  // The actual LLM summarization path (also used by the modal's ↺ retry).
  // Async contract (G2): the endpoint validates synchronously, spawns the
  // synthesis in the background and returns {ok, synthesis_id, status:"started"}
  // immediately. The DSL then arrives via finishSynthesisWait — resolved either
  // by the WS synthesis.event(finished) frame or the 2s polling fallback,
  // whichever wins the idempotency guard. sumLoading stays true until the
  // terminal state so the button ("正在总结…") doubles as the double-click
  // guard and the modal never opens on a half-finished case.
  async function freshSummarize(sessionId?: string) {
    if (sumLoading) return; // a synthesis is already in flight
    setSumMenuOpen(false);
    const targetId = sessionId || session?.id;
    if (!targetId) return;
    const label =
      targetId === session?.id
        ? session?.title || "当前会话"
        : g.sessions.find((s) => s.id === targetId)?.title || "历史会话";
    setSumLoading(true);
    setSumErr(null);
    setSumCreated(null);
    try {
      const r = await summarizeSessionToDsl(targetId, undefined, sumLastN ?? undefined);
      if (r.ok && r.synthesis_id) {
        setSumSource({ id: targetId, label });
        setSumSynthesisId(r.synthesis_id);
        setSumPendingId(r.synthesis_id);
        sumPendingRef.current = r.synthesis_id;
        // NOTE: no setSummarize / setSumLoading(false) here — the wait state
        // machine below resolves the draft once the case finishes.
      } else {
        // Synchronous validation failure (400/404/500) — HTTPException bodies
        // carry {detail}; json() doesn't throw on HTTP errors.
        setSumErr(`总结失败：${r.error ?? r.detail ?? "unknown"}`);
        setSummarize({}); // keep the modal open so the reason is visible
        setSumLoading(false);
      }
    } catch {
      setSumErr("总结失败：无法连接运行时");
      setSummarize({});
      setSumLoading(false);
    }
  }

  // Idempotent resolver for a finished synthesis wait. Both the WS handler and
  // the polling fallback call this; the ref guard makes the second caller a
  // no-op. Fetches the case detail (the finished event deliberately omits the
  // DSL) and drives the same success/error states the old sync path did.
  async function finishSynthesisWait(id: string) {
    if (sumPendingRef.current !== id) return; // already resolved / abandoned
    sumPendingRef.current = null;
    setSumPendingId(null);
    try {
      const r = await getSynthesisCase(id);
      const out = r.case?.output;
      if (r.ok && out?.status === "ok" && out.dsl) {
        setSummarize(out.dsl as Record<string, unknown>);
      } else {
        setSumErr(
          `总结失败：${out?.fail_stage || "unknown"}（案例 ${id}，~/.ginno/synthesis/${id}）`,
        );
        setSummarize({});
      }
    } catch {
      setSumErr("总结失败：无法连接运行时");
      setSummarize({});
    } finally {
      setSumLoading(false);
      void g.reloadSynthesisCases();
    }
  }

  // Polling fallback for the pending synthesis: the WS frame is the fast path,
  // but a session switch (per-session sockets) or a dropped frame must not
  // strand the wait. 2s interval, 180s hard timeout with the case id in the
  // message so the on-disk trace is locatable.
  useEffect(() => {
    if (!sumPendingId) return;
    const started = Date.now();
    const id = sumPendingId;
    const t = setInterval(() => {
      if (sumPendingRef.current !== id) {
        clearInterval(t); // resolved via WS or abandoned
        return;
      }
      if (Date.now() - started > 180_000) {
        clearInterval(t);
        sumPendingRef.current = null;
        setSumPendingId(null);
        setSumErr(`总结失败：等待超时（案例 ${id}，~/.ginno/synthesis/${id}）`);
        setSummarize({});
        setSumLoading(false);
        return;
      }
      getSynthesisCase(id)
        .then((r) => {
          if (r.ok && r.case.output) void finishSynthesisWait(id);
        })
        .catch(() => {
          /* transient — retry on the next tick */
        });
    }, 2000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sumPendingId]);

  // Summarize is the primary action and must ALWAYS run fresh — a leftover
  // draft (possibly from another session) must never intercept it. Draft
  // recovery is an explicit opt-in row in the dropdown (openDraftModal).
  async function openSummarize(sessionId?: string) {
    await freshSummarize(sessionId);
  }

  function openDraftModal() {
    const draft = readSummarizeDraft();
    if (!draft) return;
    setSumMenuOpen(false);
    setSumSource({
      id: draft.sourceSessionId || "",
      label: draft.sourceLabel || "上次草稿",
    });
    setSummarize(draft.dsl);
    setSumErr(null);
    setSumCreated(null);
  }

  function deleteDraft() {
    try {
      localStorage.removeItem(SUMMARIZE_DRAFT_KEY);
    } catch {
      /* ignore */
    }
    setDraftTick((n) => n + 1); // refresh the dropdown's draft row
  }

  function closeSummarize(saveDraft: boolean) {
    // S6: closing without creating keeps the draft recoverable for 24h
    // (sourceSessionId travels with it so ↺重新总结 works after restore). Only
    // non-trivial drafts are saved — an empty/failed `{}` must not become a
    // "restorable" draft that later confuses the user.
    const hasNodes = Array.isArray(summarize?.nodes) && (summarize!.nodes as unknown[]).length > 0;
    try {
      if (saveDraft && summarize && hasNodes) {
        localStorage.setItem(
          SUMMARIZE_DRAFT_KEY,
          JSON.stringify({
            dsl: summarize,
            sourceSessionId: sumSource?.id || undefined,
            sourceLabel: sumSource?.label,
            savedAt: Date.now(),
          }),
        );
      } else {
        localStorage.removeItem(SUMMARIZE_DRAFT_KEY);
      }
    } catch {
      /* storage unavailable */
    }
    setDraftTick((n) => n + 1);
    setSummarize(null);
    setSumErr(null);
    setSumCreated(null);
    // Abandon any in-flight synthesis wait — the server-side case keeps
    // running and stays visible in the 总结 panel; only the UI stops waiting.
    sumPendingRef.current = null;
    setSumPendingId(null);
    setSumLoading(false);
  }

  async function createFromSummarize(run: boolean, editedDsl: Record<string, unknown>) {
    if (!session) return;
    setSumBusy(run ? "run" : "create");
    setSumErr(null);
    try {
      const cw = await createWorkflow({
        name: (editedDsl.name as string) || "新流程",
        description: (editedDsl.description as string) || "",
        dsl: editedDsl,
        ...(sumSynthesisId ? { synthesis_id: sumSynthesisId } : {}),
      });
      const cwBody = cw as { ok?: boolean; workflow?: import("@/lib/types").WorkflowDef; detail?: string };
      if (!cwBody.workflow) {
        // json() doesn't throw on HTTP errors — surface the reason inline and
        // KEEP the modal open so the draft isn't lost.
        setSumErr(cwBody.detail || "创建工作流失败");
        return;
      }
      await g.reloadWorkflows(); // list reflects the new workflow immediately
      setDraftTick((n) => n + 1);
      try {
        localStorage.removeItem(SUMMARIZE_DRAFT_KEY); // created → draft consumed
      } catch { /* ignore */ }
      if (run) {
        const tr = await triggerWorkflowRun(cwBody.workflow.id, undefined, session.id);
        const trBody = tr as { ok?: boolean; run?: import("@/lib/types").WorkflowRun; detail?: string };
        if (trBody.run) {
          const list = runsBySessionRef.current[session.id] ?? [];
          if (!list.some((x) => x.id === trBody.run!.id)) list.push(trBody.run);
          runsBySessionRef.current[session.id] = [...list];
          syncDisplay(session.id);
        } else {
          // Created but not started: still a partial success — report inline.
          setSumErr(`已创建，但运行触发失败：${trBody.detail || "未知错误"}`);
          return;
        }
        setSummarize(null); // run card animates in — close the modal
      } else {
        // Create-only: keep the modal open with an explicit receipt so it is
        // unambiguous that the workflow was added (then 完成 closes it).
        setSumErr(null);
        setSumCreated(cwBody.workflow.name || "新流程");
      }
    } catch {
      setSumErr("无法连接运行时");
    } finally {
      setSumBusy(null);
    }
  }

  // S2: 进入开发会话精炼 — create the draft as v1 first, then open a
  // workflow-dev session where the agent can propose further edits.
  async function openDevFromSummarize(editedDsl: Record<string, unknown>) {
    setSumBusy("dev");
    setSumErr(null);
    try {
      const cw = await createWorkflow({
        name: (editedDsl.name as string) || "新流程",
        description: (editedDsl.description as string) || "",
        dsl: editedDsl,
        ...(sumSynthesisId ? { synthesis_id: sumSynthesisId } : {}),
      });
      const cwBody = cw as { ok?: boolean; workflow?: import("@/lib/types").WorkflowDef; detail?: string };
      if (!cwBody.workflow) {
        setSumErr(cwBody.detail || "创建工作流失败");
        return;
      }
      await g.reloadWorkflows();
      setDraftTick((n) => n + 1);
      try {
        localStorage.removeItem(SUMMARIZE_DRAFT_KEY);
      } catch { /* ignore */ }
      setSummarize(null);
      setSumCreated(null);
      await g.newSession("workflow-dev", {
        title: `精炼流程：${cwBody.workflow.name}`,
        workflow_id: cwBody.workflow.id,
      });
    } catch {
      setSumErr("无法连接运行时");
    } finally {
      setSumBusy(null);
    }
  }

  return {
    summarize, setSummarize, sumBusy, sumErr, sumCreated, sumLoading, sumSource,
    sumSynthesisId, sumMenuOpen, setSumMenuOpen, sumLastN, setSumLastN,
    savedDraft, freshSummarize, finishSynthesisWait, openSummarize,
    openDraftModal, deleteDraft, closeSummarize, createFromSummarize,
    openDevFromSummarize,
  };
}
