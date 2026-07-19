"use client";

import Link from "next/link";
import { usePathname, useRouter } from "next/navigation";
import { ChevronDown, BookOpen, Settings as SettingsIcon, LogOut } from "lucide-react";
import { useGinno } from "@/lib/store";
import { agentHex } from "@/lib/theme";
import { Icon } from "@/components/icons";

function SectionHeader({ icon, label }: { icon: React.ReactNode; label: string }) {
  return (
    <div className="mb-1.5 flex items-center gap-1.5 px-2.5 text-[11px] font-semibold uppercase tracking-wider text-faint">
      {icon}
      <span>{label}</span>
      <ChevronDown className="ml-auto h-3.5 w-3.5" />
    </div>
  );
}

export function AppShell({ children }: { children: React.ReactNode }) {
  const g = useGinno();
  const pathname = usePathname();
  const router = useRouter();
  const active = g.sessions.find((s) => s.id === g.activeSessionId) ?? null;

  const onWorkspace = pathname === "/";
  const onSettings = pathname.startsWith("/settings");
  const onKb = pathname.startsWith("/kb");

  return (
    <div className="flex h-screen w-full overflow-hidden bg-base text-txt">
      {/* left nav */}
      <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-panel">
        {/* brand */}
        <div className="flex items-center gap-2.5 px-4 py-4">
          <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-violet/15 text-violet">
            <Icon name="star" className="h-4 w-4 fill-violet" />
          </div>
          <span className="text-[15px] font-semibold tracking-tight">GinnoWork</span>
        </div>

        <div className="flex-1 overflow-y-auto px-2.5 pb-2">
          {/* sessions */}
          <SectionHeader icon={<Icon name="message-square" className="h-3.5 w-3.5" />} label="Sessions" />
          <div className="mb-4 space-y-0.5">
            {g.sessions.length === 0 && (
              <div className="px-2.5 py-1 text-xs text-faint">No sessions yet</div>
            )}
            {g.sessions.map((s) => {
              const sel = onWorkspace && s.id === g.activeSessionId;
              return (
                <button
                  key={s.id}
                  onClick={() => {
                    g.setActiveSession(s.id);
                    if (!onWorkspace) router.push("/");
                  }}
                  className={`nav-item ${sel ? "nav-item-active" : ""}`}
                >
                  <Icon name={s.icon || "message-square"} className="h-4 w-4 text-indigo" />
                  <span className="truncate">{s.title || "Untitled"}</span>
                </button>
              );
            })}
          </div>

          {/* agents */}
          <SectionHeader icon={<Icon name="boxes" className="h-3.5 w-3.5" />} label="Agents" />
          <div className="space-y-0.5">
            {g.agents.map((a) => {
              const isActive =
                a.status === "running" || a.status === "active" || a.id === active?.agent_id;
              const hex = agentHex(a.color);
              return (
                <div key={a.id} className="nav-item cursor-default">
                  <span
                    className="flex h-5 w-5 items-center justify-center rounded-md"
                    style={{ background: hex + "22", color: hex }}
                  >
                    <Icon name={a.icon} className="h-3.5 w-3.5" />
                  </span>
                  <span className="truncate text-txt">{a.name}</span>
                  <span className="ml-auto flex items-center gap-1 text-[11px] text-faint">
                    <span
                      className="h-1.5 w-1.5 rounded-full"
                      style={{ background: isActive ? "#22c55e" : "#52525b" }}
                    />
                    {isActive ? "Active" : "Idle"}
                  </span>
                </div>
              );
            })}
          </div>
        </div>

        {/* footer nav */}
        <div className="border-t border-line px-2.5 py-3">
          <Link href="/kb" className={`nav-item ${onKb ? "nav-item-active" : ""}`}>
            <BookOpen className="h-4 w-4" />
            <span>Knowledge Base</span>
          </Link>
          <Link href="/settings/model-api" className={`nav-item ${onSettings ? "nav-item-active" : ""}`}>
            <SettingsIcon className="h-4 w-4" />
            <span>Settings</span>
          </Link>

          <div className="mt-2 flex items-center gap-2.5 rounded-lg px-2.5 py-2">
            <div className="flex h-8 w-8 items-center justify-center rounded-full bg-gradient-to-br from-indigo to-violet text-xs font-semibold text-white">
              DC
            </div>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-txt">David Chen</div>
              <div className="text-[11px] text-faint">Pro Plan</div>
            </div>
            <button className="text-faint hover:text-txt" title="Sign out">
              <LogOut className="h-4 w-4" />
            </button>
          </div>
          <div className="px-2.5 pt-1 text-[10px] text-faint">© 2025 GinnoWork Inc.</div>
        </div>
      </aside>

      {/* main */}
      <main className="flex min-w-0 flex-1">{children}</main>
    </div>
  );
}
