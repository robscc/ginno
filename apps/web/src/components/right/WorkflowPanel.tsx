"use client";

import { useEffect, useState } from "react";
import { Trash2, Workflow } from "lucide-react";
import { useGinno } from "@/lib/store";
import { LiveRunBlock } from "@/components/chat/RunBlocks";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  cancelWorkflowRun,
  cleanupWorkflowRuns,
  decideWorkflowRun,
  deleteWorkflowRun,
  retryWorkflowRun,
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

  // Live refresh while a run is in flight (WS push handles the immediate cases).
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => g.reloadWorkflowRuns(), 1500);
    return () => clearInterval(t);
  }, [active, g]);

  const doDelete = (id: string) => {
    void deleteWorkflowRun(id).then(() => g.reloadWorkflowRuns());
  };
  const doCleanup = () => {
    void cleanupWorkflowRuns().then(() => g.reloadWorkflowRuns());
  };

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
          <button
            onClick={() => setConfirm("cleanup")}
            title="清除所有已完成/失败/中断的运行记录"
            className="ml-auto flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted hover:bg-red/10 hover:text-red"
          >
            <Trash2 className="h-3 w-3" /> 清除已完成 ({terminalCount})
          </button>
        )}
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto px-3">
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
            onDelete={(id) => setConfirm(`delete:${id}`)}
          />
        ))}
      </div>
      {g.workflows.length > 0 && (
        <div className="border-t border-line px-4 py-2 text-[11px] text-faint">
          {g.workflows.length} definition(s): {g.workflows.map((w) => w.name).join(", ")}
        </div>
      )}

      {confirm === "cleanup" && (
        <ConfirmModal
          title="清除已完成的运行记录"
          message={`将删除 ${terminalCount} 条已完成/失败/中断的运行记录及其事件日志。正在运行或暂停的 run 不受影响。`}
          confirmLabel="清除"
          onConfirm={() => {
            setConfirm(null);
            doCleanup();
          }}
          onCancel={() => setConfirm(null)}
        />
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
