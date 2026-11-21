/**
 * EnvVarsSettings 的纯逻辑层（node 环境可单测，无 React/DOM）。
 * 环境变量行的规范化、脏判定与保存载荷构造——与组件状态机解耦，
 * 渲染层交互（锁定/重试/保存失败保草稿）由组件持有，此处不涉及。
 */

export type Row = { key: string; value: string };

/** 服务端 env 值 → 行内字符串：字符串原样，其余 JSON 序列化（number/bool/null/对象）。 */
export const normValue = (v: unknown): string =>
  typeof v === "string" ? v : JSON.stringify(v);

/** settings.env（任意脏输入）→ 行数组；非对象（null/数组/标量）一律空表。 */
export function rowsFromEnv(raw: unknown): Row[] {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return [];
  return Object.entries(raw as Record<string, unknown>).map(([key, v]) => ({
    key,
    value: normValue(v),
  }));
}

/** 规范形：key 去首尾空白、丢弃空白 key 的行。脏判定与保存落盘同口径。 */
export function canonRows(rows: Row[]): Row[] {
  return rows
    .map((r) => ({ key: r.key.trim(), value: r.value }))
    .filter((r) => r.key !== "");
}

/** 逐行有序比较（key+value 全等）。 */
export function sameRows(a: Row[], b: Row[]): boolean {
  return (
    a.length === b.length &&
    a.every((r, i) => r.key === b[i].key && r.value === b[i].value)
  );
}

/** 草稿（规范形后）与基线是否有差异 → 能否保存。仅加一个空白行不算修改。 */
export const rowsChanged = (draft: Row[], baseline: Row[]): boolean =>
  !sameRows(canonRows(draft), baseline);

/**
 * 保存载荷：trim key、丢弃空 key 行、同名后者覆盖（与 dict 语义一致）。
 * rawPrev 是最近一次服务端原始 env——未触碰的行按原类型写回（保护已有
 * env 里的 number/boolean 不被静默字符串化），动过的行按字符串写入。
 */
export function envFromRows(
  rows: Row[],
  rawPrev: Record<string, unknown>,
): Record<string, unknown> {
  const env: Record<string, unknown> = {};
  for (const r of canonRows(rows)) {
    const prev = rawPrev[r.key];
    env[r.key] =
      prev !== undefined && normValue(prev) === r.value ? prev : r.value;
  }
  return env;
}
