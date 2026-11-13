"use client";

// 兼容导出：组件已提升为公共 ui/Toggle（2026-11-13）。旧名保留一个 re-export，
// 调用点逐步迁移；MCP 三处（McpServerCard / McpDetailView / ToolPolicyPopover）
// 已直接改用 ui/Toggle。
export { Toggle as McpToggle } from "@/components/ui/Toggle";
