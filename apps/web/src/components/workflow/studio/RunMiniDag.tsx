"use client";

import { useMemo } from "react";
import { Hexagon } from "lucide-react";
import type { WorkflowRun, WorkflowRunEvent } from "@/lib/types";
import { branchTargets, layerOrder, type DagDsl } from "./canvasLayout";

const STATUS_COLOR: Record<string, string> = {
  done: "#22c55e",
  ok: "#22c55e",
  running: "#3b82f6",
  paused: "#f59e0b",
  failed: "#ef4444",
  error: "#ef4444",
  interrupted: "#f97316",
  cancelled: "#71717a",
  skipped: "#a1a1aa",
  pending: "#71717a",
};

type MiniNode = { id: string; type: string; title: string; synthetic?: "extract" };

/**
 * Run observer's mini-DAG (方案B 打磨): the whole graph as one horizontally
 * scrollable strip, nodes in topological order, coloured by step status.
 *
 * Deliberately NOT a 2-D graph — branches show their routing targets as
 * stacked labels after the branch chip, loops show an ↻N badge instead of a
 * back-edge. Clicking a chip selects the node (same shell nodeId state the
 * step list and event filter use; click again to clear).
 */
export function RunMiniDag({
  dsl,
  run,
  events,
  selNode,
  onSelectNode,
}: {
  dsl?: DagDsl;
  run: WorkflowRun | null;
  events: WorkflowRunEvent[];
  selNode: string | null;
  onSelectNode: (id: string | null) => void;
}) {
  const stepStatus = useMemo(() => {
    const m = new Map<string, string>();
    for (const s of run?.steps || []) m.set(s.id, s.status);
    return m;
  }, [run]);

  const nodes = useMemo<MiniNode[]>(() => {
    const raw = (dsl?.nodes || []).filter((n) => !!n.id);
    if (!raw.length) return [];
    const layer = layerOrder(dsl || {});
    const known = new Set(raw.map((n) => n.id));
    // DSL order within a layer keeps the strip deterministic across renders.
    const sorted = [...raw].sort(
      (a, b) => (layer.get(a.id) ?? 0) - (layer.get(b.id) ?? 0),
    );
    const out: MiniNode[] = [];
    for (const n of sorted) {
      out.push({ id: n.id, type: n.type, title: n.title || n.id });
      // The compiler injects `<id>__extract` nodes after every `writes`
      // declaring node (they ARE in run.steps, just not in the stored DSL) —
      // surface them as small icon chips so statuses don't dangle.
      const extId = `${n.id}__extract`;
      if (!known.has(extId) && stepStatus.has(extId)) {
        out.push({ id: extId, type: "extract", title: extId, synthetic: "extract" });
      }
    }
    return out;
  }, [dsl, stepStatus]);

  // ↻N per loop head: loop_iter events carry the loop node's id.
  const loopIters = useMemo(() => {
    const m = new Map<string, number>();
    for (const e of events) {
      if (e.kind === "loop_iter" && e.node_id) m.set(e.node_id, (m.get(e.node_id) || 0) + 1);
    }
    return m;
  }, [events]);

  // A paused run parks at one node — the gate may be a compiler-injected
  // `<N>__sup` chip that isn't in the stored DSL, so strip the suffix.
  const pausedAt = useMemo(() => {
    if (run?.status !== "paused") return null;
    const nid = run.pending_interrupt?.node_id;
    return nid ? nid.replace(/__sup$/, "") : null;
  }, [run]);

  if (!nodes.length || !run) return null;

  return (
    <div className="overflow-x-auto rounded-lg border border-line bg-base/40 px-2 py-1.5">
      <div className="flex min-h-[28px] items-start gap-0.5" style={{ maxHeight: 84 }}>
        {nodes.map((n, i) => {
          // the parked node's step still reads "running" mid-interrupt — the
          // ⏸ pause wins so the strip reads "waiting", not "spinning"
          const st =
            pausedAt === n.id ? "paused" : stepStatus.get(n.id) || "pending";
          const color = STATUS_COLOR[st] || STATUS_COLOR.pending;
          const active = st === "running" || st === "paused";
          const sel = selNode === n.id;
          const isPausedHere = pausedAt === n.id;
          const iters = n.type === "loop" ? loopIters.get(n.id) || 0 : 0;
          const targets = n.type === "branch" ? branchTargets(dsl?.nodes?.find((x) => x.id === n.id) || { id: n.id, type: "branch" }) : [];
          return (
            <div key={n.id} className="flex items-start gap-0.5">
              {i > 0 && <span className="mt-1 shrink-0 text-[10px] leading-5 text-faint">→</span>}
              {n.synthetic === "extract" ? (
                <button
                  onClick={() => onSelectNode(sel ? null : n.id)}
                  title={`抽取 · ${n.id}`}
                  className={`mt-0.5 flex shrink-0 items-center rounded border px-1 py-0.5 transition-colors ${
                    sel ? "border-violet bg-violet/10" : "border-line2 bg-card hover:bg-card2/60"
                  }`}
                >
                  <Hexagon
                    className="h-3 w-3"
                    style={{ color }}
                    fill={st === "pending" ? "none" : color}
                    fillOpacity={0.25}
                  />
                </button>
              ) : (
                <div className="flex shrink-0 flex-col items-start gap-0.5">
                  <button
                    onClick={() => onSelectNode(sel ? null : n.id)}
                    title={`${n.id} · ${st}${n.type !== "step" && n.type !== "agent" && n.type !== "llm" ? ` · ${n.type}` : ""}`}
                    className={`relative flex shrink-0 items-center gap-1 rounded border px-1.5 py-0.5 text-[10.5px] transition-colors ${
                      sel ? "border-violet bg-violet/10 text-txt" : "border-line2 bg-card text-txt hover:bg-card2/60"
                    } ${active ? `ring-1 shadow-sm ${st === "running" ? "ring-blue/60" : "ring-orange/60"}` : ""}`}
                    style={
                      active
                        ? { boxShadow: `0 0 6px ${color}66` }
                        : undefined
                    }
                  >
                    <span
                      className={`h-1.5 w-1.5 shrink-0 rounded-full ${st === "running" ? "animate-pulse" : ""}`}
                      style={{ background: color }}
                    />
                    <span className="max-w-[110px] truncate">{n.title}</span>
                    {iters > 0 && (
                      <span
                        className="rounded bg-orange/15 px-0.5 font-mono text-[9px] leading-[14px] text-orange"
                        title={`${iters} 次迭代`}
                      >
                        ↻{iters}
                      </span>
                    )}
                    {/* human / supervisor gate parked here (a compiler-injected
                        `<N>__sup` id was mapped back to its source above) */}
                    {isPausedHere && (
                      <span
                        className="absolute -right-1.5 -top-1.5 rounded-full bg-orange px-0.5 text-[8px] leading-[12px] text-white"
                        title="运行暂停在此节点"
                      >
                        ⏸
                      </span>
                    )}
                  </button>
                  {/* branch routing: stacked labels, hit targets coloured by
                      the target's own status, misses grey + dashed */}
                  {targets.length > 0 && (
                    <div className="ml-1.5 flex flex-col gap-px">
                      {targets.map((t) => {
                        const tst = stepStatus.get(t);
                        const hit = !!tst && tst !== "pending";
                        const tTitle = dsl?.nodes?.find((x) => x.id === t)?.title || t;
                        return (
                          <button
                            key={t}
                            onClick={() => onSelectNode(selNode === t ? null : t)}
                            title={`路由 → ${tTitle}（${tst || "未命中"}）`}
                            className={`flex items-center gap-0.5 rounded border px-1 py-px text-[9px] leading-[14px] ${
                              hit
                                ? "border-solid bg-card text-txt"
                                : "border-dashed border-line2 text-faint"
                            } ${selNode === t ? "border-violet" : ""}`}
                          >
                            <span className="text-faint">⊃</span>
                            {hit && (
                              <span
                                className="h-1 w-1 shrink-0 rounded-full"
                                style={{ background: STATUS_COLOR[tst!] || STATUS_COLOR.pending }}
                              />
                            )}
                            <span className="max-w-[72px] truncate">{tTitle}</span>
                          </button>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
