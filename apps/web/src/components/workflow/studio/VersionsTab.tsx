"use client";

import { useEffect, useState } from "react";
import { Check, Loader2, RotateCcw } from "lucide-react";
import * as api from "@/lib/runtime";
import type { WorkflowDef } from "@/lib/types";
import { DiffView } from "../DiffView";

/**
 * Version view (design B §3): every immutable DSL version with the diff of any
 * version against the current one, and rollback (which itself appends a new
 * version — history is never rewritten).
 *
 * Extracted from the old slide-in VersionHistoryDrawer so the Studio can show
 * it as a pane; the drawer remains for the Settings detail panel.
 */
export function VersionsTab({ wf, onChanged }: { wf: WorkflowDef; onChanged: () => void }) {
  const [versions, setVersions] = useState<Array<{ version: number; current: boolean; ts?: number }>>([]);
  const [loading, setLoading] = useState(true);
  const [sel, setSel] = useState<number | null>(null);
  const [diff, setDiff] = useState<string | null>(null);
  const [diffBusy, setDiffBusy] = useState(false);
  const [rolling, setRolling] = useState(false);
  const [confirmRoll, setConfirmRoll] = useState(false);

  const current = wf.version ?? 1;

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setSel(null);
    setDiff(null);
    setConfirmRoll(false);
    api
      .listWorkflowVersions(wf.id)
      .then((r) => {
        if (!alive) return;
        setVersions(r.versions || []);
        setLoading(false);
      })
      .catch(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [wf.id, current]);

  const inspect = async (v: number) => {
    if (sel === v) {
      setSel(null);
      setDiff(null);
      return;
    }
    setSel(v);
    setDiff(null);
    setConfirmRoll(false);
    if (v === current) return;
    setDiffBusy(true);
    try {
      const r = await api.diffWorkflowVersions(wf.id, v, current);
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
      const r = await api.rollbackWorkflow(wf.id, sel, `studio: 回滚到 v${sel}`);
      if (r.ok) {
        setSel(null);
        setDiff(null);
        onChanged();
      }
    } catch {
      /* keep the pane as-is */
    } finally {
      setRolling(false);
      setConfirmRoll(false);
    }
  };

  return (
    <div className="grid h-full grid-cols-1 gap-3 overflow-hidden lg:grid-cols-[280px_minmax(0,1fr)]">
      <div className="min-h-0 overflow-y-auto rounded-lg border border-line bg-card">
        <div className="flex items-center gap-1.5 border-b border-line px-2.5 py-2">
          <span className="text-[11.5px] font-semibold text-txt">版本历史</span>
          <span className="ml-auto font-mono text-[10px] text-faint">当前 v{current}</span>
        </div>
        {loading && (
          <div className="flex items-center justify-center gap-1.5 py-6 text-[11px] text-faint">
            <Loader2 className="h-3 w-3 animate-spin" /> 加载中…
          </div>
        )}
        {!loading &&
          versions.map((v) => {
            const isCur = v.version === current;
            const on = sel === v.version;
            return (
              <button
                key={v.version}
                onClick={() => void inspect(v.version)}
                className={`flex w-full items-center gap-2 border-b border-line2 px-2.5 py-2 text-left text-[11.5px] transition-colors ${
                  on ? "bg-card2/70" : "hover:bg-card/60"
                }`}
              >
                <span className={isCur ? "font-semibold text-violet" : "text-txt"}>v{v.version}</span>
                {isCur && <span className="text-[10px] text-faint">当前</span>}
                <span className="ml-auto font-mono text-[10px] text-faint">
                  {v.ts
                    ? new Date(v.ts * 1000).toLocaleString(undefined, {
                        month: "2-digit",
                        day: "2-digit",
                        hour: "2-digit",
                        minute: "2-digit",
                      })
                    : ""}
                </span>
              </button>
            );
          })}
        {!loading && !versions.length && (
          <div className="py-6 text-center text-[11px] text-faint">暂无版本记录</div>
        )}
      </div>

      <div className="min-h-0 overflow-y-auto rounded-lg border border-line bg-card p-3">
        {sel === null ? (
          <div className="flex h-full items-center justify-center text-center text-[11px] text-faint">
            选一个版本查看它与当前版本的差异，或回滚到它。
          </div>
        ) : sel === current ? (
          <div className="flex h-full items-center justify-center text-[11px] text-faint">
            v{current} 就是当前版本。
          </div>
        ) : (
          <div className="space-y-2">
            <div className="text-[11.5px] text-muted">
              v{sel} → v{current} 的差异
            </div>
            {diffBusy ? (
              <div className="flex items-center gap-1.5 text-[11px] text-faint">
                <Loader2 className="h-3 w-3 animate-spin" /> 计算差异…
              </div>
            ) : (
              <>
                <DiffView diff={diff ?? ""} />
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => (confirmRoll ? void rollback() : setConfirmRoll(true))}
                    disabled={rolling}
                    className={`btn-press flex items-center gap-1 rounded-md border px-2.5 py-1 text-[11.5px] disabled:opacity-50 ${
                      confirmRoll
                        ? "border-orange bg-orange/10 text-orange"
                        : "border-orange/40 text-orange hover:bg-orange/10"
                    }`}
                  >
                    {rolling ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : confirmRoll ? (
                      <Check className="h-3 w-3" />
                    ) : (
                      <RotateCcw className="h-3 w-3" />
                    )}
                    {rolling ? "回滚中…" : confirmRoll ? "确认回滚？" : `回滚到 v${sel}`}
                  </button>
                  <span className="text-[10.5px] text-faint">
                    回滚不会删除历史：它是把 v{sel} 的内容写成新的 v{current + 1}
                  </span>
                </div>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}