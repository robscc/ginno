"use client";

import { useRouter } from "next/navigation";
import { Cpu, Sparkles, Plug, Users, Workflow, SlidersHorizontal, Bell, BookOpen, Globe, ShieldCheck, Webhook, FolderOpen, BarChart3, Tags, TrendingUp } from "lucide-react";

type Item = { id: string; label: string; icon: typeof Cpu; color: string };

const MAIN: Item[] = [
  { id: "model-api", label: "模型 API", icon: Cpu, color: "#a78bfa" },
  { id: "skills", label: "Skills", icon: Sparkles, color: "#c084fc" },
  { id: "mcp", label: "MCP 工具", icon: Plug, color: "#34d399" },
  { id: "agents", label: "Agent 管理", icon: Users, color: "#fb923c" },
  { id: "workflows", label: "Workflows", icon: Workflow, color: "#4ade80" },
  { id: "synthesis-quality", label: "总结质量", icon: TrendingUp, color: "#a78bfa" },
  { id: "knowledge", label: "知识库", icon: BookOpen, color: "#60a5fa" },
  { id: "web", label: "Web 搜索", icon: Globe, color: "#3b82f6" },
  { id: "browser", label: "浏览器", icon: Globe, color: "#a78bfa" },
  { id: "session-files", label: "会话文件", icon: FolderOpen, color: "#38bdf8" },
  { id: "usage", label: "用量统计", icon: BarChart3, color: "#2dd4bf" },
];
const SAFE: Item[] = [
  { id: "permissions", label: "权限策略", icon: ShieldCheck, color: "#f87171" },
  { id: "hooks", label: "Hooks", icon: Webhook, color: "#f59e0b" },
];
const SYSTEM: Item[] = [
  { id: "general", label: "通用设置", icon: SlidersHorizontal, color: "#9ca3af" },
  { id: "notifications", label: "通知", icon: Bell, color: "#fbbf24" },
  { id: "tool-labels", label: "工具标签", icon: Tags, color: "#818cf8" },
];

export function SettingsNav({ active }: { active: string }) {
  const router = useRouter();
  const Row = ({ id, label, icon: Ic, color }: Item) => {
    const sel = active === id;
    return (
      <button
        onClick={() => router.push(`/settings/${id}`)}
        className="flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors"
        style={{
          background: sel ? color + "1f" : "transparent",
          color: sel ? "#fff" : "#9a9aa6",
        }}
      >
        <Ic className="h-4 w-4" style={{ color }} />
        {label}
      </button>
    );
  };
  return (
    <nav className="w-48 shrink-0 border-r border-line px-3 py-5">
      <div className="mb-3 px-3 text-sm font-semibold text-txt">Settings</div>
      <div className="space-y-0.5">
        {MAIN.map((m) => (
          <Row key={m.id} {...m} />
        ))}
      </div>
      <div className="mb-1.5 mt-4 px-3 text-[11px] font-semibold uppercase tracking-wider text-faint">
        安全
      </div>
      <div className="space-y-0.5">
        {SAFE.map((m) => (
          <Row key={m.id} {...m} />
        ))}
      </div>
      <div className="mb-1.5 mt-4 px-3 text-[11px] font-semibold uppercase tracking-wider text-faint">
        系统
      </div>
      <div className="space-y-0.5">
        {SYSTEM.map((m) => (
          <Row key={m.id} {...m} />
        ))}
      </div>
    </nav>
  );
}
