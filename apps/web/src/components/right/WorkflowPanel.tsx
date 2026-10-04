"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, Trash2, Workflow } from "lucide-react";
import { useGinno } from "@/lib/store";
import { LiveRunBlock } from "@/components/chat/RunBlocks";
import { ConfirmModal } from "@/components/ConfirmModal";
import {
  cancelWorkflowRun,
  cleanupWorkflowRuns,
  decideWorkflowRun,
  deleteWorkflowRun,
  pauseWorkflowRun,
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
  // i18n：工作流面板框架文案（运行块内部文案归 chat 域组件）
  const t = useTranslations("right.workflow");
  const tc = useTranslations("right.common");
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
    const timer = setInterval(() => g.reloadWorkflowRuns(), 1500);
    return () => clearInterval(timer);
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
        <span className="text-sm font-semibold text-txt">{t("title")}</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{runs.length}</span>
        {active && (
          <span className="ml-auto flex items-center gap-1.5 text-[11px] text-blue">
            <span className="inline-block h-2 w-2 animate-pulse rounded-full bg-blue" /> {t("running")}
          </span>
        )}
        {!active && terminalCount > 0 && (
          <div className="relative ml-auto">
            <button
              onClick={() => setCleanupMenu((v) => !v)}
              title={t("cleanupTitle")}
              className="flex items-center gap-1 rounded-md border border-line px-2 py-1 text-[11px] text-muted hover:bg-red/10 hover:text-red"
            >
              <Trash2 className="h-3 w-3" /> {t("cleanup", { n: terminalCount })} <ChevronDown className="h-3 w-3" />
            </button>
            {cleanupMenu && (
              <div className="absolute right-0 top-full z-20 mt-1 w-44 rounded-lg border border-line bg-card p-1 shadow-2xl">
                <button
                  onClick={() => doCleanup(["done"])}
                  disabled={!doneCount}
                  className="flex w-full items-center rounded-md px-2 py-1.5 text-left text-[11px] text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
                >
                  {t("cleanupDone", { n: doneCount })}
                </button>
                <button
                  onClick={() => doCleanup(["failed", "interrupted", "cancelled"])}
                  disabled={!failedCount}
                  className="flex w-full items-center rounded-md px-2 py-1.5 text-left text-[11px] text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
                >
                  {t("cleanupFailed", { n: failedCount })}
                </button>
                <button
                  onClick={() => (cleanupArm ? doCleanup() : setCleanupArm(true))}
                  onBlur={() => setCleanupArm(false)}
                  className={`flex w-full items-center rounded-md px-2 py-1.5 text-left text-[11px] ${
                    cleanupArm ? "bg-red/10 text-red" : "text-red/80 hover:bg-red/10 hover:text-red"
                  }`}
                >
                  {cleanupArm ? t("cleanupConfirm") : t("cleanupAll")}
                </button>
              </div>
            )}
          </div>
        )}
      </div>
      <div ref={listRef} className="flex-1 space-y-3 overflow-y-auto px-3">
        {runs.length === 0 && (
          <div className="px-1 py-6 text-center text-xs text-faint">{t("empty")}</div>
        )}
        {runs.map((r) => (
          <LiveRunBlock
            key={r.id}
            run={r}
            onCancel={(id) => cancelWorkflowRun(id)}
            onPause={(id) => void pauseWorkflowRun(id)}
            onContinue={(id) => decideWorkflowRun(id, "continue")}
            onRetry={(id) =>
              retryWorkflowRun(id)
                .then((res) => {
                  g.reloadWorkflowRuns();
                  const body = res as { ok?: boolean; detail?: string } | undefined;
                  if (body && body.ok === false) return { ok: false, detail: body.detail };
                  return undefined;
                })
                .catch(() => ({ ok: false, detail: t("runtimeUnreachable") }))
            }
            onRetryFromCheckpoint={(id) =>
              retryWorkflowRunFromCheckpoint(id)
                .then((res) => {
                  g.reloadWorkflowRuns();
                  const body = res as { ok?: boolean; detail?: string } | undefined;
                  if (body && body.ok === false) return { ok: false, detail: body.detail };
                  return undefined;
                })
                .catch(() => ({ ok: false, detail: t("runtimeUnreachable") }))
            }
            onDelete={(id) => setConfirm(`delete:${id}`)}
          />
        ))}
      </div>
      {g.workflows.length > 0 && (
        <div className="border-t border-line px-4 py-2 text-[11px] text-faint">
          {t("definitions", {
            n: g.workflows.length,
            names: g.workflows.map((w) => w.name).join(", "),
          })}
        </div>
      )}

      {confirm?.startsWith("delete:") && (
        <ConfirmModal
          title={t("deleteTitle")}
          message={t("deleteMessage")}
          confirmLabel={tc("delete")}
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
