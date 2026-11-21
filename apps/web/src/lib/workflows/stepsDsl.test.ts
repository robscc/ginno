/**
 * stepsDsl 纯逻辑单测：步骤草稿 ⇄ 可执行 DSL 的双向转换与「无损表达」判定。
 * node 环境无 DOM——组件渲染层由 packaged UI e2e 覆盖（vitest.config.ts 分工）。
 */
import { describe, expect, it } from "vitest";
import {
  makeStepDraft,
  stepsRepresentation,
  stepsToDsl,
  validateSteps,
  type StepDraft,
} from "./stepsDsl";

const draft = (title: string, goal: string): StepDraft => ({ key: "k", title, goal });

describe("validateSteps", () => {
  it("空列表按步骤 1 为空提示", () => {
    expect(validateSteps([])).toEqual([{ index: 1, code: "blank" }]);
    expect(validateSteps(null)).toEqual([{ index: 1, code: "blank" }]);
  });

  it("null/非对象行按空步骤处理，不抛错", () => {
    expect(validateSteps([null, undefined, 42])).toEqual([
      { index: 1, code: "blank" },
      { index: 2, code: "blank" },
      { index: 3, code: "blank" },
    ]);
  });

  it("blank（名称与要求全空）与 no_goal（缺执行要求）分别报出，定位到行", () => {
    expect(validateSteps([draft("收集资料", ""), draft("", "写报告"), draft("", "")])).toEqual([
      { index: 1, code: "no_goal" },
      { index: 3, code: "blank" },
    ]);
  });

  it("全空列表无问题", () => {
    expect(validateSteps([draft("a", "b"), draft(" c ", " d ")])).toEqual([]);
  });
});

describe("stepsToDsl", () => {
  it("生成线性链 DSL：位置 id、顺序连边、entry=首个节点", () => {
    const d = stepsToDsl([draft("第一步", "收集资料"), draft("第二步", "写报告")]);
    expect(d).toEqual({
      entry: "s1",
      nodes: [
        { id: "s1", type: "step", agent: "dev", title: "第一步", goal: "收集资料" },
        { id: "s2", type: "step", agent: "dev", title: "第二步", goal: "写报告" },
      ],
      edges: [{ from: "s1", to: "s2" }],
    });
  });

  it("名称为空时省略 title 字段", () => {
    const d = stepsToDsl([draft("", "只干活")]);
    expect(d?.nodes[0]).toEqual({ id: "s1", type: "step", agent: "dev", goal: "只干活" });
  });

  it("空 / 非数组输入返回 null，不抛错", () => {
    expect(stepsToDsl([])).toBeNull();
    expect(stepsToDsl(null)).toBeNull();
    expect(stepsToDsl([null])).toEqual({
      entry: "s1",
      nodes: [{ id: "s1", type: "step", agent: "dev" }],
      edges: [],
    });
  });

  it("三步链边数 = N-1 且顺序正确", () => {
    const d = stepsToDsl([draft("a", "a"), draft("b", "b"), draft("c", "c")]);
    expect(d?.edges).toEqual([
      { from: "s1", to: "s2" },
      { from: "s2", to: "s3" },
    ]);
  });
});

describe("stepsRepresentation：可表达判定", () => {
  it("stepsToDsl 的产物可无损往返", () => {
    const src = [draft("  收集 ", " 收集资料 "), draft("", "写报告")];
    const rep = stepsRepresentation(stepsToDsl(src));
    expect(rep.ok).toBe(true);
    if (!rep.ok) return;
    expect(rep.steps.map((s) => ({ title: s.title, goal: s.goal }))).toEqual([
      { title: "收集", goal: "收集资料" },
      { title: "", goal: "写报告" },
    ]);
    expect(stepsToDsl(rep.steps)).toEqual(stepsToDsl(src));
  });

  it("组件 dslTemplate 同构的单步骨架（含空 context）可表达", () => {
    const dsl = {
      name: "New Workflow",
      description: "",
      entry: "s1",
      context: { schema: { type: "object", properties: {} }, initial: {} },
      nodes: [{ id: "s1", type: "step", agent: "dev", goal: "hint" }],
      edges: [],
    };
    expect(stepsRepresentation(dsl).ok).toBe(true);
  });

  it("空 context 的变体（缺省 schema.type / 缺省 initial）也可表达", () => {
    expect(
      stepsRepresentation({
        context: { schema: { properties: {} } },
        nodes: [{ id: "s1", type: "step", agent: "dev", goal: "g" }],
      }).ok,
    ).toBe(true);
  });

  it("非空 context（带属性/初值/多余键/非对象）→ extra_top，不丢复杂草稿", () => {
    const base = { nodes: [{ id: "s1", type: "step", agent: "dev", goal: "g" }] };
    expect(
      stepsRepresentation({
        ...base,
        context: { schema: { type: "object", properties: { q: { type: "string" } } }, initial: {} },
      }),
    ).toEqual({ ok: false, reason: "extra_top" });
    expect(
      stepsRepresentation({
        ...base,
        context: { schema: { type: "object", properties: {} }, initial: { k: 1 } },
      }),
    ).toEqual({ ok: false, reason: "extra_top" });
    expect(stepsRepresentation({ ...base, context: 5 })).toEqual({ ok: false, reason: "extra_top" });
    expect(
      stepsRepresentation({
        ...base,
        context: { schema: { type: "object", properties: {} }, initial: {}, extra: 1 },
      }),
    ).toEqual({ ok: false, reason: "extra_top" });
  });

  it("1 节点 0 边可表达；entry 缺省视为 s1", () => {
    expect(
      stepsRepresentation({ nodes: [{ id: "s1", type: "agent", agent: "dev", title: "t" }] }).ok,
    ).toBe(true);
  });

  it("非对象 / 数组 / null → not_object", () => {
    expect(stepsRepresentation(null)).toEqual({ ok: false, reason: "not_object" });
    expect(stepsRepresentation([1])).toEqual({ ok: false, reason: "not_object" });
    expect(stepsRepresentation("x")).toEqual({ ok: false, reason: "not_object" });
  });

  it("顶层多余键 → extra_top，不丢复杂草稿", () => {
    const dsl = {
      schedule: { cron: "0 0 * * *" },
      entry: "s1",
      nodes: [{ id: "s1", type: "step", agent: "dev", goal: "g" }],
      edges: [],
    };
    expect(stepsRepresentation(dsl)).toEqual({ ok: false, reason: "extra_top" });
  });

  it("nodes 缺失 / 空数组 → missing_nodes", () => {
    expect(stepsRepresentation({ nodes: [] })).toEqual({ ok: false, reason: "missing_nodes" });
    expect(stepsRepresentation({})).toEqual({ ok: false, reason: "missing_nodes" });
  });

  it("非 step/agent 节点（python/loop/human）→ bad_node 定位到序号", () => {
    const dsl = {
      entry: "s1",
      nodes: [
        { id: "s1", type: "step", agent: "dev", goal: "g" },
        { id: "s2", type: "loop", over: "{{x}}" },
      ],
      edges: [{ from: "s1", to: "s2" }],
    };
    expect(stepsRepresentation(dsl)).toEqual({ ok: false, reason: "bad_node", index: 2 });
  });

  it("非默认 agent 绑定 / null 节点 / 位置 id 不符 / 空内容 / 额外字段 → bad_node", () => {
    const base = { id: "s1", type: "step", goal: "g" };
    expect(
      stepsRepresentation({
        nodes: [{ ...base, agent: "PM" }],
      }),
    ).toEqual({ ok: false, reason: "bad_node", index: 1 });
    expect(stepsRepresentation({ nodes: [null] })).toEqual({ ok: false, reason: "bad_node", index: 1 });
    expect(
      stepsRepresentation({ nodes: [{ ...base, agent: "dev", id: "fetch" }] }),
    ).toEqual({ ok: false, reason: "bad_node", index: 1 });
    expect(
      stepsRepresentation({ nodes: [{ id: "s1", type: "step", agent: "dev" }] }),
    ).toEqual({ ok: false, reason: "bad_node", index: 1 });
    expect(
      stepsRepresentation({
        nodes: [{ ...base, agent: "dev", skills: ["x"] }],
      }),
    ).toEqual({ ok: false, reason: "bad_node", index: 1 });
    // type 缺省的节点无法确认语义 → 不可表达（编辑器重建时会写 type:"step"）
    expect(
      stepsRepresentation({ nodes: [{ id: "s1", agent: "dev", goal: "g" }] }),
    ).toEqual({ ok: false, reason: "bad_node", index: 1 });
  });

  it("title/goal 为非字符串（number 等）按空处理，不抛错", () => {
    expect(validateSteps([{ title: 42, goal: "g" }])).toEqual([]);
    const d = stepsToDsl([{ title: 42, goal: "g" }]);
    expect(d?.nodes[0]).toEqual({ id: "s1", type: "step", agent: "dev", goal: "g" });
    // goal 为非字符串、title 是字符串 → 仅 title 合法，可表达
    expect(
      stepsRepresentation({ nodes: [{ id: "s1", type: "step", agent: "dev", title: "t", goal: 7 }] }).ok,
    ).toBe(true);
  });

  it("isEmptyContext 防御分支：畸形 schema/properties 不视为空 context", () => {
    const base = { nodes: [{ id: "s1", type: "step", agent: "dev", goal: "g" }] };
    expect(stepsRepresentation({ ...base, context: { schema: null } })).toEqual({
      ok: false,
      reason: "extra_top",
    });
    expect(stepsRepresentation({ ...base, context: { schema: ["x"] } })).toEqual({
      ok: false,
      reason: "extra_top",
    });
    expect(
      stepsRepresentation({ ...base, context: { schema: { type: "string", properties: {} } } }),
    ).toEqual({ ok: false, reason: "extra_top" });
    expect(
      stepsRepresentation({ ...base, context: { schema: { type: "object", properties: null } } }),
    ).toEqual({ ok: false, reason: "extra_top" });
  });

  it("缺边 / 多边 / 条件边（额外键）/ 乱序对 → bad_edges", () => {
    const n2 = [
      { id: "s1", type: "step", agent: "dev", goal: "a" },
      { id: "s2", type: "step", agent: "dev", goal: "b" },
    ];
    expect(stepsRepresentation({ nodes: n2, edges: [] })).toEqual({ ok: false, reason: "bad_edges" });
    expect(stepsRepresentation({ nodes: n2, edges: [{ from: "s1", to: "s2" }, { from: "s2", to: "s1" }] }))
      .toEqual({ ok: false, reason: "bad_edges" });
    expect(
      stepsRepresentation({
        nodes: n2,
        edges: [{ from: "s1", to: "s2", when: "{{x}}" }],
      }),
    ).toEqual({ ok: false, reason: "bad_edges" });
    expect(stepsRepresentation({ nodes: n2, edges: ["s1->s2"] })).toEqual({
      ok: false,
      reason: "bad_edges",
    });
    expect(stepsRepresentation({ nodes: n2, edges: [{ from: "s1", to: "" }] })).toEqual({
      ok: false,
      reason: "bad_edges",
    });
    // 方向反了的边不是这条链（s2→s1 ≠ s1→s2）→ 不可表达
    expect(stepsRepresentation({ nodes: n2, edges: [{ to: "s1", from: "s2" }] })).toEqual({
      ok: false,
      reason: "bad_edges",
    });
  });

  it("边数组顺序无关紧要：配对正确的乱序边仍可表达（编辑器按位置重建）", () => {
    const n3 = [
      { id: "s1", type: "step", agent: "dev", goal: "a" },
      { id: "s2", type: "step", agent: "dev", goal: "b" },
      { id: "s3", type: "step", agent: "dev", goal: "c" },
    ];
    expect(
      stepsRepresentation({
        nodes: n3,
        edges: [
          { from: "s2", to: "s3" },
          { from: "s1", to: "s2" },
        ],
      }).ok,
    ).toBe(true);
  });

  it("entry 非 s1 → entry_mismatch", () => {
    const dsl = {
      entry: "s2",
      nodes: [
        { id: "s1", type: "step", agent: "dev", goal: "a" },
        { id: "s2", type: "step", agent: "dev", goal: "b" },
      ],
      edges: [{ from: "s1", to: "s2" }],
    };
    expect(stepsRepresentation(dsl)).toEqual({ ok: false, reason: "entry_mismatch" });
  });
});

describe("makeStepDraft", () => {
  it("生成空草稿", () => {
    expect(makeStepDraft("k1")).toEqual({ key: "k1", title: "", goal: "" });
  });
});
