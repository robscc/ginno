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
    { tab: "external-agents" },
    { tab: "permissions" },
    { tab: "hooks" },
    { tab: "session-files" },
    { tab: "usage" },
    { tab: "general" },
    { tab: "notifications" },
    { tab: "floating" },
    // Must stay in sync with SettingsNav's SYSTEM group. A tab that is
    // navigable from the nav but missing here is NOT built by the static
    // export, so clicking it 404s — which reads as "the feature is not
    // implemented" even though SettingsView renders it.
    { tab: "tool-labels" },
  ];
}

export default function SettingsPage({ params }: { params: { tab?: string } }) {
  return <SettingsView tab={params.tab || "model-api"} />;
}
