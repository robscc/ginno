"use client";

/**
 * CodeEditorColumn — the editor half of the code panel (hooks split 批次 3-3):
 * the open-file tab strip (EditorTabs), the S2 conflict bar, and the content
 * area (CodeEditorContent). The panel hides the whole column while BROWSING.
 *
 * Presentational except for the tab-data assembly (dirty/conflict/agent mark):
 * that needs the store's change map, which the panel passes down as `marks`.
 */

import { AlertTriangle, Columns2, RotateCcw, Save } from "lucide-react";
import { useTranslations } from "next-intl";
import { EditorTabs, type EditorTab } from "../EditorTabs";
import { BarButton } from "./codePanelBits";
import { CodeEditorContent } from "./CodeEditorContent";
import type { Jump, ReadState, TabMeta, InlineDiffState } from "./panelShared";

export interface CodeEditorColumnProps {
  tabs: EditorTab[];
  activeKey: string | null;
  onSelect: (key: string) => void;
  onClose: (key: string) => void;
  // ---- conflict bar (S2) ----
  conflict: { version: string } | null;
  showDiff: boolean;
  onToggleDiff: () => void;
  onReload: () => void;
  onForceOverwrite: () => void;
  // ---- content ----
  sessionId: string;
  activeTab: EditorTab | null;
  activeRead: ReadState | undefined;
  activeMeta: TabMeta | undefined;
  jump: Jump | null;
  inlineDiff: InlineDiffState | null;
  htmlPreview: boolean;
  onExitInlineDiff: () => void;
  onDirtyChange: (key: string, dirty: boolean) => void;
  onSave: (key: string, text: string) => void;
  onChangeText: (key: string, text: string) => void;
}

export function CodeEditorColumn(props: CodeEditorColumnProps) {
  const {
    tabs,
    activeKey,
    onSelect,
    onClose,
    conflict,
    showDiff,
    onToggleDiff,
    onReload,
    onForceOverwrite,
    ...content
  } = props;
  const t = useTranslations("code");

  return (
    <>
      <EditorTabs tabs={tabs} activeKey={activeKey} onSelect={onSelect} onClose={onClose} />

      {/* Conflict bar (S2). A refused save is ANNOUNCED and resolved by a
          human — the panel never overwrites the disk copy on its own
          (design §6 "公告而不覆盖"). */}
      {conflict ? (
        <div className="flex shrink-0 flex-wrap items-center gap-x-2 gap-y-1 border-b border-line bg-base px-3 py-1.5 text-[11px]">
          <AlertTriangle size={13} className="shrink-0 text-yellow" />
          <span className="text-txt">{t("conflict.title")}</span>
          <span className="text-faint">
            {t("conflict.diskVersion", { version: conflict.version })}
          </span>
          <div className="ml-auto flex shrink-0 items-center gap-1">
            <BarButton onClick={onToggleDiff} icon={<Columns2 size={12} />}>
              {showDiff ? t("conflict.hideDiff") : t("conflict.viewDiff")}
            </BarButton>
            <BarButton onClick={onReload} icon={<RotateCcw size={12} />} title={t("conflict.reloadTitle")}>
              {t("conflict.reload")}
            </BarButton>
            <BarButton
              onClick={onForceOverwrite}
              icon={<Save size={12} />}
              title={t("conflict.forceOverwriteTitle")}
            >
              {t("conflict.forceOverwrite")}
            </BarButton>
          </div>
        </div>
      ) : null}

      <div className="min-h-0 flex-1">
        <CodeEditorContent
          sessionId={content.sessionId}
          activeTab={content.activeTab}
          activeRead={content.activeRead}
          activeMeta={content.activeMeta}
          activeKey={activeKey}
          jump={content.jump}
          inlineDiff={content.inlineDiff}
          showDiff={showDiff}
          htmlPreview={content.htmlPreview}
          onExitInlineDiff={content.onExitInlineDiff}
          onDirtyChange={content.onDirtyChange}
          onSave={content.onSave}
          onChangeText={content.onChangeText}
        />
      </div>
    </>
  );
}
