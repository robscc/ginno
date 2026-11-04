"use client";

/**
 * Inline diff overlay for a CLEAN tab the agent just rewrote
 * (docs/code-panel-diff-design.md §B / §4.1): session baseline vs current disk
 * content, rendered by Monaco's DiffEditor in INLINE (unified) mode.
 *
 * This is the third Monaco surface in the panel, so it reuses the same shared
 * theme registry / worker environment as MonacoEditor and ConflictDiff — one
 * palette, one worker bootstrap. It takes plain strings and knows nothing
 * about the code API.
 *
 * Large diffs (>INLINE_DIFF_MAX_LINES lines or >INLINE_DIFF_MAX_BYTES bytes of
 * combined content) are NOT rendered automatically: the banner shows a
 * 「查看差异」 button and the editor mounts only after the user asks — the same
 * lazy-load degradation the design specifies.
 */

import { useEffect, useRef, useState } from "react";
import { AlignJustify, Columns2, FileDiff, GitCompare, X } from "lucide-react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";
import { languageForPath } from "./language";
import {
  appThemeName,
  ensureThemes,
  installMonacoEnvironment,
  type Monaco,
} from "./MonacoEditor";

type DiffEditor = import("monaco-editor").editor.IStandaloneDiffEditor;

/** version 形如 "{mtime}:{inode}"（files/baselines.py）。整串太长太丑
 *  （2026-11-04 用户反馈），只展示 mtime 段转成的 HH:MM:SS；解析不了
 *  就退回前 8 位。 */
function shortVersion(v?: string | null): string {
  if (!v) return "—";
  const mt = Number(v.split(":")[0]);
  if (Number.isFinite(mt) && mt > 0) {
    const d = new Date(mt * 1000);
    const p = (n: number) => String(n).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  return v.slice(0, 8);
}
type TextModel = import("monaco-editor").editor.ITextModel;

/** Diff-size caps (design §4.1: >4000 行或内容 >1MB 降级懒加载). */
const INLINE_DIFF_MAX_LINES = 4000;
const INLINE_DIFF_MAX_BYTES = 1024 * 1024;

export interface InlineDiffOverlayProps {
  path: string;
  /** Session baseline (pre-first-agent-write content). Empty when the file is
   *  session-added — the whole file then shows as additions. */
  base: string;
  /** Current on-disk content. */
  current: string;
  /** Version tokens for the banner (v_base → v_current); may be empty. */
  baseVersion: string;
  currentVersion: string;
  /** True when the runtime could not deliver full baseline content. */
  degraded?: boolean;
  onExit(): void;
  className?: string;
}

/** Instance counter for the model URIs — same pit as ConflictDiff: Monaco
 *  throws on createModel when a URI is already taken. */
let instanceSeq = 0;

function diffUri(monaco: Monaco, uid: number, path: string, side: "base" | "current") {
  return monaco.Uri.from({
    scheme: "inmemory",
    authority: `ginno-inline-diff-${uid}`,
    path: `/${path}/${side}`,
  });
}

function basename(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

/** Too big to render without asking: >cap lines or >cap bytes combined. */
export function isLargeInlineDiff(base: string, current: string): boolean {
  const bytes = base.length + current.length;
  if (bytes > INLINE_DIFF_MAX_BYTES) return true;
  const lines =
    base.split("\n").length + current.split("\n").length;
  return lines > INLINE_DIFF_MAX_LINES;
}

export function InlineDiffOverlay({
  path,
  base,
  current,
  baseVersion,
  currentVersion,
  degraded,
  onExit,
  className,
}: InlineDiffOverlayProps): JSX.Element {
  const t = useTranslations("code.inlineDiff");
  const hostRef = useRef<HTMLDivElement | null>(null);
  const diffRef = useRef<DiffEditor | null>(null);
  const baseModelRef = useRef<TextModel | null>(null);
  const currentModelRef = useRef<TextModel | null>(null);
  const monacoRef = useRef<Monaco | null>(null);
  const latestRef = useRef({ base, current });
  const sideBySideRef = useRef(false);

  // Lazy gate: a large diff shows the banner with a 「查看差异」 button and no
  // editor until the user asks for it (design §4.1 diffLarge).
  const large = isLargeInlineDiff(base, current);
  const [expanded, setExpanded] = useState(!large);
  const [sideBySide, setSideBySide] = useState(false);
  const [uid] = useState<number>(() => ++instanceSeq);

  // Keep text refs current before the awaited import resolves, and live-update
  // mounted models (both sides read-only, so setValue costs no undo stack).
  useEffect(() => {
    latestRef.current = { base, current };
    const baseModel = baseModelRef.current;
    const currentModel = currentModelRef.current;
    if (baseModel && baseModel.getValue() !== base) baseModel.setValue(base);
    if (currentModel && currentModel.getValue() !== current) currentModel.setValue(current);
  }, [base, current]);

  useEffect(() => {
    sideBySideRef.current = sideBySide;
    diffRef.current?.updateOptions({ renderSideBySide: sideBySide });
  }, [sideBySide]);

  // Mount-scoped: editor + models + theme observer, all disposed together.
  // Only mounts when `expanded` flips true (the lazy-load path).
  useEffect(() => {
    if (!expanded) return;
    let disposed = false;
    const observer = new MutationObserver(() => {
      monacoRef.current?.editor.setTheme(appThemeName());
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

    void (async () => {
      installMonacoEnvironment();
      const monaco = await import("monaco-editor");
      if (disposed || !hostRef.current) return;
      monacoRef.current = monaco;
      ensureThemes(monaco);
      monaco.editor.setTheme(appThemeName());

      const language = languageForPath(path);
      const original = monaco.editor.createModel(
        latestRef.current.base,
        language,
        diffUri(monaco, uid, path, "base"),
      );
      const modified = monaco.editor.createModel(
        latestRef.current.current,
        language,
        diffUri(monaco, uid, path, "current"),
      );
      baseModelRef.current = original;
      currentModelRef.current = modified;

      diffRef.current = monaco.editor.createDiffEditor(hostRef.current, {
        readOnly: true,
        originalEditable: false,
        renderSideBySide: sideBySideRef.current,
        useInlineViewWhenSpaceIsLimited: false,
        ignoreTrimWhitespace: false,
        automaticLayout: true,
        minimap: { enabled: false },
        renderOverviewRuler: false,
        stickyScroll: { enabled: false },
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 12,
        lineNumbersMinChars: 3,
        // inline diff 默认把「原始+修改」两个行号挤在一列（相同行显示 374374，
        // 2026-11-04 用户反馈）。回调只给修改侧单号，消除重复；
        // 删除行的原始行号仍由 diff 装饰（红色背景）表达位置。
        lineNumbers: (n) => String(n),
        scrollBeyondLastLine: false,
        smoothScrolling: true,
        padding: { top: 8, bottom: 8 },
        scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8, useShadows: false },
      });
      diffRef.current.setModel({ original, modified });
    })();

    return () => {
      disposed = true;
      observer.disconnect();
      diffRef.current?.dispose();
      baseModelRef.current?.dispose();
      currentModelRef.current?.dispose();
      diffRef.current = null;
      baseModelRef.current = null;
      currentModelRef.current = null;
    };
  }, [expanded, path, uid]);

  const versionLabel =
    baseVersion || currentVersion
      ? t("versionRange", {
          base: shortVersion(baseVersion),
          current: shortVersion(currentVersion),
        })
      : null;

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-[rgb(var(--code-bg))]", className)}>
      {/* Banner: what happened + exit. Accent blue per design §5. */}
      <div className="flex shrink-0 items-center gap-2 border-b border-[rgba(74,158,255,.3)] bg-[rgba(74,158,255,.1)] px-2.5 py-1 text-[11px]">
        <GitCompare size={12} className="shrink-0 text-[#4a9eff]" />
        <span className="shrink-0 font-medium text-txt" title={path}>
          {basename(path)}
        </span>
        {versionLabel ? (
          <span className="shrink-0 font-mono text-[10px] text-faint">{versionLabel}</span>
        ) : null}
        <span className="min-w-0 flex-1 truncate text-muted">
          {degraded ? t("degradedHint") : t("hint")}
        </span>
        {large && !expanded ? (
          <button
            type="button"
            onClick={() => setExpanded(true)}
            className="flex shrink-0 items-center gap-1 rounded border border-[rgba(74,158,255,.4)] px-1.5 py-0.5 text-[11px] text-[#4a9eff] hover:bg-[rgba(74,158,255,.15)]"
          >
            <FileDiff size={12} />
            {t("viewDiff")}
          </button>
        ) : (
          <div
            role="group"
            aria-label={t("layoutGroup")}
            className="flex shrink-0 items-center gap-0.5"
          >
            <button
              type="button"
              title={t("sideBySide")}
              aria-pressed={sideBySide}
              onClick={() => setSideBySide(true)}
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded border transition-colors",
                sideBySide
                  ? "border-[#4a9eff] bg-[rgba(74,158,255,.15)] text-[#4a9eff]"
                  : "border-line text-faint hover:text-txt",
              )}
            >
              <Columns2 size={12} />
            </button>
            <button
              type="button"
              title={t("inline")}
              aria-pressed={!sideBySide}
              onClick={() => setSideBySide(false)}
              className={cn(
                "flex h-5 w-5 items-center justify-center rounded border transition-colors",
                !sideBySide
                  ? "border-[#4a9eff] bg-[rgba(74,158,255,.15)] text-[#4a9eff]"
                  : "border-line text-faint hover:text-txt",
              )}
            >
              <AlignJustify size={12} />
            </button>
          </div>
        )}
        <button
          type="button"
          title={t("exit")}
          aria-label={t("exit")}
          onClick={onExit}
          className="flex h-5 w-5 shrink-0 items-center justify-center rounded text-faint transition-colors hover:bg-card2 hover:text-txt"
        >
          <X size={14} />
        </button>
      </div>
      {expanded ? <div ref={hostRef} className="min-h-0 flex-1" /> : null}
    </div>
  );
}
