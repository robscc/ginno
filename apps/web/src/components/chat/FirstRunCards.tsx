"use client";

// FirstRunCards — V2 P2-3 first-run onboarding.
//
// Shown on the home/new-session view only while the app is ready, no sessions
// exist yet and the starter has not been dismissed (`ginno-first-run-done` in
// localStorage, owned by ChatStream). Non-modal, three compact cards sitting
// above the normal composer. ChatStream supplies the handlers because they need
// the pending-mount state / composer ref / store actions.

import { FolderOpen, Loader2, MessageSquare, Workflow, X } from "lucide-react";
import { useTranslations } from "next-intl";

export function FirstRunCards({
  busy,
  onMountFolder,
  onAsk,
  onWorkflow,
  onDismiss,
}: {
  /** Folder picker / session creation in flight. */
  busy: boolean;
  onMountFolder: () => void;
  onAsk: () => void;
  onWorkflow: () => void;
  onDismiss: () => void;
}) {
  const tc = useTranslations("chat");

  const cards = [
    {
      key: "mount",
      icon: <FolderOpen className="h-4 w-4" />,
      title: tc("firstRun.mountTitle"),
      detail: tc("firstRun.mountDetail"),
      onClick: onMountFolder,
      accent: "text-green",
      spin: busy,
    },
    {
      key: "ask",
      icon: <MessageSquare className="h-4 w-4" />,
      title: tc("firstRun.askTitle"),
      detail: tc("firstRun.askDetail"),
      onClick: onAsk,
      accent: "text-violet",
      spin: false,
    },
    {
      key: "workflow",
      icon: <Workflow className="h-4 w-4" />,
      title: tc("firstRun.workflowTitle"),
      detail: tc("firstRun.workflowDetail"),
      onClick: onWorkflow,
      accent: "text-orange",
      spin: false,
    },
  ];

  return (
    <div className="w-full max-w-[760px]">
      <div className="mb-1.5 flex items-center justify-between">
        <span className="text-[10.5px] font-medium uppercase tracking-wide text-faint">
          {tc("firstRun.heading")}
        </span>
        <button
          onClick={onDismiss}
          title={tc("firstRun.dismiss")}
          aria-label={tc("firstRun.dismiss")}
          className="rounded p-0.5 text-faint transition-colors hover:text-txt"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>
      <div className="grid gap-2 sm:grid-cols-3">
        {cards.map((c) => (
          <button
            key={c.key}
            onClick={c.onClick}
            disabled={busy}
            className="flex flex-col items-start gap-1 rounded-xl border border-line bg-card/60 px-3 py-2.5 text-left transition-colors hover:border-line2 hover:bg-card disabled:opacity-60"
          >
            <span className={`flex items-center gap-1.5 ${c.accent}`}>
              {c.spin ? <Loader2 className="h-4 w-4 animate-spin" /> : c.icon}
              <span className="text-xs font-medium text-txt">{c.title}</span>
            </span>
            <span className="text-[11px] leading-snug text-faint">{c.detail}</span>
          </button>
        ))}
      </div>
    </div>
  );
}
