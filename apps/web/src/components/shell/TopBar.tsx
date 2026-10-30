"use client";

import { useState } from "react";
import { ArrowDownUp, Download, Loader2, MoreVertical } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { pushNotice } from "@/lib/notice";
import { useGinno } from "@/lib/store";
import type { AgentConfig, SessionMeta, SessionUsage } from "@/lib/types";
import { agentHex } from "@/lib/theme";
import { AgentIcon } from "@/components/icons";
import { GoalChip } from "./GoalChip";
import { ContextFoldersChip } from "./ContextFoldersChip";
import { LinkedSessionsChip } from "./LinkedSessionsChip";
import { TopBarFuncButton } from "./TopBarFuncButton";

/** Filename from `Content-Disposition` (RFC 5987 `filename*=` first, then the
 *  plain form). The project-export endpoint returns FileResponse, whose header
 *  carries the slug + timestamp; fall back to a stable name if it is absent. */
function exportFilename(disposition: string | null, fallback: string): string {
  if (!disposition) return fallback;
  const star = /filename\*=UTF-8''([^;]+)/i.exec(disposition);
  if (star) {
    try {
      return decodeURIComponent(star[1].trim());
    } catch {
      return star[1].trim();
    }
  }
  const plain = /filename="?([^";]+)"?/i.exec(disposition);
  return plain ? plain[1].trim() : fallback;
}

/** Hand a fetched blob to the browser as a download (same behaviour as
 *  following a download URL, but lets us POST first). */
function downloadBlob(blob: Blob, filename: string) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  // Revoke late: some webviews start the write only after the click returns.
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

/** Click-to-copy session uuid chip — same shape as the per-turn trace chip on
 * chat bubbles. The full id is what you grep the sidecar logs for
 * (`session=...`); we show a short prefix to keep the bar tidy. */
function SessionIdChip({ sessionId }: { sessionId?: string }) {
  const tr = useTranslations("shell");
  const [copied, setCopied] = useState(false);
  if (!sessionId) return null;
  const short = sessionId.slice(0, 8);
  return (
    <button
      onClick={async () => {
        try {
          await navigator.clipboard.writeText(sessionId);
          setCopied(true);
          setTimeout(() => setCopied(false), 1200);
        } catch {
          /* clipboard unavailable */
        }
      }}
      title={tr("topbar.idChipTitle", { id: sessionId })}
      className="shrink-0 whitespace-nowrap rounded border border-line2 px-1 py-px font-mono text-[9px] text-faint transition-colors hover:border-violet/50 hover:text-violet"
    >
      {copied ? tr("topbar.copied") : `#${short}`}
    </button>
  );
}

export function TopBar({
  session,
  agent,
  running,
  usage,
}: {
  session: SessionMeta | null;
  agent: AgentConfig | null;
  running: boolean;
  usage?: SessionUsage | null;
}) {
  const g = useGinno();
  const tr = useTranslations("shell");
  const [menu, setMenu] = useState(false);
  const [exportMenu, setExportMenu] = useState(false);
  const [exporting, setExporting] = useState(false);
  const hex = agentHex(agent?.color);

  function exportSessionMarkdown() {
    if (!session) return;
    setExportMenu(false);
    // Direct navigation: the sidecar answers with Content-Disposition and the
    // webview hands the Markdown file to the OS.
    window.location.href = api.exportSessionUrl(session.id);
  }

  /** V2 P1-3: POST then download the returned zip as a blob. Calling
   *  `api.exportProject` would run the response through `json()` (it is a
   *  file), so the fetch is done here — the endpoint also keeps a copy under
   *  ~/.ginno/index/exports/. */
  async function exportProjectZip() {
    if (exporting) return;
    setExporting(true);
    try {
      const res = await fetch(`${api.BASE}/export/project`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_slug: "default" }),
      });
      if (!res.ok) throw new Error(`export failed: ${res.status}`);
      const blob = await res.blob();
      if (!blob.size) throw new Error("empty export");
      downloadBlob(blob, exportFilename(res.headers.get("Content-Disposition"), "ginno-export.zip"));
      pushNotice("success", tr("topbar.exportProjectDone"));
      setExportMenu(false);
    } catch {
      pushNotice("error", tr("topbar.exportFailed"));
    } finally {
      setExporting(false);
    }
  }

  return (
    <header className="flex min-h-14 min-w-0 shrink-0 select-none flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-line px-5 py-1.5">
      <div className="flex min-w-[min(100%,12rem)] flex-1 flex-wrap items-center gap-x-2.5 gap-y-1">
        {/* 身份区(topbar-unification.md §2.4):agent 纯图标、标题左侧,名称进
            tooltip——侧栏行同款「图标+颜色」识别,让出的宽度全给标题。 */}
        {agent && (
          <span
            className="flex h-[18px] w-[18px] shrink-0 items-center justify-center"
            style={{ color: hex }}
            title={agent.name}
          >
            <AgentIcon name={agent.icon} className="h-[18px] w-[18px]" />
          </span>
        )}
        <h1 className="min-w-[5rem] max-w-full truncate text-[15px] font-semibold tracking-tight text-txt" title={session?.title || tr("topbar.newSession")}>
          {session?.title || tr("topbar.newSession")}
        </h1>
        <SessionIdChip sessionId={session?.id} />
      </div>

      {/* 功能按钮组(§2.3):五个同宽 32px,徽标 + hover 数据 + click 面板;
          home(无 session)整组隐藏(维持现状)。 */}
      {session && (
        <div className="flex shrink-0 items-center gap-0.5">
          {/* ⏺ 状态位:无面板,click 无行为(决策点 A)。语义色走 --st-*
              (主题感知:暗色青/亮色深青,不用绿色——白底洗白)。 */}
          <TopBarFuncButton
            title={running ? tr("topbar.running") : tr("topbar.idle")}
            live={running}
            active={running ? "var(--st-running)" : undefined}
          >
            <span
              className="h-2 w-2 rounded-full"
              style={{ background: running ? "var(--st-running)" : "var(--st-skipped)" }}
            />
          </TopBarFuncButton>

          <GoalChip sessionId={session.id} />

          <ContextFoldersChip session={session} />

          <LinkedSessionsChip session={session} />

          {/* 📊 Stats:纯 hover 数据(决策点 D,会话累计口径)。 */}
          {usage && usage.calls > 0 && (
            <TopBarFuncButton
              title={tr("topbar.usageTitle", {
                input: usage.input_tokens,
                cache: usage.cache_read_tokens,
                output: usage.output_tokens,
                calls: usage.calls,
              })}
            >
              <ArrowDownUp className="h-4 w-4" />
            </TopBarFuncButton>
          )}
        </div>
      )}

      <div className="ml-auto flex shrink-0 items-center gap-2">
        <div className="relative">
          <button
            onClick={() => setExportMenu((m) => !m)}
            aria-label={tr("topbar.exportMenu")}
            aria-haspopup="menu"
            aria-expanded={exportMenu}
            disabled={exporting}
            title={tr("topbar.exportMenu")}
            className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt disabled:cursor-wait disabled:opacity-70"
          >
            {exporting ? <Loader2 className="h-4 w-4 animate-spin" /> : <Download className="h-4 w-4" />}
          </button>
          {exportMenu && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setExportMenu(false)} />
              <div
                role="menu"
                className="absolute right-0 z-50 mt-1 w-60 overflow-hidden rounded-lg border border-line bg-card py-1 text-sm shadow-xl"
              >
                <button
                  role="menuitem"
                  disabled={!session || exporting}
                  onClick={exportSessionMarkdown}
                  title={session ? undefined : tr("topbar.exportNoSession")}
                  className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt disabled:cursor-not-allowed disabled:text-faint disabled:hover:bg-transparent"
                >
                  {tr("topbar.exportSession")}
                </button>
                <button
                  role="menuitem"
                  disabled={exporting}
                  onClick={() => void exportProjectZip()}
                  className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt disabled:cursor-wait disabled:text-faint disabled:hover:bg-transparent"
                >
                  {exporting && <Loader2 className="h-3.5 w-3.5 animate-spin" />}
                  {exporting ? tr("topbar.exporting") : tr("topbar.exportProject")}
                </button>
                <div className="border-t border-line px-3 pb-1 pt-1.5 text-[10.5px] leading-snug text-faint">
                  {tr("topbar.exportProjectHint")}
                </div>
              </div>
            </>
          )}
        </div>
        <div className="relative">
          <button
            onClick={() => setMenu((m) => !m)}
            aria-label={tr("topbar.actions")}
            aria-haspopup="menu"
            aria-expanded={menu}
            className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt"
          >
            <MoreVertical className="h-4 w-4" />
          </button>
          {menu && (
            <>
              <div className="fixed inset-0 z-40" onClick={() => setMenu(false)} />
              <div
                role="menu"
                className="absolute right-0 z-50 mt-1 w-40 overflow-hidden rounded-lg border border-line bg-card py-1 text-sm shadow-xl"
              >
                <button
                  role="menuitem"
                  onClick={async () => {
                    setMenu(false);
                    if (!session) return;
                    const name = window.prompt(tr("topbar.renamePrompt"), session.title || "");
                    if (name != null && name.trim()) {
                      await api.patchSession(session.id, { title: name.trim() });
                      await g.reloadSessions();
                    }
                  }}
                  className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                >
                  {tr("session.rename")}
                </button>
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenu(false);
                    if (session) void navigator.clipboard?.writeText(session.id);
                  }}
                  className="block w-full px-3 py-1.5 text-left text-muted hover:bg-card2 hover:text-txt"
                >
                  {tr("topbar.copyId")}
                </button>
              </div>
            </>
          )}
        </div>
      </div>
    </header>
  );
}
