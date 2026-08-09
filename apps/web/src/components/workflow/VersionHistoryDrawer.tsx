"use client";

import { useEffect, useState } from "react";
import { ChevronRight, History, Loader2, RotateCcw, X } from "lucide-react";
import * as api from "@/lib/runtime";
import { DiffView } from "./DiffView";

/**
 * Slide-in version history drawer (workflow-ux-redesign P2): lists every
 * immutable DSL version, shows the diff of any version against the current
 * one, and rolls back with an inline confirm (no modal).
 */
export function VersionHistoryDrawer({
  workflowId,
  currentVersion,
  onClose,
  onRolledBack,
}: {
  workflowId: string;
  currentVersion: number;
  onClose: () => void;
  onRolledBack?: () => void;
}) {
  const [versions, setVersions] = useState<Array<{ version: number; current: boolean; ts?: number }>>([]);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState<number | null>(null); // version to inspect
  const [diff, setDiff] = useState<string | null>(null);
  const [diffBusy, setDiffBusy] = useState(false);
  const [rolling, setRolling] = useState(false);
  const [confirmRoll, setConfirmRoll] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .listWorkflowVersions(workflowId)
      .then((r) => {
        if (!alive) return;
        setVersions(r.versions || []);
        setLoading(false);
      })
      .catch(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [workflowId]);

  const inspect = async (v: number) => {
    if (sel === v) {
      setSel(null);
      setDiff(null);
      return;
    }
    setSel(v);
    setDiff(null);
    setConfirmRoll(false);
    if (v === currentVersion) return;
    setDiffBusy(true);
    try {
      const r = await api.diffWorkflowVersions(workflowId, v, currentVersion);
      setDiff(r.ok ? r.diff : "");
    } catch {
      setDiff("");
    } finally {
      setDiffBusy(false);
    }
  };

  const rollback = async () => {
    if (sel === null || rolling) return;
    setRolling(true);
    try {
      const r = await api.rollbackWorkflow(workflowId, sel, "rollback via version history");
      if (r.ok) {
        onRolledBack?.();
        onClose();
      }
    } catch {
      /* ignore — the drawer stays open */
    } finally {
      setRolling(false);
      setConfirmRoll(false);
    }
  };

  return (
    <div className="fixed inset-0 z-40" onClick={onClose}>
      <div
        className="absolute right-0 top-0 flex h-full w-72 flex-col border-l border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-line px-3 py-2.5">
          <History className="h-3.5 w-3.5 text-muted" />
          <span className="text-xs font-semibold text-txt">版本历史</span>
          <span className="ml-auto text-[10px] text-faint">v{currentVersion} 当前</span>
          <button onClick={onClose} className="rounded p-0.5 text-faint hover:bg-card2 hover:text-txt" aria-label="关闭">
            <X className="h-3.5 w-3.5" />
          </button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center gap-1.5 py-6 text-xs text-faint">
              <Loader2 className="h-3 w-3 animate-spin" /> 加载中…
            </div>
          )}
          {!loading &&
            versions.map((v) => {
              const isCur = v.version === currentVersion;
              const open = sel === v.version;
              return (
                <div key={v.version} className="border-b border-line2">
                  <button
                    onClick={() => void inspect(v.version)}
                    className={`flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-card ${
                      open ? "bg-card" : ""
                    }`}
                  >
                    <ChevronRight className={`h-3 w-3 text-faint transition-transform ${open ? "rotate-90" : ""}`} />
                    <span className={isCur ? "font-medium text-violet" : "text-txt"}>v{v.version}</span>
                    {isCur && <span className="text-[10px] text-faint">(当前)</span>}
                    {v.ts && (
                      <span className="text-[10px] text-faint">
                        {new Date(v.ts * 1000).toLocaleString(undefined, {
                          month: "2-digit",
                          day: "2-digit",
                          hour: "2-digit",
                          minute: "2-digit",
                        })}
                      </span>
                    )}
                    {!isCur && <span className="ml-auto text-[10px] text-faint">查看差异</span>}
                  </button>
                  {open && !isCur && (
                    <div className="space-y-2 px-3 pb-2">
                      {diffBusy ? (
                        <div className="flex items-center gap-1.5 text-[11px] text-faint">
                          <Loader2 className="h-3 w-3 animate-spin" /> 计算差异…
                        </div>
                      ) : (
                        <>
                          <div className="text-[10px] text-faint">v{v.version} → v{currentVersion} 的差异</div>
                          <DiffView diff={diff ?? ""} />
                          <button
                            onClick={() => (confirmRoll ? void rollback() : setConfirmRoll(true))}
                            disabled={rolling}
                            className={`btn-press flex w-full items-center justify-center gap-1 rounded-md border px-2 py-1 text-[11px] ${
                              confirmRoll
                                ? "border-orange bg-orange/10 text-orange"
                                : "border-orange/40 text-orange hover:bg-orange/10"
                            } disabled:opacity-50`}
                          >
                            {rolling ? <Loader2 className="h-3 w-3 animate-spin" /> : <RotateCcw className="h-3 w-3" />}
                            {rolling ? "回滚中…" : confirmRoll ? "确认回滚？（创建新版本）" : `回滚到 v${v.version}`}
                          </button>
                        </>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          {!loading && !versions.length && (
            <div className="py-6 text-center text-xs text-faint">暂无版本记录</div>
          )}
        </div>
      </div>
    </div>
  );
}
