import { SettingsView } from "@/components/settings/SettingsView";

export function generateStaticParams() {
  return [
    { tab: "model-api" },
    { tab: "skills" },
    { tab: "mcp" },
    { tab: "agents" },
    { tab: "workflows" },
    { tab: "general" },
    { tab: "notifications" },
  ];
}

export default function SettingsPage({ params }: { params: { tab?: string } }) {
  return <SettingsView tab={params.tab || "model-api"} />;
}
