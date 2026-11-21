/**
 * 设置页工作流「步骤编辑器」纯逻辑层（无 React/DOM，vitest node 环境可测）。
 *
 * 简单模式把若干有序步骤编译成真实可执行的 DSL：每个步骤一个 agent 节点
 * （"step" 是 runtime AgentNode 的注册别名，见 workflows/nodes/builtin.py），
 * 按数组顺序 s1→s2→…→sN 线性连边，entry 恒为首个节点。
 *
 * 反方向 stepsRepresentation() 只在「能无损往返」时才承认该 DSL 可以用步骤
 * 编辑器表达——任何编辑器表达不了的结构（自定义节点类型、分支连边、非默认
 * agent 绑定、额外字段）都让上层留在 JSON 模式，绝不默默降级丢内容。
 */

/** 步骤编辑器里的一行草稿。key 仅作 React 列表锚点，不进 DSL。 */
export interface StepDraft {
  key: string;
  /** 步骤名称 → 节点 title（运行面板的人读标签；可空）。 */
  title: string;
  /** 执行要求 → 节点 goal（agent 实际执行的指令）。 */
  goal: string;
}

export type StepIssueCode = "blank" | "no_goal";

export interface StepIssue {
  /** 1-based，UI 按它把错误定位到具体步骤行。 */
  index: number;
  code: StepIssueCode;
}

export type StepDslRepReason =
  | "not_object"
  | "extra_top"
  | "missing_nodes"
  | "bad_node"
  | "bad_edges"
  | "entry_mismatch";

export type StepsRepresentation =
  | { ok: true; steps: StepDraft[] }
  | { ok: false; reason: StepDslRepReason; /** reason==="bad_node" 时的 1-based 节点序号 */ index?: number };

/** 步骤编辑器目标形态的 DSL（与组件里 dslTemplate 的骨架同构）。 */
export interface StepsDsl {
  name?: string;
  description?: string;
  entry: string;
  nodes: Array<Record<string, unknown>>;
  edges: Array<{ from: string; to: string }>;
}

/** 简单编辑器只服务默认 dev agent 的线性步骤；其余结构一律留在 JSON 模式。 */
const SIMPLE_AGENT = "dev";
const TOP_KEYS = new Set(["name", "description", "entry", "nodes", "edges"]);
const NODE_KEYS = new Set(["id", "type", "agent", "goal", "title"]);
const EDGE_KEYS = new Set(["from", "to"]);

/**
 * 空 context（schema 无属性 + initial 无条目）与缺省 context 等价——
 * 后端 compile 对缺省 context 补的正是这个空对象（workflows/dsl.py compile），
 * 编辑器重建时省略它属于无损转换。带任何属性/初值的 context 一律不可表达。
 */
function isEmptyContext(c: unknown): boolean {
  if (typeof c !== "object" || c === null || Array.isArray(c)) return false;
  const o = c as Record<string, unknown>;
  for (const k of Object.keys(o)) {
    if (k !== "schema" && k !== "initial") return false;
  }
  const schema = o.schema;
  if (typeof schema !== "object" || schema === null || Array.isArray(schema)) return false;
  const s = schema as Record<string, unknown>;
  for (const k of Object.keys(s)) {
    if (k !== "type" && k !== "properties") return false;
  }
  if (s.type !== undefined && s.type !== "object") return false;
  const props = s.properties;
  if (props !== undefined && (typeof props !== "object" || props === null || Object.keys(props).length > 0)) {
    return false;
  }
  const initial = o.initial;
  if (initial === undefined) return true;
  return typeof initial === "object" && initial !== null && !Array.isArray(initial) && Object.keys(initial).length === 0;
}

function strField(v: unknown, key: string): string {
  if (typeof v !== "object" || v === null) return "";
  const x = (v as Record<string, unknown>)[key];
  return typeof x === "string" ? x : "";
}

export function makeStepDraft(key: string): StepDraft {
  return { key, title: "", goal: "" };
}

/**
 * 防御式逐行校验；返回问题列表（空 = 可以创建）。null 等无效行不抛错，
 * 按空步骤处理；空列表按「步骤 1 为空」提示（引导先添加步骤）。
 */
export function validateSteps(steps: unknown): StepIssue[] {
  const arr = Array.isArray(steps) ? steps : [];
  if (arr.length === 0) return [{ index: 1, code: "blank" }];
  const issues: StepIssue[] = [];
  arr.forEach((s, i) => {
    const title = strField(s, "title").trim();
    const goal = strField(s, "goal").trim();
    if (!title && !goal) issues.push({ index: i + 1, code: "blank" });
    else if (!goal) issues.push({ index: i + 1, code: "no_goal" });
  });
  return issues;
}

/**
 * 步骤草稿 → 可执行 DSL。步骤按位置得 id s1..sN 并顺序连边；
 * title/goal 为空则省略对应字段（backend 校验要求 goal-or-title，
 * 由 validateSteps 保证 goal 存在后才提交）。空列表返回 null。
 */
export function stepsToDsl(steps: unknown): StepsDsl | null {
  const arr = Array.isArray(steps) ? steps : [];
  if (arr.length === 0) return null;
  const nodes = arr.map((s, i) => {
    const node: Record<string, unknown> = { id: `s${i + 1}`, type: "step", agent: SIMPLE_AGENT };
    const title = strField(s, "title").trim();
    const goal = strField(s, "goal").trim();
    if (title) node.title = title;
    if (goal) node.goal = goal;
    return node;
  });
  return {
    entry: "s1",
    nodes,
    edges: nodes.slice(0, -1).map((n, i) => ({ from: String(n.id), to: `s${i + 2}` })),
  };
}

/**
 * 已解析的 DSL 能否无损映射为步骤编辑器草稿。判定从紧：
 * - 顶层键 ⊆ {name, description, entry, nodes, edges}，另接受「空 context」
 *   （schema 无属性 + initial 无条目；与缺省等价，见 isEmptyContext）；
 * - 每个节点 type ∈ {agent, step}、agent 恒为默认 dev、键 ⊆
 *   {id, type, agent, goal, title}、id 恒为位置 id s{n}、goal/title 至少一个非空；
 * - 边恰好是按数组顺序的线性链（1 节点允许无边）；
 * - entry 缺省视为 s1（编辑器重生成时会补上，语义不变），显式给出则必须为 s1。
 * 任何一条不满足都返回不 ok——上层保持 JSON 模式，不做有损转换。
 */
export function stepsRepresentation(dsl: unknown): StepsRepresentation {
  if (typeof dsl !== "object" || dsl === null || Array.isArray(dsl)) {
    return { ok: false, reason: "not_object" };
  }
  const d = dsl as Record<string, unknown>;
  for (const k of Object.keys(d)) {
    if (TOP_KEYS.has(k)) continue;
    if (k === "context" && isEmptyContext(d.context)) continue;
    return { ok: false, reason: "extra_top" };
  }
  const nodes = d.nodes;
  if (!Array.isArray(nodes) || nodes.length === 0) return { ok: false, reason: "missing_nodes" };
  for (let i = 0; i < nodes.length; i++) {
    const n = nodes[i];
    if (typeof n !== "object" || n === null || Array.isArray(n)) {
      return { ok: false, reason: "bad_node", index: i + 1 };
    }
    const o = n as Record<string, unknown>;
    // type 必须显式给出且为 agent/step——后端对缺 type 判 "unknown type None"
    // （workflows/dsl.py），编辑器不静默补全别人的半成品
    if ((o.type !== "agent" && o.type !== "step") || o.agent !== SIMPLE_AGENT) {
      return { ok: false, reason: "bad_node", index: i + 1 };
    }
    for (const k of Object.keys(o)) {
      if (!NODE_KEYS.has(k)) return { ok: false, reason: "bad_node", index: i + 1 };
    }
    if (o.id !== `s${i + 1}`) return { ok: false, reason: "bad_node", index: i + 1 };
    if (!strField(o, "goal").trim() && !strField(o, "title").trim()) {
      return { ok: false, reason: "bad_node", index: i + 1 };
    }
  }
  const expected = nodes.slice(0, -1).map((_, i) => `${i + 1}->${i + 2}`);
  const rawEdges = Array.isArray(d.edges) ? d.edges : [];
  if (rawEdges.length !== expected.length) return { ok: false, reason: "bad_edges" };
  const actual: string[] = [];
  for (const e of rawEdges) {
    if (typeof e !== "object" || e === null || Array.isArray(e)) {
      return { ok: false, reason: "bad_edges" };
    }
    const o = e as Record<string, unknown>;
    for (const k of Object.keys(o)) {
      if (!EDGE_KEYS.has(k)) return { ok: false, reason: "bad_edges" };
    }
    const from = strField(o, "from");
    const to = strField(o, "to");
    if (!from || !to) return { ok: false, reason: "bad_edges" };
    actual.push(`${from.slice(1)}->${to.slice(1)}`);
  }
  if ([...actual].sort().join() !== [...expected].sort().join()) {
    return { ok: false, reason: "bad_edges" };
  }
  const entry = d.entry === undefined ? "s1" : d.entry;
  if (entry !== "s1") return { ok: false, reason: "entry_mismatch" };
  return {
    ok: true,
    steps: nodes.map((n, i) => ({
      key: `s${i + 1}`,
      title: strField(n, "title").trim(),
      goal: strField(n, "goal").trim(),
    })),
  };
}
