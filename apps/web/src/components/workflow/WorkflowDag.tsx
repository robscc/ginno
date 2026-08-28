"use client";

import { useMemo, useState } from "react";

type Node = { id: string; type: string; title?: string; goal?: string; agent?: string };
type Edge = { from: string; to: string };
type Dsl = { entry?: string; nodes?: Node[]; edges?: Edge[] };

const STATUS_COLOR: Record<string, string> = {
  done: "#22c55e",
  ok: "#22c55e",
  running: "#3b82f6",
  failed: "#ef4444",
  error: "#ef4444",
  pending: "#71717a",
};

const NW = 132;
const NH = 40;
const GX = 56;
const GY = 60;

/** BFS layering from entry → {nodeId: layer}; branch targets share a layer band. */
function layerOrder(dsl: Dsl): Map<string, number> {
  const nodes = dsl.nodes || [];
  const adj = new Map<string, string[]>();
  for (const e of dsl.edges || []) {
    adj.set(e.from, [...(adj.get(e.from) || []), e.to]);
  }
  // include branch case targets as adjacency too
  for (const n of nodes) {
    if (n.type === "branch") {
      const cs = ((n as unknown as { cases?: { then?: string }[] }).cases || [])
        .map((c) => c.then)
        .filter(Boolean) as string[];
      const def = (n as unknown as { default?: string }).default;
      adj.set(n.id, [...(adj.get(n.id) || []), ...cs, ...(def ? [def] : [])]);
    }
  }
  const layer = new Map<string, number>();
  const queue: string[] = [];
  // A branch back-edge (retry/loop) makes longest-path layering diverge; clamp
  // the layer and cap iterations so a cycle can never spin this BFS forever and
  // freeze the tab.
  const maxLayer = Math.max(1, nodes.length);
  const cap = nodes.length * nodes.length + 16;
  let guard = 0;
  if (dsl.entry) {
    layer.set(dsl.entry, 0);
    queue.push(dsl.entry);
  }
  while (queue.length && guard++ < cap) {
    const cur = queue.shift()!;
    for (const nx of adj.get(cur) || []) {
      const cand = Math.min((layer.get(cur) ?? 0) + 1, maxLayer);
      if (!layer.has(nx) || layer.get(nx)! < cand) {
        layer.set(nx, cand);
        queue.push(nx);
      }
    }
  }
  // any unreached node (disconnected) gets its own trailing layer
  for (const n of nodes) if (!layer.has(n.id)) layer.set(n.id, 0);
  return layer;
}

export function WorkflowDag({
  dsl,
  status,
  selected,
  onSelect,
  interactive = true,
}: {
  dsl?: Dsl;
  status?: Record<string, string>;
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  /** false = pure preview (e.g. SummarizeModal): nodes are not clickable. */
  interactive?: boolean;
}) {
  const [sel, setSel] = useState<string | null>(null);
  const selId = selected !== undefined ? selected : sel;
  const setSelId = (v: string | null) => {
    if (onSelect) onSelect(v);
    else setSel(v);
  };
  const layout = useMemo(() => {
    const nodes = dsl?.nodes || [];
    const layer = layerOrder(dsl || {});
    const columns = new Map<number, string[]>();
    for (const n of nodes) {
      const L = layer.get(n.id) ?? 0;
      if (!columns.has(L)) columns.set(L, []);
      columns.get(L)!.push(n.id);
    }
    const pos = new Map<string, { x: number; y: number }>();
    let maxRows = 1;
    for (const [, ids] of columns) maxRows = Math.max(maxRows, ids.length);
    for (const [L, ids] of columns) {
      ids.forEach((id, r) => pos.set(id, { x: 16 + L * (NW + GX), y: 16 + r * (NH + GY) }));
    }
    const width = 32 + (columns.size || 1) * (NW + GX);
    const height = 32 + maxRows * (NH + GY);
    return { pos, width, height };
  }, [dsl]);

  if (!dsl?.nodes?.length) {
    return <div className="py-4 text-center text-xs text-faint">无 DSL 节点</div>;
  }
  const byId = new Map((dsl.nodes || []).map((n) => [n.id, n]));
  const edges = dsl.edges || [];

  return (
    <div className="overflow-auto rounded-lg border border-line bg-base/40">
      <svg width={layout.width} height={layout.height} className="block">
        <defs>
          <marker id="wf-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
            <path d="M0,0 L7,3 L0,6 Z" fill="rgb(var(--faint))" />
          </marker>
        </defs>
        {edges.map((e, i) => {
          const a = layout.pos.get(e.from);
          const b = layout.pos.get(e.to);
          if (!a || !b) return null;
          const x1 = a.x + NW;
          const y1 = a.y + NH / 2;
          const x2 = b.x;
          const y2 = b.y + NH / 2;
          const mx = (x1 + x2) / 2;
          return (
            <path
              key={i}
              d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
              fill="none"
              stroke="rgb(var(--line2))"
              strokeWidth={1.5}
              markerEnd="url(#wf-arrow)"
            />
          );
        })}
        {(dsl.nodes || []).map((n) => {
          const p = layout.pos.get(n.id);
          if (!p) return null;
          const st = status?.[n.id] || "pending";
          const color = STATUS_COLOR[st] || STATUS_COLOR.pending;
          const isSel = selId === n.id;
          return (
            <g
              key={n.id}
              transform={`translate(${p.x},${p.y})`}
              onClick={interactive ? () => setSelId(isSel ? null : n.id) : undefined}
              style={{ cursor: interactive ? "pointer" : "default" }}
            >
              <rect
                width={NW}
                height={NH}
                rx={8}
                fill="rgb(var(--card))"
                stroke={isSel ? color : "rgb(var(--line2))"}
                strokeWidth={isSel ? 2 : 1}
              />
              <circle cx={12} cy={NH / 2} r={4} fill={color} />
              <text x={22} y={17} fill="rgb(var(--txt))" fontSize={11} fontWeight={600}>
                {(n.title || n.goal || n.id).slice(0, 16)}
              </text>
              <text x={22} y={31} fill="rgb(var(--faint))" fontSize={9}>
                {n.type}
                {n.agent ? ` · ${n.agent}` : ""}
              </text>
            </g>
          );
        })}
      </svg>
    </div>
  );
}
