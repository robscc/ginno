/**
 * Session label/icon 纯逻辑（session-label-icon-design.md §3/§4）——
 * 过滤/折叠都是纯函数，vitest 直接覆盖（不渲染 DOM）。
 */

/** `#release` → label 过滤；`#a b` 混合输入 MVP 不支持，# 后整串当作 label。 */
export function parseLabelQuery(q: string): { isLabel: boolean; label: string } {
  const trimmed = q.trimStart();
  if (!trimmed.startsWith("#")) return { isLabel: false, label: "" };
  // 单独一个 # 视为空 label（isLabel=true 但匹配空串=全命中已打标？）——
  // 约定：# 后为空时不做 label 过滤（退回普通搜索语义由调用方决定）。
  const label = trimmed.slice(1).trim();
  return { isLabel: label.length > 0, label };
}

/** label 子串匹配（忽略大小写），命中任一 label 即通过。 */
export function sessionMatchesLabel(labels: string[] | undefined, label: string): boolean {
  if (!label) return false;
  const needle = label.toLowerCase();
  return (labels ?? []).some((l) => l.toLowerCase().includes(needle));
}

/** 侧栏行 chip 折叠：最多显示 2 个，余下折叠为 +N。 */
export function foldLabels(labels: string[] | undefined): {
  shown: string[];
  extra: number;
} {
  const list = labels ?? [];
  return { shown: list.slice(0, 2), extra: Math.max(0, list.length - 2) };
}
