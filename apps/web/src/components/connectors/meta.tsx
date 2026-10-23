"use client";

/* 连接器卡片的共享展示逻辑（状态徽标/图标/来源）。
 * 内置连接器与 Claude Mod 在统一页面上共用同一套视觉语言。 */

import {
  Cable,
  CheckCircle2,
  CircleDashed,
  CircleOff,
  Globe,
  Loader2,
  Monitor,
  Puzzle,
  RotateCw,
  TriangleAlert,
  XCircle,
} from "lucide-react";
import type { ConnectorInfo } from "@/lib/runtime";

export type ConnStatusKey =
  | "connected"
  | "disconnected"
  | "not_installed"
  | "installing"
  | "error"
  | "disabled"
  | "restarting";

export const STATUS_META: Record<
  string,
  { statusKey: ConnStatusKey; cls: string; Icon: typeof CheckCircle2 }
> = {
  connected: { statusKey: "connected", cls: "text-green-600 dark:text-green-400", Icon: CheckCircle2 },
  disconnected: { statusKey: "disconnected", cls: "text-yellow-600 dark:text-yellow-400", Icon: TriangleAlert },
  not_installed: { statusKey: "not_installed", cls: "text-faint", Icon: CircleDashed },
  installing: { statusKey: "installing", cls: "text-blue-600 dark:text-blue-400", Icon: Loader2 },
  error: { statusKey: "error", cls: "text-red-600 dark:text-red-400", Icon: XCircle },
  disabled: { statusKey: "disabled", cls: "text-faint", Icon: CircleOff },
  restarting: { statusKey: "restarting", cls: "text-yellow-600 dark:text-yellow-400", Icon: RotateCw },
};

/** 徽标优先反映 enabled 开关：后端从不把 status 置为 disabled，
 *  停用只落在 settings 的 connectors.<id>.enabled。 */
export function statusMeta(c: ConnectorInfo) {
  return !c.enabled
    ? STATUS_META.disabled
    : STATUS_META[c.status] || STATUS_META.not_installed;
}

export function connectorKind(c: ConnectorInfo): "builtin" | "mod" {
  return c.kind === "mod" ? "mod" : "builtin";
}

export function connectorIcon(c: ConnectorInfo) {
  if (c.kind === "mod") return Puzzle;
  if (c.icon === "globe") return Globe;
  if (c.icon === "monitor") return Monitor;
  return Cable;
}

/** 来源（global/project/市场名）——extra 里的 UTF-8 字符串，缺失则不展示。 */
export function sourceOf(c: ConnectorInfo): string | null {
  const extra = c.extra || {};
  for (const key of ["source", "origin", "marketplace"]) {
    const v = extra[key];
    if (typeof v === "string" && v.trim()) return v;
  }
  return null;
}

/** 卡片副行（版本由卡片单独渲染，避免重复）：browserType · statusDetail。 */
export function metaLine(c: ConnectorInfo): string {
  const bits: string[] = [];
  const extra = c.extra || {};
  if (typeof extra.browserType === "string") bits.push(String(extra.browserType));
  // 停用时不拼 statusDetail——「已停用」徽标下再显示「Connected」会误导
  // （底层 relay 可能仍连着，但用户视角此连接器已不可用）。
  if (c.enabled && c.status === "connected" && c.statusDetail) bits.push(c.statusDetail);
  return bits.join(" · ");
}