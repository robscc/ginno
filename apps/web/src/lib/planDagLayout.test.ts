import { describe, expect, it } from "vitest";
import {
  DAG_NODE_H,
  DAG_NODE_W,
  dependencyChain,
  layoutPlanDag,
} from "./planDagLayout";

const item = (id: string, deps: string[] = []) => ({ item_id: id, depends_on: deps });

describe("layoutPlanDag（横向左→右，workflow 同构）", () => {
  it("空清单 → 空布局", () => {
    const l = layoutPlanDag([]);
    expect(l.nodes).toEqual([]);
    expect(l.edges).toEqual([]);
    expect(l.width).toBe(0);
    expect(l.height).toBe(0);
  });

  it("单节点 → 第 0 列", () => {
    const l = layoutPlanDag([item("a")]);
    expect(l.layers).toBe(1);
    expect(l.nodes[0]).toMatchObject({ id: "a", layer: 0, order: 0 });
    expect(l.edges).toEqual([]);
  });

  it("线性链 a→b→c → 三列水平递增，单节点列垂直居中（y 相同）", () => {
    const l = layoutPlanDag([item("c", ["b"]), item("a"), item("b", ["a"])]);
    expect(l.layers).toBe(3);
    const byId = new Map(l.nodes.map((n) => [n.id, n]));
    expect(byId.get("a")!.layer).toBe(0);
    expect(byId.get("b")!.layer).toBe(1);
    expect(byId.get("c")!.layer).toBe(2);
    expect(byId.get("a")!.x).toBeLessThan(byId.get("b")!.x);
    expect(byId.get("b")!.x).toBeLessThan(byId.get("c")!.x);
    expect(byId.get("a")!.y).toBe(byId.get("c")!.y);
    // 宽度 = 3 列 × 节点宽 + 2 列距 + 边距（150×3 + 56×2 + 16×2）
    expect(l.width).toBe(3 * DAG_NODE_W + 2 * 56 + 32);
    expect(l.edges).toEqual(
      expect.arrayContaining([
        { from: "a", to: "b" },
        { from: "b", to: "c" },
      ]),
    );
  });

  it("菱形 a→{b,c}→d → 中间列两节点同 x 不同 y，不重叠", () => {
    const l = layoutPlanDag([item("a"), item("b", ["a"]), item("c", ["a"]), item("d", ["b", "c"])]);
    expect(l.layers).toBe(3);
    const byId = new Map(l.nodes.map((n) => [n.id, n]));
    expect(byId.get("b")!.layer).toBe(1);
    expect(byId.get("c")!.layer).toBe(1);
    expect(byId.get("b")!.x).toBe(byId.get("c")!.x);
    expect(byId.get("b")!.y).not.toBe(byId.get("c")!.y);
    expect(Math.abs(byId.get("b")!.y - byId.get("c")!.y)).toBeGreaterThanOrEqual(DAG_NODE_H);
    expect(l.edges).toHaveLength(4);
  });

  it("无依赖并行节点 → 同列垂直堆叠", () => {
    const l = layoutPlanDag([item("a"), item("b"), item("c")]);
    expect(l.layers).toBe(1);
    expect(l.nodes.every((n) => n.layer === 0 && n.x === l.nodes[0].x)).toBe(true);
    expect(l.nodes.map((n) => n.order)).toEqual([0, 1, 2]);
    expect(l.nodes[0].y).toBeLessThan(l.nodes[1].y);
  });

  it("最长路径分层：d 依赖 a 和 c（a→b→c）→ d 在第 3 列而非第 1 列", () => {
    const l = layoutPlanDag([item("a"), item("b", ["a"]), item("c", ["b"]), item("d", ["a", "c"])]);
    const byId = new Map(l.nodes.map((n) => [n.id, n]));
    expect(byId.get("d")!.layer).toBe(3);
  });

  it("悬空依赖（不存在的 id）与自依赖 → 过滤不崩", () => {
    const l = layoutPlanDag([item("a", ["ghost", "a"]), item("b", ["a"])]);
    expect(l.nodes).toHaveLength(2);
    expect(l.edges).toEqual([{ from: "a", to: "b" }]);
    expect(l.nodes.find((n) => n.id === "a")!.deps).toEqual([]);
  });

  it("环防御：a↔b 不崩、不丢节点、坐标有限", () => {
    const l = layoutPlanDag([item("a", ["b"]), item("b", ["a"])]);
    expect(l.nodes).toHaveLength(2);
    expect(l.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y))).toBe(true);
  });

  it("窄列垂直居中于总高（单节点列上下边距相等）", () => {
    const l = layoutPlanDag([item("a"), item("b"), item("c", ["a", "b"])]);
    const byId = new Map(l.nodes.map((n) => [n.id, n]));
    const c = byId.get("c")!;
    const top = c.y;
    const bottom = l.height - (c.y + DAG_NODE_H);
    expect(Math.abs(top - bottom)).toBeLessThan(1);
  });

  it("barycenter 减交叉：左列 [a,b]，右列 x 依赖 b、y 依赖 a → y 排在 x 上方", () => {
    const l = layoutPlanDag([item("a"), item("b"), item("x", ["b"]), item("y", ["a"])]);
    const byId = new Map(l.nodes.map((n) => [n.id, n]));
    expect(byId.get("y")!.order).toBeLessThan(byId.get("x")!.order);
    expect(byId.get("y")!.y).toBeLessThan(byId.get("x")!.y);
  });

  it("坐标尺寸自洽：height ≥ 最高列节点数 × 节点高", () => {
    const l = layoutPlanDag([item("a"), item("b", ["a"]), item("c", ["a"]), item("d", ["a"])]);
    expect(l.height).toBeGreaterThanOrEqual(3 * DAG_NODE_H);
    expect(l.width).toBeGreaterThanOrEqual(2 * DAG_NODE_W);
  });
});

describe("dependencyChain", () => {
  it("菱形中间节点 → 闭包含全部上下游", () => {
    const l = layoutPlanDag([item("a"), item("b", ["a"]), item("c", ["a"]), item("d", ["b", "c"])]);
    expect(dependencyChain(l, "b")).toEqual(new Set(["a", "b", "d"]));
    expect(dependencyChain(l, "a")).toEqual(new Set(["a", "b", "c", "d"]));
    expect(dependencyChain(l, "d")).toEqual(new Set(["a", "b", "c", "d"]));
  });

  it("孤立节点 → 仅自身", () => {
    const l = layoutPlanDag([item("a"), item("b")]);
    expect(dependencyChain(l, "a")).toEqual(new Set(["a"]));
  });
});