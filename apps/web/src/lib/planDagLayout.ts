// planDagLayout — 任务清单依赖图的纯布局逻辑（零渲染依赖，node 环境可测）。
//
// 横向分层 DAG（与 workflow 编辑器 canvasLayout 同构：左→右，layer = 列，
// 列内垂直居中）：longest-path 分层（源在第 0 列，节点列号 = 上游最大列号 + 1）
// + barycenter 排序减少边交叉。后端 plans/store.py 建单时已拓扑校验拒环，
// 前端仍做环防御（脏快照不崩渲染）。

export interface DagInputItem {
  item_id: string;
  depends_on?: string[];
}

export interface DagNode {
  id: string;
  layer: number;
  order: number;
  /** px，节点左上角 */
  x: number;
  y: number;
  /** 有效上游（过滤掉不存在的 id） */
  deps: string[];
  dependents: string[];
}

export interface DagEdge {
  from: string;
  to: string;
}

export interface DagLayout {
  nodes: DagNode[];
  edges: DagEdge[];
  width: number;
  height: number;
  layers: number;
}

// 节点尺寸与 workflow PREVIEW_BOX 同族（WorkflowDag NW=132/NH=40，plan 的
// goal 副标题略宽取 150×44）；列距/行距/边距同构 workflow 画布。
export const DAG_NODE_W = 150;
export const DAG_NODE_H = 44;
const GAP_X = 56;
const GAP_Y = 18;
const PAD = 16;

export function layoutPlanDag(items: DagInputItem[]): DagLayout {
  if (items.length === 0) {
    return { nodes: [], edges: [], width: 0, height: 0, layers: 0 };
  }
  const ids = new Set(items.map((i) => i.item_id));
  const deps = new Map<string, string[]>();
  const dependents = new Map<string, string[]>();
  for (const it of items) {
    const d = [...new Set(it.depends_on ?? [])].filter((x) => ids.has(x) && x !== it.item_id);
    deps.set(it.item_id, d);
    if (!dependents.has(it.item_id)) dependents.set(it.item_id, []);
    for (const x of d) {
      if (!dependents.has(x)) dependents.set(x, []);
      dependents.get(x)!.push(it.item_id);
    }
  }

  // ---- 分层：Kahn 拓扑 + longest-path。环防御：拓扑排不完的节点兜底落
  // 当前最大层 + 1（不崩、不丢节点；正常数据后端已拒环）。
  const layer = new Map<string, number>();
  const indeg = new Map<string, number>();
  for (const it of items) indeg.set(it.item_id, deps.get(it.item_id)!.length);
  const queue = items.filter((i) => indeg.get(i.item_id) === 0).map((i) => i.item_id);
  for (const id of queue) layer.set(id, 0);
  let head = 0;
  while (head < queue.length) {
    const id = queue[head++];
    for (const child of dependents.get(id) ?? []) {
      layer.set(child, Math.max(layer.get(child) ?? 0, layer.get(id)! + 1));
      indeg.set(child, indeg.get(child)! - 1);
      if (indeg.get(child) === 0) queue.push(child);
    }
  }
  let maxLayer = 0;
  for (const l of layer.values()) maxLayer = Math.max(maxLayer, l);
  for (const it of items) {
    if (!layer.has(it.item_id)) layer.set(it.item_id, ++maxLayer);
  }

  // ---- 列内初始序 = 插入序；barycenter 左右各扫两遍减交叉。
  const byLayer: string[][] = [];
  for (const it of items) {
    const l = layer.get(it.item_id)!;
    (byLayer[l] ??= []).push(it.item_id);
  }
  // 环兜底分层可能留下空列（稀疏数组洞）——填平，sort/坐标不崩。
  for (let l = 0; l < byLayer.length; l++) byLayer[l] ??= [];
  const order = new Map<string, number>();
  byLayer.forEach((row) => row.forEach((id, i) => order.set(id, i)));
  const bary = (id: string, neighbors: string[]) => {
    if (neighbors.length === 0) return order.get(id)!;
    return neighbors.reduce((s, n) => s + (order.get(n) ?? 0), 0) / neighbors.length;
  };
  for (let pass = 0; pass < 2; pass++) {
    for (let l = 1; l < byLayer.length; l++) {
      byLayer[l].sort((a, b) => bary(a, deps.get(a)!) - bary(b, deps.get(b)!));
      byLayer[l].forEach((id, i) => order.set(id, i));
    }
    for (let l = byLayer.length - 2; l >= 0; l--) {
      byLayer[l].sort((a, b) => bary(a, dependents.get(a)!) - bary(b, dependents.get(b)!));
      byLayer[l].forEach((id, i) => order.set(id, i));
    }
  }

  // ---- 坐标：横向左→右，每列垂直居中于总高（workflow 同构）。
  const colH = (n: number) => (n === 0 ? 0 : n * DAG_NODE_H + (n - 1) * GAP_Y);
  const height = Math.max(...byLayer.map((r) => colH(r.length))) + PAD * 2;
  const layers = byLayer.length;
  const width = layers * DAG_NODE_W + (layers - 1) * GAP_X + PAD * 2;
  const nodes: DagNode[] = [];
  byLayer.forEach((row, l) => {
    const offset = PAD + (height - PAD * 2 - colH(row.length)) / 2;
    row.forEach((id, i) => {
      nodes.push({
        id,
        layer: l,
        order: i,
        x: PAD + l * (DAG_NODE_W + GAP_X),
        y: offset + i * (DAG_NODE_H + GAP_Y),
        deps: deps.get(id)!,
        dependents: dependents.get(id)!,
      });
    });
  });
  const edges: DagEdge[] = [];
  for (const it of items) {
    for (const d of deps.get(it.item_id)!) edges.push({ from: d, to: it.item_id });
  }
  return { nodes, edges, width, height, layers };
}

/** hover 高亮：节点的全部上游 + 下游（传递闭包），含自身。 */
export function dependencyChain(layout: DagLayout, id: string): Set<string> {
  const byId = new Map(layout.nodes.map((n) => [n.id, n]));
  const seen = new Set<string>([id]);
  const walk = (start: string, pick: (n: DagNode) => string[]) => {
    const stack = [start];
    while (stack.length) {
      const cur = stack.pop()!;
      for (const next of pick(byId.get(cur)!)) {
        if (!seen.has(next)) {
          seen.add(next);
          stack.push(next);
        }
      }
    }
  };
  walk(id, (n) => n.deps);
  walk(id, (n) => n.dependents);
  return seen;
}