"use client";

/**
 * Open-file tab bar for the code panel (design §3.4 编辑器).
 *
 * S2: one flat row of tabs with select / close and horizontal overflow
 * scrolling (middle-click closes too), plus the dirty dot and conflict badge
 * driven by the optional `dirty` / `conflict` flags on each tab. Kept
 * presentational: the open-tab list, the active tab and both flags live in
 * CodePanel, which wires them to the store and to `MonacoEditor`.
 *
 * The tab key is `<rootId>\0<path>`; use `codeTabKey` to build it so the
 * caller and this component never drift.
 */

import { AlertTriangle, FileText, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

export interface EditorTab {
  /** Root the file belongs to — part of the identity, not just a label. */
  rootId: string;
  /** Root-relative path. */
  path: string;
  /** Display label; defaults to the path's basename. */
  label?: string;
  /** Has unsaved edits in the buffer. */
  dirty?: boolean;
  /** Save was rejected: the file changed on disk since it was read. */
  conflict?: boolean;
}

export interface EditorTabsProps {
  tabs: EditorTab[];
  /** Key of the active tab (`codeTabKey`), or null when none. */
  activeKey: string | null;
  onSelect(key: string): void;
  onClose(key: string): void;
  className?: string;
}

/** Stable identity for a tab / open file: root id + relative path. */
export function codeTabKey(tab: { rootId: string; path: string }): string {
  return `${tab.rootId}\u0000${tab.path}`;
}

/** Label shown on a tab: the explicit label, else the basename. */
function tabLabel(tab: EditorTab): string {
  if (tab.label) return tab.label;
  const parts = tab.path.split("/");
  return parts[parts.length - 1] || tab.path || "/";
}

export function EditorTabs({ tabs, activeKey, onSelect, onClose, className }: EditorTabsProps) {
  const t = useTranslations("code.tabs");

  /** Tooltip for a tab: the path, plus why it is marked (closing loses edits). */
  const tabTip = (tab: EditorTab): string => {
    const notes: string[] = [];
    // Conflict first: it outranks dirty when both are set.
    if (tab.conflict) notes.push(t("conflictTip"));
    if (tab.dirty) notes.push(t("dirtyTip", { name: tabLabel(tab) }));
    if (notes.length === 0) return tab.path;
    return `${tab.path} (${notes.join("; ")})`;
  };

  return (
    <div
      role="tablist"
      aria-label={t("openFiles")}
      className={cn(
        "flex h-8 shrink-0 items-stretch overflow-x-auto border-b border-line bg-panel",
        className,
      )}
    >
      {tabs.map((tab) => {
        const key = codeTabKey(tab);
        const active = key === activeKey;
        return (
          <div
            key={key}
            role="tab"
            aria-selected={active}
            title={tabTip(tab)}
            onClick={() => onSelect(key)}
            onAuxClick={(e) => {
              // Middle-click closes, matching editor/browser tab behaviour.
              if (e.button === 1) {
                e.preventDefault();
                onClose(key);
              }
            }}
            className={cn(
              "group flex h-full max-w-[200px] shrink-0 cursor-pointer items-center gap-1.5 border-r border-line px-2.5 text-xs transition-colors",
              active ? "bg-base text-txt" : "text-muted hover:bg-card hover:text-txt",
            )}
          >
            <FileText size={12} className="shrink-0 opacity-70" />
            <span className="truncate">{tabLabel(tab)}</span>
            {/* Conflict badge outranks the dirty dot: both set → conflict only. */}
            {tab.conflict ? (
              <span
                role="img"
                title={t("conflictTip")}
                aria-label={t("conflictTip")}
                className="flex shrink-0 items-center text-yellow"
              >
                <AlertTriangle size={12} />
              </span>
            ) : tab.dirty ? (
              <span
                role="img"
                title={t("dirtyTip", { name: tabLabel(tab) })}
                aria-label={t("dirtyTip", { name: tabLabel(tab) })}
                className="flex shrink-0 items-center text-muted"
              >
                <span className="block h-1.5 w-1.5 rounded-full bg-current" />
              </span>
            ) : null}
            <button
              type="button"
              title={t("close")}
              aria-label={t("closeNamed", { name: tabLabel(tab) })}
              onClick={(e) => {
                e.stopPropagation();
                onClose(key);
              }}
              className={cn(
                "-mr-1 flex h-4 w-4 shrink-0 items-center justify-center rounded hover:bg-card2",
                active ? "opacity-70 hover:opacity-100" : "opacity-0 group-hover:opacity-80",
              )}
            >
              <X size={12} />
            </button>
          </div>
        );
      })}
    </div>
  );
}