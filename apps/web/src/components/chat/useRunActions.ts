"use client";

// useRunActions — in-chat 运行卡片的操作动作（自 ChatStream.tsx 机械拆出，
// 行为零变化）：取消/暂停/继续/重试/检查点重试/删除。

import { useTranslations } from "next-intl";
import { useGinno } from "@/lib/store";
import {
  cancelWorkflowRun,
  decideWorkflowRun,
  deleteWorkflowRun,
  pauseWorkflowRun,
  retryWorkflowRun,
  retryWorkflowRunFromCheckpoint,
} from "@/lib/runtime";

interface RunActionsDeps {
  g: ReturnType<typeof useGinno>;
  // engine 持有的共享 ref：删除运行后同步 per-session 显示状态。
  curSessionIdRef: { current: string | null };
  runsBySessionRef: { current: Record<string, import("@/lib/types").WorkflowRun[]> };
  syncDisplay: (sid: string) => void;
}

export function useRunActions({ g, curSessionIdRef, runsBySessionRef, syncDisplay }: RunActionsDeps) {
  const tc = useTranslations("chat");

  function cancelRun(runId: string) {
    cancelWorkflowRun(runId);
  }
  function pauseRun(runId: string) {
    // Manual pause (#14): cooperative — the run flips to paused via the
    // run.status push once it reaches a safe boundary.
    void pauseWorkflowRun(runId);
  }
  function continueRun(runId: string) {
    decideWorkflowRun(runId, "continue");
  }
  function retryRun(runId: string): Promise<{ ok?: boolean; detail?: string } | void> {
    // The retry creates a NEW run bound to the same session; the run.bind push
    // (or the workflows.changed reload) surfaces it. Refresh the local list too.
    // Returns the outcome so LiveRunBlock can shake + show the reason on failure.
    return retryWorkflowRun(runId)
      .then((r) => {
        g.reloadWorkflowRuns();
        const body = r as { ok?: boolean; detail?: string } | undefined;
        if (body && body.ok === false) return { ok: false, detail: body.detail };
        return undefined;
      })
      .catch(() => ({ ok: false, detail: tc("run.retryNoConnection") }));
  }
  function retryRunFromCheckpoint(runId: string): Promise<{ ok?: boolean; detail?: string } | void> {
    // P2: re-execute from the persisted checkpoint (failed node + suffix only).
    return retryWorkflowRunFromCheckpoint(runId)
      .then((r) => {
        g.reloadWorkflowRuns();
        const body = r as { ok?: boolean; detail?: string } | undefined;
        if (body && body.ok === false) return { ok: false, detail: body.detail };
        return undefined;
      })
      .catch(() => ({ ok: false, detail: tc("run.retryNoConnection") }));
  }
  function deleteRun(runId: string) {
    void deleteWorkflowRun(runId).then(() => {
      const sid = curSessionIdRef.current;
      if (sid) {
        const list = (runsBySessionRef.current[sid] ?? []).filter((r) => r.id !== runId);
        runsBySessionRef.current[sid] = list;
        syncDisplay(sid);
      }
      g.reloadWorkflowRuns();
    });
  }

  return { cancelRun, pauseRun, continueRun, retryRun, retryRunFromCheckpoint, deleteRun };
}
