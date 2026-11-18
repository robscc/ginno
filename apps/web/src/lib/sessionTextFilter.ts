/* ── 会话/产物就地过滤的文本匹配（2026-11-18 用户旅程走查 P1-3）──────────
 * 打分制：contains(3) > 拼音首字母串 contains(2) > fuzzy 子序列(1)。
 * 与 @ 菜单（commandMenu.ts）同款规则提炼成公共件——那边历史实现未导出，
 * 这里成为唯一实现，commandMenu 后续迁移引用。
 * 拼音仅取首字母（多音字用常用读法，不做整词拼音——成本大收益小）；
 * 子序列匹配允许跳跃但要求顺序（Levenshtein 太重，subsequence 足够）。 */
import { pinyin } from "pinyin-pro";

const pinyinCache = new Map<string, string>();

export function pinyinInitials(s: string): string {
  let v = pinyinCache.get(s);
  if (v === undefined) {
    try {
      // 数组含空格项（原文空白），过滤后拼接——「会话 热更新」→ "hhrgx"。
      v = pinyin(s, { pattern: "first", toneType: "none", type: "array" })
        .filter((x) => x.trim())
        .join("");
    } catch {
      v = "";
    }
    if (pinyinCache.size > 2000) pinyinCache.clear(); // 防长会话膨胀
    pinyinCache.set(s, v);
  }
  return v;
}

export function isSubsequence(hay: string, needle: string): boolean {
  let i = 0;
  for (const ch of hay) {
    if (ch === needle[i]) i++;
    if (i === needle.length) return true;
  }
  return false;
}

/** 打分：0 = 不匹配；3 = 原文 contains；2 = 拼音首字母 contains；1 = 子序列。 */
export function textMatchScore(title: string, query: string): number {
  const q = query.trim().toLowerCase();
  if (!q) return 3; // 空查询 = 全匹配（不过滤）
  const hay = title.toLowerCase();
  if (hay.includes(q)) return 3;
  if (pinyinInitials(title).toLowerCase().includes(q)) return 2;
  if (isSubsequence(hay, q)) return 1;
  return 0;
}

/** 通用就地过滤：按打分 >0 保留并按分数稳定排序（contains 优先）。 */
export function filterByText<T>(
  items: T[],
  query: string,
  titleOf: (item: T) => string,
): T[] {
  const q = query.trim();
  if (!q) return items;
  return items
    .map((it) => ({ it, score: textMatchScore(titleOf(it), q) }))
    .filter((x) => x.score > 0)
    .sort((a, b) => b.score - a.score)
    .map((x) => x.it);
}
