"use client";

/**
 * Shared DAG layout math (extracted from WorkflowDag so the Studio canvas and
 * the read-only previews agree on node placement).
 *
 * `WorkflowDag` keeps its original 132×40 geometry by importing the defaults;
 * the Studio canvas passes its larger node box in explicitly.
 */

export type DagNode = {
  id: string;
  type: string;
  title?: string;
  goal?: string;
  agent?: string;
  [k: string]: unknown;
};
export type DagEdge = { from: string; to: string };
export type DagDsl = { entry?: string; nodes?: DagNode[]; edges?: DagEdge[] };
export type NodeBox = { nw: number; nh: number; gx: number; gy: number; pad: number };

export const NW = 132;
export const NH = 40;
export const GX = 56;
export const GY = 60;

/** Read-only preview geometry (unchanged from the original WorkflowDag). */
export const PREVIEW_BOX: NodeBox = { nw: NW, nh: NH, gx: GX, gy: GY, pad: 16 };
/** Studio canvas geometry: roomier cards with a title, type row and goal line. */
export const STUDIO_BOX: NodeBox = { nw: 184, nh: 66, gx: 62, gy: 34, pad: 24 };

/** BFS layering from entry → {nodeId: layer}; branch targets share a layer band. */
export function layerOrder(dsl: DagDsl): Map<string, number> {
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

/** Layer/row positions for every node, plus the bounding size of the drawing. */
export function computeLayout(
  dsl: DagDsl,
  box: NodeBox = PREVIEW_BOX,
): { pos: Map<string, { x: number; y: number }>; width: number; height: number; rows: number } {
  const nodes = dsl.nodes || [];
  const layer = layerOrder(dsl);
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
    ids.forEach((id, r) =>
      pos.set(id, { x: box.pad + L * (box.nw + box.gx), y: box.pad + r * (box.nh + box.gy) }),
    );
  }
  const width = box.pad * 2 + (columns.size || 1) * (box.nw + box.gx);
  const height = box.pad * 2 + maxRows * (box.nh + box.gy);
  return { pos, width, height, rows: maxRows };
}

/** Branch routing targets (cases + default) — the canvas draws these as edges
 *  because branch nodes have no explicit out-edge in the DSL. */
export function branchTargets(n: DagNode): string[] {
  const out: string[] = [];
  const cases = (n as unknown as { cases?: { then?: string }[] }).cases || [];
  for (const c of cases) if (c?.then) out.push(c.then);
  const def = (n as unknown as { default?: string }).default;
  if (def) out.push(def);
  return out;
}

/** Every drawn edge of the graph: explicit edges plus branch routing. */
export function allEdges(dsl: DagDsl): DagEdge[] {
  const edges: DagEdge[] = [...(dsl.edges || [])];
  for (const n of dsl.nodes || []) {
    if (n.type === "branch") {
      for (const t of branchTargets(n)) edges.push({ from: n.id, to: t });
    }
  }
  return edges;
}