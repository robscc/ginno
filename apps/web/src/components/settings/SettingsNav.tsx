"use client";

import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { Cpu, Sparkles, Plug, Users, Workflow, SlidersHorizontal, Bell, BookOpen, Globe, ShieldCheck, Webhook, FolderOpen, FolderInput, BarChart3, Tags, TrendingUp, Pin } from "lucide-react";

// label 为 settings.nav.items.* 的 catalog key（字面量联合，供 next-intl 类型检查）
type NavKey =
  | "modelApi" | "skills" | "mcp" | "agents" | "workflows" | "synthesis" | "knowledge"
  | "folders" | "webSearch" | "sessionFiles" | "usage" | "permissions" | "hooks"
  | "general" | "notifications" | "floating" | "toolLabels";
type Item = { id: string; label: NavKey; icon: typeof Cpu; color: string };

// label 存 catalog key（settings.nav.items.*），渲染时经 t() 翻译
const MAIN: Item[] = [
  { id: "model-api", label: "modelApi", icon: Cpu, color: "#a78bfa" },
  { id: "skills", label: "skills", icon: Sparkles, color: "#c084fc" },
  { id: "mcp", label: "mcp", icon: Plug, color: "#34d399" },
  { id: "agents", label: "agents", icon: Users, color: "#fb923c" },
  { id: "workflows", label: "workflows", icon: Workflow, color: "#4ade80" },
  { id: "synthesis-quality", label: "synthesis", icon: TrendingUp, color: "#a78bfa" },
  { id: "knowledge", label: "knowledge", icon: BookOpen, color: "#60a5fa" },
  { id: "folders", label: "folders", icon: FolderInput, color: "#34d399" },
  { id: "web", label: "webSearch", icon: Globe, color: "#3b82f6" },
  { id: "session-files", label: "sessionFiles", icon: FolderOpen, color: "#38bdf8" },
  { id: "usage", label: "usage", icon: BarChart3, color: "#2dd4bf" },
];
const SAFE: Item[] = [
  { id: "permissions", label: "permissions", icon: ShieldCheck, color: "#f87171" },
  { id: "hooks", label: "hooks", icon: Webhook, color: "#f59e0b" },
];
const SYSTEM: Item[] = [
  { id: "general", label: "general", icon: SlidersHorizontal, color: "#9ca3af" },
  { id: "notifications", label: "notifications", icon: Bell, color: "#fbbf24" },
  { id: "floating", label: "floating", icon: Pin, color: "#f472b6" },
  { id: "tool-labels", label: "toolLabels", icon: Tags, color: "#818cf8" },
];

export function SettingsNav({ active }: { active: string }) {
  const router = useRouter();
  const t = useTranslations("settings.nav");
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
        {t(`items.${label}`)}
      </button>
    );
  };
  return (
    <nav className="w-48 shrink-0 border-r border-line px-3 py-5">
      <div className="mb-3 px-3 text-sm font-semibold text-txt">{t("title")}</div>
      <div className="space-y-0.5">
        {MAIN.map((m) => (
          <Row key={m.id} {...m} />
        ))}
      </div>
      <div className="mb-1.5 mt-4 px-3 text-[11px] font-semibold uppercase tracking-wider text-faint">
        {t("groupSecurity")}
      </div>
      <div className="space-y-0.5">
        {SAFE.map((m) => (
          <Row key={m.id} {...m} />
        ))}
      </div>
      <div className="mb-1.5 mt-4 px-3 text-[11px] font-semibold uppercase tracking-wider text-faint">
        {t("groupSystem")}
      </div>
      <div className="space-y-0.5">
        {SYSTEM.map((m) => (
          <Row key={m.id} {...m} />
        ))}
      </div>
    </nav>
  );
}
