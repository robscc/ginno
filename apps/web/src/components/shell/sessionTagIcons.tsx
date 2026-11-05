"use client";

// Session label/icon（session-label-icon-design.md §2）：8 个受控枚举的
// lucide 映射 + 语义色（颜色对齐 design-mocks/session-label-icon-preview.html）。
// 纯映射与渲染，不含状态。

import {
  Bug,
  FileText,
  MessageCircle,
  Rocket,
  Search,
  Star,
  TriangleAlert,
  Wrench,
  type LucideIcon,
} from "lucide-react";

export interface SessionTagIconDef {
  id: string;
  Icon: LucideIcon;
  color: string;
}

/** 顺序即打标面板 8 宫格的顺序。id 固定，与后端 SESSION_TAG_ICONS 枚举一致。 */
export const SESSION_TAG_ICONS: SessionTagIconDef[] = [
  { id: "wrench", Icon: Wrench, color: "#9aa3b2" }, // 灰 构建/工程
  { id: "bug", Icon: Bug, color: "#f87171" }, // 红 排障
  { id: "doc", Icon: FileText, color: "#60a5fa" }, // 蓝 文档/方案
  { id: "release", Icon: Rocket, color: "#4ade80" }, // 绿 发版
  { id: "research", Icon: Search, color: "#a78bfa" }, // 紫 调研
  { id: "chat", Icon: MessageCircle, color: "#9aa3b2" }, // 灰 闲聊/问答
  { id: "warn", Icon: TriangleAlert, color: "#fbbf24" }, // 琥珀 风险/待办
  { id: "star", Icon: Star, color: "#f59e0b" }, // 金黄 收藏
];

const BY_ID = new Map(SESSION_TAG_ICONS.map((d) => [d.id, d]));

/** 侧栏行 14px 语义色图标；未知 id/无 icon 渲染 null（不占位）。 */
export function SessionTagIcon({
  id,
  size = 14,
  className,
}: {
  id?: string | null;
  size?: number;
  className?: string;
}) {
  if (!id) return null;
  const def = BY_ID.get(id);
  if (!def) return null;
  return (
    <def.Icon
      style={{ color: def.color, width: size, height: size }}
      className={`shrink-0 ${className ?? ""}`}
      aria-hidden
    />
  );
}
