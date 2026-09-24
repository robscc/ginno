"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Maximize2, Minus, Plus } from "lucide-react";
import {
  allEdges,
  computeLayout,
  STUDIO_BOX,
  type DagDsl,
  type NodeBox,
} from "./canvasLayout";

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

const MIN_ZOOM = 0.35;
const MAX_ZOOM = 2.2;
const DRAG_SLOP = 4; // px of travel before a press counts as a drag, not a click

type Pos = { x: number; y: number };
type DragState =
  | { mode: "pan"; sx: number; sy: number; ox: number; oy: number; moved: boolean }
  | { mode: "node"; id: string; sx: number; sy: number; ox: number; oy: number; moved: boolean }
  | null;

/**
 * Studio canvas: zoom (wheel, about the cursor), pan (drag the background),
 * cosmetic node repositioning (drag a node — client state only, never
 * persisted; topology comes from the DSL, not from this canvas).
 *
 * Node *parameters* are edited in the inspector, not here — see NodeInspector.
 */
export function StudioCanvas({
  dsl,
  status,
  selected,
  onSelect,
  posOverrides,
  onPosChange,
  box = STUDIO_BOX,
  className = "",
}: {
  dsl?: DagDsl;
  status?: Record<string, string>;
  selected?: string | null;
  onSelect?: (id: string | null) => void;
  posOverrides?: Record<string, Pos>;
  onPosChange?: (id: string, pos: Pos) => void;
  box?: NodeBox;
  className?: string;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<DragState>(null);
  const [view, setView] = useState({ x: 24, y: 24, z: 1 });

  const layout = useMemo(() => computeLayout(dsl || {}, box), [dsl, box]);
  const edges = useMemo(() => allEdges(dsl || {}), [dsl]);
  const nodes = dsl?.nodes || [];

  const posOf = useCallback(
    (id: string): Pos => posOverrides?.[id] ?? layout.pos.get(id) ?? { x: box.pad, y: box.pad },
    [posOverrides, layout, box.pad],
  );

  const fit = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    const cw = el.clientWidth || 1;
    const ch = el.clientHeight || 1;
    const z = Math.min(
      1,
      Math.max(MIN_ZOOM, Math.min((cw - 40) / layout.width, (ch - 40) / layout.height)),
    );
    setView({
      z,
      x: Math.max(12, (cw - layout.width * z) / 2),
      y: Math.max(12, (ch - layout.height * z) / 2),
    });
  }, [layout.width, layout.height]);

  // Re-fit when a different graph is loaded.
  const fitKey = `${dsl?.entry ?? ""}:${nodes.length}:${layout.width}`;
  useEffect(() => {
    fit();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey]);

  // Wheel zoom must preventDefault, which needs a non-passive listener — React's
  // onWheel is passive, so attach it by hand.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = el.getBoundingClientRect();
      const mx = e.clientX - rect.left;
      const my = e.clientY - rect.top;
      setView((v) => {
        const z = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v.z * Math.exp(-e.deltaY * 0.0015)));
        const k = z / v.z;
        return { z, x: mx - (mx - v.x) * k, y: my - (my - v.y) * k };
      });
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const onPointerDown = (e: React.PointerEvent) => {
    if (e.button !== 0) return;
    const target = (e.target as HTMLElement).closest("[data-node-id]") as HTMLElement | null;
    const id = target?.dataset.nodeId;
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
    if (id) {
      const p = posOf(id);
      dragRef.current = { mode: "node", id, sx: e.clientX, sy: e.clientY, ox: p.x, oy: p.y, moved: false };
    } else {
      dragRef.current = { mode: "pan", sx: e.clientX, sy: e.clientY, ox: view.x, oy: view.y, moved: false };
    }
  };

  const onPointerMove = (e: React.PointerEvent) => {
    const d = dragRef.current;
    if (!d) return;
    const dx = e.clientX - d.sx;
    const dy = e.clientY - d.sy;
    if (!d.moved && Math.hypot(dx, dy) < DRAG_SLOP) return;
    d.moved = true;
    if (d.mode === "pan") {
      setView((v) => ({ ...v, x: d.ox + dx, y: d.oy + dy }));
    } else {
      onPosChange?.(d.id, { x: d.ox + dx / view.z, y: d.oy + dy / view.z });
    }
  };

  const onPointerUp = (e: React.PointerEvent) => {
    const d = dragRef.current;
    dragRef.current = null;
    if (!d || d.moved) return;
    // A press with no travel is a click: nodes select, background clears.
    onSelect?.(d.mode === "node" ? (selected === d.id ? null : d.id) : null);
    void e;
  };

  if (!nodes.length) {
    return (
      <div className={`flex items-center justify-center rounded-lg border border-dashed border-line text-xs text-faint ${className}`}>
        该配方还没有节点
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onPointerCancel={onPointerUp}
      className={`relative select-none overflow-hidden rounded-lg border border-line bg-base/40 ${className}`}
      style={{
        backgroundImage: "radial-gradient(circle, rgb(var(--line)) 1px, transparent 1px)",
        backgroundSize: `${22 * view.z}px ${22 * view.z}px`,
        backgroundPosition: `${view.x}px ${view.y}px`,
        cursor: dragRef.current ? "grabbing" : "grab",
        touchAction: "none",
      }}
    >
      <div
        className="absolute left-0 top-0 origin-top-left"
        style={{ transform: `translate(${view.x}px, ${view.y}px) scale(${view.z})` }}
      >
        <svg
          width={layout.width}
          height={layout.height}
          className="pointer-events-none absolute left-0 top-0"
          aria-hidden="true"
        >
          <defs>
            <marker id="studio-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto">
              <path d="M0,0 L7,3 L0,6 Z" fill="rgb(var(--faint))" />
            </marker>
          </defs>
          {edges.map((e, i) => {
            const a = posOf(e.from);
            const b = posOf(e.to);
            const isLoopback = b.x <= a.x; // back-edge (retry / loop body)
            if (isLoopback) {
              // route back-edges under the row so they read as "return", not overlap
              const y = Math.max(a.y, b.y) + box.nh + 22;
              return (
                <path
                  key={i}
                  d={`M${a.x + box.nw / 2},${a.y + box.nh} V${y} H${b.x + box.nw / 2} V${b.y + box.nh}`}
                  fill="none"
                  stroke="rgb(var(--line2))"
                  strokeWidth={1.5}
                  strokeDasharray="5 4"
                  markerEnd="url(#studio-arrow)"
                />
              );
            }
            const x1 = a.x + box.nw;
            const y1 = a.y + box.nh / 2;
            const x2 = b.x;
            const y2 = b.y + box.nh / 2;
            const mx = (x1 + x2) / 2;
            return (
              <path
                key={i}
                d={`M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`}
                fill="none"
                stroke="rgb(var(--line2))"
                strokeWidth={1.5}
                markerEnd="url(#studio-arrow)"
              />
            );
          })}
        </svg>

        {nodes.map((n) => {
          const p = posOf(n.id);
          const st = status?.[n.id] || "pending";
          const color = STATUS_COLOR[st] || STATUS_COLOR.pending;
          const isSel = selected === n.id;
          const isLoop = n.type === "loop";
          return (
            <div
              key={n.id}
              data-node-id={n.id}
              className="absolute rounded-[10px] border bg-card px-3 py-2 shadow-sm"
              style={{
                left: p.x,
                top: p.y,
                width: box.nw,
                height: box.nh,
                cursor: "grab",
                borderStyle: isLoop ? "dashed" : "solid",
                borderColor: isSel ? "rgb(var(--violet))" : "rgb(var(--line2))",
                boxShadow: isSel ? "0 0 0 2px rgb(var(--violet) / 0.25)" : undefined,
              }}
            >
              {/* decorative ports — topology is edited in the DSL, not by dragging */}
              <span className="absolute -left-[5px] top-1/2 h-2 w-2 -translate-y-1/2 rounded-full border border-line2 bg-card2" />
              <span className="absolute -right-[5px] top-1/2 h-2 w-2 -translate-y-1/2 rounded-full border border-line2 bg-card2" />
              <div className="flex items-center gap-1.5">
                <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: color }} />
                <span className="truncate text-[12px] font-semibold text-txt">
                  {n.title || n.id}
                </span>
              </div>
              <div className="mt-0.5 truncate font-mono text-[9.5px] uppercase tracking-wide text-faint">
                {n.type}
                {n.agent ? ` · ${n.agent}` : ""}
              </div>
              {n.goal && (
                <div className="mt-0.5 line-clamp-2 text-[10.5px] leading-tight text-muted">{n.goal}</div>
              )}
            </div>
          );
        })}
      </div>

      <div className="absolute bottom-3 right-3 flex items-center gap-1 rounded-lg border border-line bg-panel/90 p-0.5 backdrop-blur">
        <button
          onClick={() => setView((v) => ({ ...v, z: Math.min(MAX_ZOOM, v.z * 1.2) }))}
          className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
          title="放大"
          aria-label="放大"
        >
          <Plus className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={() => setView((v) => ({ ...v, z: Math.max(MIN_ZOOM, v.z / 1.2) }))}
          className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
          title="缩小"
          aria-label="缩小"
        >
          <Minus className="h-3.5 w-3.5" />
        </button>
        <button
          onClick={fit}
          className="rounded p-1 text-muted hover:bg-card2 hover:text-txt"
          title="适配视图"
          aria-label="适配视图"
        >
          <Maximize2 className="h-3.5 w-3.5" />
        </button>
        <span className="px-1 font-mono text-[10px] tabular-nums text-faint">
          {Math.round(view.z * 100)}%
        </span>
      </div>
    </div>
  );
}