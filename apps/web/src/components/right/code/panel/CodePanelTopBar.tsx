"use client";

/**
 * CodePanelTopBar — breadcrumb + the panel's tool toggles (hooks split 批次 3-3).
 *
 * Presentational: which toggles exist is decided by the props (browsing hides
 * them all; the HTML preview pair only when the active tab is renderable).
 * Owns no state — the panel wires every onClick to its hooks.
 */

import { Columns2, Eye, FileCode2, FolderTree, RotateCcw } from "lucide-react";
import { useTranslations } from "next-intl";
import { Breadcrumb, ToolButton } from "./codePanelBits";

export interface CodePanelTopBarProps {
  /** No tab open → the tree IS the panel, nothing to toggle. */
  browsing: boolean;
  treeVisible: boolean;
  crumbs: { label: string; path: string }[];
  onJumpDir: (path: string) => void;
  /** The active tab is an html/htm page → show the 预览/源码 pair. */
  htmlTab: boolean;
  htmlPreview: boolean;
  onToggleHtmlPreview: () => void;
  onReload: () => void;
  codeTreeOpen: boolean;
  onToggleTree: () => void;
  wide: boolean;
  sideActive: boolean;
  onToggleMode: () => void;
}

export function CodePanelTopBar(props: CodePanelTopBarProps) {
  const {
    browsing,
    treeVisible,
    crumbs,
    onJumpDir,
    htmlTab,
    htmlPreview,
    onToggleHtmlPreview,
    onReload,
    codeTreeOpen,
    onToggleTree,
    wide,
    sideActive,
    onToggleMode,
  } = props;
  const t = useTranslations("code");

  return (
    <div className="flex shrink-0 items-center gap-1 border-b border-line px-2 py-1">
      {treeVisible ? (
        <span className="min-w-0 flex-1 truncate text-[11px] text-faint">{t("panel.workspace")}</span>
      ) : (
        <Breadcrumb crumbs={crumbs} onJump={onJumpDir} />
      )}
      {/* While browsing there is nothing to toggle: the tree IS the panel. */}
      {browsing ? null : (
        <div className="ml-auto flex shrink-0 items-center gap-0.5">
          {/* HTML 页面的 预览/源码 切换（2026-10-21）：渲染页走沙箱 iframe
              (HtmlPreview)，源码走 Monaco。只在当前 tab 是 html/htm 时出现。 */}
          {htmlTab ? (
            <>
              <ToolButton
                title={t("preview.viewRendered")}
                active={htmlPreview}
                onClick={() => {
                  if (!htmlPreview) onToggleHtmlPreview();
                }}
              >
                <Eye className="h-3.5 w-3.5" />
              </ToolButton>
              <ToolButton
                title={t("preview.viewSource")}
                active={!htmlPreview}
                onClick={() => {
                  if (htmlPreview) onToggleHtmlPreview();
                }}
              >
                <FileCode2 className="h-3.5 w-3.5" />
              </ToolButton>
            </>
          ) : null}
          {/* 手动刷新当前文件（2026-10-13）：bash/脚本改文件不产生
              code.changed（只有文件工具有结果标记），S3 自动跟随收不到
              信号——显式重载兜底。脏/冲突 tab 先 confirm（issue #8）：
              确认丢弃才 reload，取消则缓冲区原样保留。 */}
          <ToolButton title={t("panel.reloadFile")} onClick={onReload}>
            <RotateCcw className="h-3.5 w-3.5" />
          </ToolButton>
          <ToolButton title={t("panel.treeToggle")} active={codeTreeOpen} onClick={onToggleTree}>
            <FolderTree className="h-3.5 w-3.5" />
          </ToolButton>
          {wide && (
            <ToolButton
              title={sideActive ? t("panel.layoutFile") : t("panel.layoutSide")}
              onClick={onToggleMode}
            >
              <Columns2 className="h-3.5 w-3.5" />
            </ToolButton>
          )}
        </div>
      )}
    </div>
  );
}
