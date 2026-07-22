"use client";

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { WikiPage } from "@/lib/types";

interface Node { id: string; title: string; x: number; y: number; vx: number; vy: number; deg: number; }
interface Edge { s: string; t: string; }

const COL_NODE = "#9a9aa6";
const COL_SEL = "#a78bfa";
const COL_NEIGH = "#c4b5fd";
const COL_EDGE = "#34343f";
const COL_EDGE_HI = "#8b5cf6";

/**
 * Obsidian-style graph view: one node per vault page, one edge per resolved
 * wikilink. Hand-rolled force simulation on an SVG (no d3/vis dependency):
 * repulsion + edge springs + gentle centering, with drag, hover-neighbour
 * highlighting and click-to-open.
 */
export function GraphView({ pages, selected, onSelect }: { pages: WikiPage[]; selected?: string | null; onSelect: (path: string) => void; }) {
  const wrapRef = useRef<HTMLDivElement | null>(null);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [size, setSize] = useState({ w: 800, h: 520 });
  const sizeRef = useRef(size);
  const [hover, setHover] = useState<string | null>(null);
  const nodesRef = useRef<Node[]>([]);
  const alphaRef = useRef(1);
  const dragRef = useRef<{ id: string; moved: boolean } | null>(null);
  const rafRef = useRef<number | null>(null);
  const [, setFrame] = useState(0);

  const { edges, adjacency, nodes } = useMemo(() => {
    const byTitle = new Map<string, WikiPage>();
    const byPath = new Map<string, WikiPage>();
    for (const p of pages) { byTitle.set(p.title.toLowerCase(), p); byPath.set(p.path.toLowerCase(), p); }
    const deg = new Map<string, number>();
    const seen = new Set<string>();
    const es: Edge[] = [];
    const adj = new Map<string, Set<string>>();
    for (const p of pages) {
      for (const l of p.links || []) {
        const t = byTitle.get(l.toLowerCase()) || byPath.get(l.toLowerCase());
        if (!t || t.path === p.path) continue;
        const key = [p.path, t.path].sort().join("");
        if (seen.has(key)) continue;
        seen.add(key);
        es.push({ s: p.path, t: t.path });
        deg.set(p.path, (deg.get(p.path) || 0) + 1);
        deg.set(t.path, (deg.get(t.path) || 0) + 1);
        if (!adj.has(p.path)) adj.set(p.path, new Set());
        if (!adj.has(t.path)) adj.set(t.path, new Set());
        adj.get(p.path)!.add(t.path);
        adj.get(t.path)!.add(p.path);
      }
    }
    const ns: Node[] = pages.map((p) => ({
      id: p.path, title: p.title, deg: deg.get(p.path) || 0, vx: 0, vy: 0, x: 0, y: 0,
    }));
    return { edges: es, adjacency: adj, nodes: ns };
  }, [pages]);

  // (re)lay out nodes in a circle whenever the page set changes.
  useEffect(() => {
    const { w, h } = sizeRef.current;
    const cx = w / 2, cy = h / 2, r = Math.min(w, h) * 0.36;
    const n = nodes.length || 1;
    nodes.forEach((nd, i) => {
      const a = (i / n) * Math.PI * 2;
      nd.x = cx + Math.cos(a) * r + (Math.random() - 0.5) * 8;
      nd.y = cy + Math.sin(a) * r + (Math.random() - 0.5) * 8;
      nd.vx = 0; nd.vy = 0;
    });
    nodesRef.current = nodes;
    alphaRef.current = 1;
    ensureRunning();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nodes]);

  useLayoutEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => {
      const rect = el.getBoundingClientRect();
      const s = { w: Math.max(320, rect.width), h: Math.max(360, rect.height) };
      sizeRef.current = s;
      setSize(s);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  function ensureRunning() {
    if (rafRef.current != null) return;
    const loop = () => {
      step();
      setFrame((f) => (f + 1) % 1000000);
      if (alphaRef.current > 0.02 || dragRef.current) rafRef.current = requestAnimationFrame(loop);
      else rafRef.current = null;
    };
    rafRef.current = requestAnimationFrame(loop);
  }
  useEffect(() => () => { if (rafRef.current != null) cancelAnimationFrame(rafRef.current); }, []);

  function step() {
    const ns = nodesRef.current;
    const { w, h } = sizeRef.current;
    const cx = w / 2, cy = h / 2;
    const alpha = alphaRef.current;
    for (let i = 0; i < ns.length; i++) {
      for (let j = i + 1; j < ns.length; j++) {
        let dx = ns[i].x - ns[j].x, dy = ns[i].y - ns[j].y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.01) { dx = Math.random() - 0.5; dy = Math.random() - 0.5; d2 = 0.01; }
        const f = (4200 * alpha) / d2;
        const d = Math.sqrt(d2);
        const fx = (dx / d) * f, fy = (dy / d) * f;
        ns[i].vx += fx; ns[i].vy += fy; ns[j].vx -= fx; ns[j].vy -= fy;
      }
    }
    const byId = new Map(ns.map((n) => [n.id, n]));
    for (const e of edges) {
      const a = byId.get(e.s), b = byId.get(e.t);
      if (!a || !b) continue;
      const dx = b.x - a.x, dy = b.y - a.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 0.01;
      const f = (d - 96) * 0.02 * alpha;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      a.vx += fx; a.vy += fy; b.vx -= fx; b.vy -= fy;
    }
    for (const nd of ns) {
      nd.vx += (cx - nd.x) * 0.004 * alpha;
      nd.vy += (cy - nd.y) * 0.004 * alpha;
      if (dragRef.current && dragRef.current.id === nd.id) { nd.vx = 0; nd.vy = 0; continue; }
      nd.vx *= 0.85; nd.vy *= 0.85;
      const sp = Math.hypot(nd.vx, nd.vy);
      if (sp > 30) { nd.vx = (nd.vx / sp) * 30; nd.vy = (nd.vy / sp) * 30; }
      nd.x += nd.vx; nd.y += nd.vy;
      nd.x = Math.max(20, Math.min(w - 20, nd.x));
      nd.y = Math.max(20, Math.min(h - 20, nd.y));
    }
    alphaRef.current = Math.max(0, alpha * 0.985 - 0.0008);
  }

  const toLocal = (e: React.PointerEvent) => {
    const rect = svgRef.current!.getBoundingClientRect();
    return { x: e.clientX - rect.left, y: e.clientY - rect.top };
  };
  const onDown = (e: React.PointerEvent, id: string) => {
    e.stopPropagation();
    (e.target as Element).setPointerCapture?.(e.pointerId);
    dragRef.current = { id, moved: false };
    alphaRef.current = Math.max(alphaRef.current, 0.35);
    ensureRunning();
  };
  const onMove = (e: React.PointerEvent) => {
    if (!dragRef.current) return;
    const { x, y } = toLocal(e);
    const nd = nodesRef.current.find((n) => n.id === dragRef.current!.id);
    if (nd) { nd.x = x; nd.y = y; nd.vx = 0; nd.vy = 0; dragRef.current.moved = true; }
  };
  const onUp = (e: React.PointerEvent) => {
    const d = dragRef.current;
    dragRef.current = null;
    if (d && !d.moved) onSelect(d.id);
    alphaRef.current = Math.max(alphaRef.current, 0.15);
    ensureRunning();
    void e;
  };

  const focus = hover || selected || null;
  const neigh = focus ? adjacency.get(focus) : null;
  const isHi = (id: string) => id === focus || (!!neigh && neigh.has(id));

  if (!nodes.length) {
    return (
      <div ref={wrapRef} className="flex h-[520px] items-center justify-center text-sm text-faint">
        还没有可绘制的页面 / 链接。
      </div>
    );
  }

  return (
    <div
      ref={wrapRef}
      className="relative h-[560px] w-full overflow-hidden rounded-xl border border-line bg-base/40"
      style={{
        backgroundImage:
          "radial-gradient(120% 90% at 50% -10%, rgba(139,92,246,0.12), transparent 55%), radial-gradient(90% 70% at 85% 110%, rgba(96,165,250,0.08), transparent 60%)",
      }}
    >
      <div className="pointer-events-none absolute left-3 top-2 z-10 text-[11px] text-faint">
        图谱 · {nodes.length} 页 · {edges.length} 链 — 拖拽节点 / 悬停看邻接 / 点击打开
      </div>
      <svg
        ref={svgRef}
        width={size.w}
        height={size.h}
        viewBox={`0 0 ${size.w} ${size.h}`}
        className="block touch-none select-none"
        onPointerMove={onMove}
        onPointerUp={onUp}
      >
        {edges.map((e, i) => {
          const a = nodesRef.current.find((n) => n.id === e.s);
          const b = nodesRef.current.find((n) => n.id === e.t);
          if (!a || !b) return null;
          const hi = !!focus && (e.s === focus || e.t === focus);
          return <line key={i} x1={a.x} y1={a.y} x2={b.x} y2={b.y} stroke={hi ? COL_EDGE_HI : COL_EDGE} strokeWidth={hi ? 1.6 : 1} strokeOpacity={hi ? 0.9 : focus ? 0.25 : 0.6} />;
        })}
        {nodesRef.current.map((n) => {
          const sel = n.id === selected;
          const hi = isHi(n.id);
          const dim = !!focus && !hi;
          const r = 4 + Math.min(n.deg, 8);
          const showLabel = nodes.length <= 60 || hi;
          return (
            <g
              key={n.id}
              transform={`translate(${n.x},${n.y})`}
              className="cursor-pointer"
              onPointerDown={(e) => onDown(e, n.id)}
              onPointerEnter={() => setHover(n.id)}
              onPointerLeave={() => setHover((h) => (h === n.id ? null : h))}
              opacity={dim ? 0.3 : 1}
            >
              <circle
                r={hi ? r + 1.5 : r}
                fill={sel ? COL_SEL : hi ? COL_NEIGH : COL_NODE}
                stroke={sel ? "#fff" : "none"}
                strokeWidth={sel ? 1.5 : 0}
                style={{
                  transition: "fill .15s ease, r .15s ease",
                  filter: sel || hi ? "drop-shadow(0 0 6px rgba(167,139,250,0.7))" : "none",
                }}
              />
              {showLabel && (
                <text x={r + 4} y={3} fontSize={11} fill={hi ? "#e9e9f0" : "#9a9aa6"} className="pointer-events-none">
                  {n.title.length > 22 ? n.title.slice(0, 22) + "…" : n.title}
                </text>
              )}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
