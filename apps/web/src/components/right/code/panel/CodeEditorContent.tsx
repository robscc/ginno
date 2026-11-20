"use client";

/**
 * CodeEditorContent — the ACTIVE tab's content area switch (hooks split 批次 3-3).
 *
 * Priority order (each replaces the editor entirely):
 *   1. inline diff overlay (agent rewrote a CLEAN tab, code-panel-diff-design §B)
 *   2. conflict diff (「看差异」 on a refused save)
 *   3. image preview (binary → /api/code/raw)
 *   4. rendered HTML (sandboxed iframe; reads disk bytes, not the buffer)
 *   5. Monaco — otherwise; `key` carries the model revision so a reload
 *      rebuilds the model, a plain save never does.
 * Plus the loading / error / no-file empty states.
 */

import { AlertTriangle } from "lucide-react";
import { useTranslations } from "next-intl";
import { codeFileUrl, codeRawUrl } from "@/lib/runtime";
import { MonacoEditor } from "../MonacoEditor";
import { ImagePreview } from "../ImagePreview";
import { HtmlPreview } from "../HtmlPreview";
import { ConflictDiff } from "../ConflictDiff";
import { InlineDiffOverlay } from "../InlineDiffOverlay";
import { EmptyHint } from "./codePanelBits";
import { codeTabKey, type EditorTab } from "../EditorTabs";
import { PROJECT_SLUG, type Jump, type ReadState, type TabMeta, type InlineDiffState } from "./panelShared";

export interface CodeEditorContentProps {
  sessionId: string;
  activeTab: EditorTab | null;
  activeRead: ReadState | undefined;
  activeMeta: TabMeta | undefined;
  activeKey: string | null;
  jump: Jump | null;
  inlineDiff: InlineDiffState | null;
  showDiff: boolean;
  htmlPreview: boolean;
  // ---- wired to the hooks by the panel ----
  onExitInlineDiff: () => void;
  onDirtyChange: (key: string, dirty: boolean) => void;
  onSave: (key: string, text: string) => void;
  onChangeText: (key: string, text: string) => void;
}

export function CodeEditorContent(props: CodeEditorContentProps) {
  const {
    sessionId,
    activeTab,
    activeRead,
    activeMeta,
    activeKey,
    jump,
    inlineDiff,
    showDiff,
    htmlPreview,
    onExitInlineDiff,
    onDirtyChange,
    onSave,
    onChangeText,
  } = props;
  const t = useTranslations("code");

  return activeTab && activeRead?.status === "ok" && activeRead.data ? (
    inlineDiff && inlineDiff.key === codeTabKey(activeTab) ? (
      /* Inline diff (code-panel-diff-design.md §B): session baseline
         vs current disk content for a CLEAN tab the agent rewrote.
         Takes priority — it replaces the plain editor entirely until
         the user exits (or edits, which clears the state above). */
      <InlineDiffOverlay
        path={activeTab.path}
        base={inlineDiff.base}
        current={inlineDiff.current}
        baseVersion={inlineDiff.baseVersion}
        currentVersion={inlineDiff.currentVersion}
        degraded={inlineDiff.degraded}
        onExit={onExitInlineDiff}
        className="h-full"
      />
    ) : showDiff && activeMeta?.conflict ? (
      <ConflictDiff
        path={activeTab.path}
        mine={activeMeta.conflict.mine}
        /* openDiff only opens the diff once the disk copy is loaded,
           so this is a real string whenever the diff is reachable. */
        theirs={activeMeta.conflict.diskText ?? ""}
        className="h-full"
      />
    ) : activeRead.data.readonly_reason === "image" ? (
      /* Images are binary, so there is no text for Monaco to show.
         The runtime flags them readonly_reason="image" and the bytes
         stream from /api/code/raw (see ImagePreview). */
      <ImagePreview
        path={activeTab.path}
        url={codeRawUrl(
            PROJECT_SLUG,
            sessionId,
            activeTab.rootId,
            activeTab.path,
            activeRead.data.version,
          )}
        className="h-full"
      />
    ) : htmlPreview ? (
      /* Rendered page for html/htm (HtmlPreview for the sandboxing
         story). Reads the bytes ON DISK — an unsaved buffer is not
         shown until saved; the version rides in the URL, so a save
         (or an agent rewrite followed by a reload) refreshes it. */
      <HtmlPreview
        path={activeTab.path}
        url={codeFileUrl(
            PROJECT_SLUG,
            sessionId,
            activeTab.rootId,
            activeTab.path,
            activeRead.data.version,
          )}
        className="h-full"
      />
    ) : (
      <MonacoEditor
        /* `key` carries the model revision. Bumping it is the only way
           to make Monaco adopt externally-changed text: its model is
           rebuilt only when the URI (rootId + path) changes, so a
           reload would otherwise refetch and keep showing the stale
           buffer. Never bumped on a plain save. */
        key={`${activeTab.rootId}/${activeTab.path}#${activeMeta?.rev ?? 0}`}
        rootId={activeTab.rootId}
        path={activeTab.path}
        text={activeRead.data.text}
        language={activeRead.data.language}
        encoding={activeRead.data.encoding}
        editable={activeRead.data.editable}
        readonlyReason={activeRead.data.readonly_reason}
        markSavedNonce={activeMeta?.savedNonce ?? 0}
        onDirtyChange={(dirty) => {
          if (activeKey) onDirtyChange(activeKey, dirty);
        }}
        onSave={(text) => {
          if (activeKey) onSave(activeKey, text);
        }}
        onChangeText={(text) => {
          if (activeKey) onChangeText(activeKey, text);
        }}
        revealLine={
          jump && jump.rootId === activeTab.rootId && jump.path === activeTab.path
            ? jump.line
            : null
        }
        revealNonce={
          jump && jump.rootId === activeTab.rootId && jump.path === activeTab.path
            ? jump.nonce
            : 0
        }
        className="h-full"
      />
    )
  ) : activeTab && activeRead?.status === "error" ? (
    <EmptyHint icon={<AlertTriangle size={18} />} text={activeRead.error ?? t("notice.readFailed")} />
  ) : activeTab ? (
    <EmptyHint text={t("panel.reading")} />
  ) : (
    <EmptyHint text={t("panel.emptyTitle")} hint={t("panel.emptyHint")} />
  );
}
