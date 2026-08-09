"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, Trash2, Workflow } from "lucide-react";
import { useGinno } from "@/lib/store";
import { LiveRunBlock } from "@/components/chat/RunBlocks";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  cancelWorkflowRun,
  cleanupWorkflowRuns,
  decideWorkflowRun,
  deleteWorkflowRun,
  retryWorkflowRun,
  retryWorkflowRunFromCheckpoint,
} from "@/lib/runtime";

const TERMINAL = new Set(["done", "failed", "cancelled", "interrupted"]);

/**
 * Right-panel Workflow tab (design A): renders live run blocks (same component as
 * the in-chat blocks) with cancel/continue/retry/delete controls + a jump to the
 * workflow page. Polls while any run is active as a fallback to the run.* WS push.
 */
export function WorkflowPanel() {
  const g = useGinno();
  const runs = g.workflowRuns;
  const active = runs.some((r) => r.status === "running" || r.status === "paused");
  const terminalCount = runs.filter((r) => TERMINAL.has(r.status)).length;
  // Which run the confirm modal targets: "delete:<id>" or "cleanup".
  const [confirm, setConfirm] = useState<string | null>(null);
  // P3: cleanup dropdown — 已完成 / 已失败 / 全部 (inline-confirm).
  const [cleanupMenu, setCleanupMenu] = useState(false);
  const [cleanupArm, setCleanupArm] = useState(false);
  const listRef = useRef<HTMLDivElement | null>(null);

  // Live refresh while a run is in flight (WS push handles the immediate cases).
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => g.reloadWorkflowRuns(), 1500);
    return () => clearInterval(t);
  }, [active, g]);

  // P1: opening the tab while a run waits for human input scrolls straight to
  // it — the yellow dock badge promised "something needs you".
  useEffect(() => {
    if (!g.pendingHumanCount) return;
    const el = listRef.current?.querySelector('[data-waiting-human="true"]');
    el?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }, [g.pendingHumanCount]);

  const doDelete = (id: string) => {
    void deleteWorkflowRun(id).then(() => g.reloadWorkflowRuns());
  };
  const doCleanup = (statuses?: string[]) => {
    setCleanupMenu(false);
    setCleanupArm(false);
    void cleanupWorkflowRuns(statuses).then(() => g.reloadWorkflowRuns());
  };
  const doneCount = runs.filter((r) => r.status === "done").length;
  const failedCount = runs.filter((r) => ["failed", "interrupted", "cancelled"].includes(r.status)).length;

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Workflow className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">Workflow Runs</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{runs.length}</span>
        {active && (
          <span className="ml-auto flex items-center gap-1.5 text-[11px] text-blue">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-blue" /> 运行中
          </span>
        )}
        {!active && terminalCount > 0 && (
          <div className="relative ml-auto">
            <button
              onClick={() => setCleanupMenu((v) => !v)}
              title="清除历史运行记录"
              className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted hover:bg-red/10 hover:text-red"
            >
              <Trash2 className="h-3 w-3" /> 清除 ({terminalCount}) <ChevronDown className="h-3 w-3" />
            </button>
            {cleanupMenu && (
              <div className="absolute right-0 top-full z-20 mt-1 w-44 rounded-lg border border-line bg-card p-1 shadow-2xl">
                <button
                  onClick={() => doCleanup(["done"])}
                  disabled={!doneCount}
                  className="flex w-full items-center rounded-md px-2 py-1.5 text-left text-[11px] text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
                >
                  清除已完成 ({doneCount})
                </button>
                <button
                  onClick={() => doCleanup(["failed", "interrupted", "cancelled"])}
                  disabled={!failedCount}
                  className="flex w-full items-center rounded-md px-2 py-1.5 text-left text-[11px] text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
                >
                  清除已失败 ({failedCount})
                </button>
                <button
                  onClick={() => (cleanupArm ? doCleanup() : setCleanupArm(true))}
                  onBlur={() => setCleanupArm(false)}
                  className={`flex w-full items-center rounded-md px-2 py-1.5 text-left text-[11px] ${
                    cleanupArm ? "bg-red/10 text-red" : "text-red/80 hover:bg-red/10 hover:text-red"
                  }`}
                >
                  {cleanupArm ? "确认清除全部？" : "清除全部历史"}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
      <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto px-3">
        {runs.length === 0 && (
          <div className="px-1 py-6 text-center text-xs text-faint">
            No runs yet. 在聊天里点「⌁ 总结成流程 → 创建并运行」，或让 agent 运行一个 workflow。
          </div>
        )}
        {runs.map((r) => (
          <LiveRunBlock
            key={r.id}
            run={r}
            onCancel={(id) => cancelWorkflowRun(id)}
            onContinue={(id) => decideWorkflowRun(id, "continue")}
            onRetry={(id) =>
              retryWorkflowRun(id)
                .then((res) => {
                  g.reloadWorkflowRuns();
                  const body = res as { ok?: boolean; detail?: string } | undefined;
                  if (body && body.ok === false) return { ok: false, detail: body.detail };
                  return undefined;
                })
                .catch(() => ({ ok: false, detail: "无法连接运行时" }))
            }
            onRetryFromCheckpoint={(id) =>
              retryWorkflowRunFromCheckpoint(id)
                .then((res) => {
                  g.reloadWorkflowRuns();
                  const body = res as { ok?: boolean; detail?: string } | undefined;
                  if (body && body.ok === false) return { ok: false, detail: body.detail };
                  return undefined;
                })
                .catch(() => ({ ok: false, detail: "无法连接运行时" }))
            }
            onDelete={(id) => setConfirm(`delete:${id}`)}
          />
        ))}
      </div>
      {g.workflows.length > 0 && (
        <div className="border-t border-line px-4 py-2 text-[11px] text-faint">
          {g.workflows.length} definition(s): {g.workflows.map((w) => w.name).join(", ")}
        </div>
      )}

      {confirm?.startsWith("delete:") && (
        <ConfirmModal
          title="删除运行记录"
          message="删除该运行记录？事件日志与检查点将一并删除，此操作不可撤销。"
          confirmLabel="删除"
          onConfirm={() => {
            const id = confirm.slice("delete:".length);
            setConfirm(null);
            doDelete(id);
          }}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  );
}
