/**
 * Right-panel tab registry — the single source for the tabs' order, labels and
 * icons, shared by the tab strip (RightPanel), the collapsed dock (RightDock)
 * and Settings → 通用设置.
 *
 * It lives here rather than in `store.tsx` because the store needs the
 * ordering helpers and the components need the registry; importing the registry
 * from a component would drag React components into the store's module graph.
 * The `RightTab` import below is **type-only**, so it is erased at build time
 * and no runtime cycle exists even though `store.tsx` imports this module.
 */

import { Brain, Code2, FileBox, ListTodo, Sparkles, Zap, type LucideIcon } from "lucide-react";
import type { RightTab } from "./store";

/** 标签 key 的字面量联合（"tabs.<id>"），保住 useTranslations 的 key 类型检查。 */
export type RightTabLabelKey = `tabs.${RightTab}`;

export interface RightTabMeta {
  id: RightTab;
  /** 标签的 i18n key（right 域相对路径，如 "tabs.artifacts" → right.tabs.*）。
   *  注册表不持成品文案；渲染处用 useTranslations("right") 取 key 翻译。 */
  labelKey: RightTabLabelKey;
  icon: LucideIcon;
}

/** Default order. A newly added tab goes at the END so it never displaces the
 *  established ones (the same rule the tab strip has always followed). */
export const RIGHT_TABS: RightTabMeta[] = [
  { id: "artifacts", labelKey: "tabs.artifacts", icon: FileBox },
  { id: "todo", labelKey: "tabs.todo", icon: ListTodo },
  { id: "workflow", labelKey: "tabs.workflow", icon: Zap },
  { id: "memory", labelKey: "tabs.memory", icon: Brain },
  { id: "synthesis", labelKey: "tabs.synthesis", icon: Sparkles },
  { id: "code", labelKey: "tabs.code", icon: Code2 },
];

export const RIGHT_TAB_BY_ID = Object.fromEntries(
  RIGHT_TABS.map((t) => [t.id, t]),
) as Record<RightTab, RightTabMeta>;

export const ALL_RIGHT_TAB_IDS: RightTab[] = RIGHT_TABS.map((t) => t.id);

function asIdArray(v: unknown): string[] {
  return Array.isArray(v) ? v.filter((x): x is string => typeof x === "string") : [];
}

/**
 * The full tab order (hidden ones included), made safe against a stale stored
 * preference:
 *
 *  - an id this build no longer knows is **dropped** (a removed tab must not
 *    linger in the list);
 *  - a tab that exists in code but is missing from the stored order is
 *    **appended** — otherwise a newly shipped panel would stay invisible until
 *    the user thought to press 恢复默认.
 */
export function canonicalTabOrder(order?: unknown): RightTab[] {
  const known = new Set<string>(ALL_RIGHT_TAB_IDS);
  const seen = new Set<string>();
  const out: RightTab[] = [];
  for (const id of asIdArray(order)) {
    if (known.has(id) && !seen.has(id)) {
      seen.add(id);
      out.push(id as RightTab);
    }
  }
  for (const id of ALL_RIGHT_TAB_IDS) if (!seen.has(id)) out.push(id);
  return out;
}

/**
 * The tabs to actually render, in the user's order.
 *
 * Never returns empty: an empty strip leaves the panel with nothing to click
 * (⌘\ toggles a panel whose only affordance would be gone), so the last
 * remaining visible tab is kept.
 */
export function visibleTabOrder(order?: unknown, hidden?: unknown): RightTab[] {
  const hid = new Set(asIdArray(hidden));
  const full = canonicalTabOrder(order);
  const visible = full.filter((id) => !hid.has(id));
  return visible.length ? visible : [full[0]];
}