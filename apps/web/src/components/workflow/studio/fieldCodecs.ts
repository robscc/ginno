/**
 * NodeInspector 结构化字段（方案 D2）的 draft↔DSL 双向序列化——纯逻辑，
 * 无 React/DOM 依赖（对齐 vitest node 环境分工：渲染层由 packaged UI e2e 覆盖）。
 *
 * 三类结构化字段：
 * - python `entry`：下拉选项来自后端注册表白名单（api/workflows 的
 *   python-entries 只读端点）；离线/未连 sidecar 时降级手输，不阻塞编辑。
 * - python `args`：kv-map 行编辑（key/value，value 支持 `{{expr}}` 模板）。
 * - branch `cases`：cond-list 行编辑（`when` 表达式 → `then` 节点 id）。
 *
 * 「绝不静默丢字段」契约：
 * - 只有可无损表达的值（扁平标量 args / 纯 {when,then} cases）才允许结构化
 *   编辑；嵌套 args、带 transform 的 case 等 → `lossless:false` → 组件锁定
 *   结构化模式并提示切 JSON 高级模式，`applyDraftFields` 对锁定字段原样
 *   保留原值（不写、不删）。
 * - 未编辑过的标量值（number/bool/null）在行编辑中保留原类型——只有用户
 *   真正改过的行才退化为 string。
 */

/** kv-map 单行。value 保留原始标量类型（改过才是 string）。 */
export type KvRow = { key: string; value: string | number | boolean | null };
/** cond-list 单行。when 为空串写回时省略 key（`{then}` 与 `{when:"",then}` 等价且往返保真）。 */
export type CondRow = { when: string; then: string };

/** args 值域：扁平 map，值必须是标量（嵌套 → 不可表达 → 锁定）。 */
function isScalar(v: unknown): v is string | number | boolean | null {
  return v === null || ["string", "number", "boolean"].includes(typeof v);
}

export function argsToRows(v: unknown): { rows: KvRow[]; lossless: boolean } {
  if (v === undefined || v === null) return { rows: [], lossless: true };
  if (typeof v !== "object" || Array.isArray(v)) return { rows: [], lossless: false };
  const rows: KvRow[] = [];
  for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
    if (!isScalar(val)) return { rows: [], lossless: false };
    rows.push({ key: k, value: val });
  }
  return { rows, lossless: true };
}

export function rowsToArgs(rows: KvRow[]): Record<string, string | number | boolean | null> {
  const out: Record<string, string | number | boolean | null> = {};
  for (const r of rows) {
    // 空 key 行（刚点「+」还没填）也保留：draft 必须能无损往返半成品行，
    // 否则「添加一行」立即被序列化吞掉（行永远加不出来）。写回 DSL 时由
    // applyDraftFields 清理。
    out[r.key] = r.value;
  }
  return out;
}

/** case 只允许 {when?, then} 两个 key（transform/default_transform 等 → 锁定）。 */
const CASE_KEYS = new Set(["when", "then"]);

export function casesToRows(v: unknown): { rows: CondRow[]; lossless: boolean } {
  if (v === undefined || v === null) return { rows: [], lossless: true };
  if (!Array.isArray(v)) return { rows: [], lossless: false };
  const rows: CondRow[] = [];
  for (const c of v) {
    if (typeof c !== "object" || c === null || Array.isArray(c)) return { rows: [], lossless: false };
    const keys = Object.keys(c as Record<string, unknown>);
    if (keys.some((k) => !CASE_KEYS.has(k))) return { rows: [], lossless: false };
    const when = (c as Record<string, unknown>).when;
    const then = (c as Record<string, unknown>).then;
    if (when !== undefined && typeof when !== "string") return { rows: [], lossless: false };
    if (typeof then !== "string") return { rows: [], lossless: false };
    rows.push({ when: when ?? "", then });
  }
  return { rows, lossless: true };
}

export function rowsToCases(rows: CondRow[]): Array<{ when?: string; then: string }> {
  const out: Array<{ when?: string; then: string }> = [];
  for (const r of rows) {
    const when = r.when.trim();
    // 与 DSL 惯例同序（when 在前），且空 when 省略 key —— 往返字节级稳定。
    // then 空的半成品行同样保留（理由同 rowsToArgs），写回时清理。
    out.push(when ? { when, then: r.then } : { then: r.then });
  }
  return out;
}

/**
 * draft（JSON 字符串）↔ rows。draft 为空串 = 字段未设置。
 * 返回 null = draft 不是合法 JSON（用户在 JSON 高级模式改坏了，由 json 分支报错）。
 */
export function parseRows(draft: string): KvRow[] | null {
  if (!draft.trim()) return [];
  let v: unknown;
  try {
    v = JSON.parse(draft);
  } catch {
    return null;
  }
  const { rows, lossless } = argsToRows(v);
  return lossless ? rows : null;
}

export function parseCondRows(draft: string): CondRow[] | null {
  if (!draft.trim()) return [];
  let v: unknown;
  try {
    v = JSON.parse(draft);
  } catch {
    return null;
  }
  const { rows, lossless } = casesToRows(v);
  return lossless ? rows : null;
}

/** rows → draft JSON 字符串（与 initialText/pretty 的 2 空格缩进一致，避免假 dirty）。 */
export function rowsToDraft(rows: KvRow[]): string {
  const args = rowsToArgs(rows);
  return JSON.stringify(args, null, 2);
}

export function condRowsToDraft(rows: CondRow[]): string {
  return JSON.stringify(rowsToCases(rows), null, 2);
}

/**
 * `{{expr}}` 模板语法 regex 粗检（即时高亮用；语义校验仍由 dry-run 承接，
 * 方案 D2 ③——前端不重复造）。返回结构化码，文案走 i18n（wf.node.tplIssue.*）。
 */
export type TemplateIssue = { code: "unclosed" | "empty" | "stray" };

const PAIRED = /\{\{([^{}]*)\}\}/g;

export function templateIssues(s: string): TemplateIssue[] {
  if (!s.includes("{{") && !s.includes("}}")) return [];
  const issues: TemplateIssue[] = [];
  const rest = s.replace(PAIRED, (_m, inner: string) => {
    if (!inner.trim()) issues.push({ code: "empty" });
    return "\u0000"; // 已配对段挖掉，剩余的花括号都是问题
  });
  if (rest.includes("{{")) issues.push({ code: "unclosed" });
  else if (rest.includes("}")) issues.push({ code: "stray" });
  return issues;
}

/** DSL 可用 context keys（schema.properties ∪ initial，排序去重）——kv-map「插入引用」的数据源。 */
export function contextKeys(dsl: unknown): string[] {
  const ctx = (dsl as { context?: { schema?: { properties?: Record<string, unknown> }; initial?: Record<string, unknown> } } | undefined)
    ?.context;
  const keys = new Set<string>([
    ...Object.keys(ctx?.schema?.properties ?? {}),
    ...Object.keys(ctx?.initial ?? {}),
  ]);
  keys.delete("");
  return [...keys].sort();
}

/** 画布 DAG 节点 id 列表（cond-list 的 then、branch default、loop body 下拉数据源）。 */
export function nodeIds(dsl: unknown): string[] {
  const nodes = (dsl as { nodes?: Array<{ id?: unknown }> } | undefined)?.nodes;
  if (!Array.isArray(nodes)) return [];
  return nodes.map((n) => (typeof n?.id === "string" ? n.id : "")).filter(Boolean);
}

/** 字段的写回分派（与 NodeInspector 的 FieldKind 对齐；node = 节点 id 输入）。 */
export type FieldKindLite = "text" | "area" | "json" | "number" | "select" | "node" | "kv-map" | "cond-list";

/**
 * 把 draft 应用回节点（buildDraft 的纯逻辑核心——「绝不静默丢字段」在此钉死）：
 * - lockedKeys 中的字段（结构化无法表达的 DSL）**原样保留原值**，不写不删；
 * - draft 空 → 删 key；text/area/select/node 写字符串、number 转 Number、
 *   json/kv-map/cond-list JSON.parse 写回。
 * 返回新节点对象；不合法 JSON 由调用方（组件 parseField）先拦，这里只在
 * invariant 破裂时回退原值（防御式：宁可不动也不丢）。
 */
export function applyDraftFields(
  node: Record<string, unknown>,
  fields: Array<{ key: string; kind: FieldKindLite }>,
  draft: Record<string, string>,
  lockedKeys: ReadonlySet<string>,
): Record<string, unknown> {
  const next: Record<string, unknown> = { ...node };
  for (const f of fields) {
    if (lockedKeys.has(f.key)) continue; // 锁定字段：绝不触碰（不丢 transform/嵌套结构）
    const raw = draft[f.key] ?? "";
    if (!raw.trim()) {
      delete next[f.key];
      continue;
    }
    if (f.kind === "json" || f.kind === "kv-map" || f.kind === "cond-list") {
      try {
        let v: unknown = JSON.parse(raw);
        // kv-map/cond-list：draft 会保留半成品行（空 key / 未选 then）以便
        // 往返；写回 DSL 前清理，绝不把空壳行落库。
        if (f.kind === "kv-map" && v && typeof v === "object" && !Array.isArray(v)) {
          const cleaned: Record<string, unknown> = {};
          for (const [k, val] of Object.entries(v as Record<string, unknown>)) {
            if (k.trim()) cleaned[k] = val;
          }
          v = cleaned;
        }
        if (f.kind === "cond-list" && Array.isArray(v)) {
          v = v.filter((c) => typeof c === "object" && c !== null && typeof (c as Record<string, unknown>).then === "string" && String((c as Record<string, unknown>).then).trim() !== "");
        }
        next[f.key] = v;
      } catch {
        // 不可能路径（组件已校验），防御式保留原值。
      }
    } else if (f.kind === "number") {
      const n = Number(raw);
      if (Number.isFinite(n)) next[f.key] = n;
    } else {
      next[f.key] = raw;
    }
  }
  return next;
}
