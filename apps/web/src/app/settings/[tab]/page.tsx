import { SettingsView } from "@/components/settings/SettingsView";

export function generateStaticParams() {
  return [
    { tab: "model-api" },
    { tab: "skills" },
    { tab: "mcp" },
    { tab: "agents" },
    { tab: "workflows" },
    { tab: "synthesis-quality" },
    { tab: "knowledge" },
    { tab: "folders" },
    { tab: "web" },
    { tab: "browser" },
    { tab: "permissions" },
    { tab: "hooks" },
    { tab: "session-files" },
    { tab: "usage" },
    { tab: "general" },
    { tab: "notifications" },
  ];
}

export default function SettingsPage({ params }: { params: { tab?: string } }) {
  return <SettingsView tab={params.tab || "model-api"} />;
}
