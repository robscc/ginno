"use client";

import { useRouter } from "next/navigation";
import { Cpu, Sparkles, Plug, Users, Workflow, SlidersHorizontal, Bell } from "lucide-react";

const MAIN = [
  { id: "model-api", label: "模型 API", icon: Cpu },
  { id: "skills", label: "Skills", icon: Sparkles },
  { id: "mcp", label: "MCP 工具", icon: Plug },
  { id: "agents", label: "Agent 管理", icon: Users },
  { id: "workflows", label: "Workflows", icon: Workflow },
];
const SYSTEM = [
  { id: "general", label: "通用设置", icon: SlidersHorizontal },
  { id: "notifications", label: "通知", icon: Bell },
];

export function SettingsNav({ active }: { active: string }) {
  const router = useRouter();
  const Item = ({ id, label, icon: Ic }: { id: string; label: string; icon: typeof Cpu }) => {
    const sel = active === id;
    return (
      <button
        onClick={() => router.push(`/settings/${id}`)}
        className={`flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors ${
          sel ? "bg-card2 text-txt" : "text-muted hover:bg-card hover:text-txt"
        }`}
      >
        <Ic className="h-4 w-4" style={sel ? { color: "#a78bfa" } : undefined} />
        {label}
      </button>
    );
  };
  return (
    <nav className="w-48 shrink-0 border-r border-line px-3 py-5">
      <div className="mb-3 px-3 text-sm font-semibold text-txt">Settings</div>
      <div className="space-y-0.5">
        {MAIN.map((m) => (
          <Item key={m.id} {...m} />
        ))}
      </div>
      <div className="mb-1.5 mt-4 px-3 text-[11px] font-semibold uppercase tracking-wider text-faint">
        系统
      </div>
      <div className="space-y-0.5">
        {SYSTEM.map((m) => (
          <Item key={m.id} {...m} />
        ))}
      </div>
    </nav>
  );
}
