"use client";

import { useMemo, useState } from "react";
import {
  computeLayout,
  NW,
  NH,
  PREVIEW_BOX,
  type DagDsl as Dsl,
} from "./studio/canvasLayout";

const STATUS_COLOR: Record<string, string> = {
  done: "#22c55e",
  ok: "#22c55e",
  running: "#3b82f6",
  failed: "#ef4444",
  error: "#ef4444",
  pending: "#71717a",
};

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
  const layout = useMemo(() => computeLayout(dsl || {}, PREVIEW_BOX), [dsl]);

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
