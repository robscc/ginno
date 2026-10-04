"use client";

/**
 * Conflict diff for the code panel (brief §3.4 / design §4.5): the user's
 * unsaved buffer against what is on disk right now.
 *
 * This is the second Monaco surface in the panel, so it deliberately reuses
 * `MonacoEditor`'s theme registry and worker environment instead of defining
 * its own — two palettes would drift apart the first time one of them changes.
 * It takes plain strings and knows nothing about the code API, so S3 can mount
 * the same component for HEAD-vs-worktree.
 *
 * Ownership rule (same pit as `MonacoEditor`): one model per side, both
 * disposed together with the editor on unmount. Leaking either is a stable
 * memory leak — Monaco never garbage-collects a model you forgot to dispose.
 */

import { useEffect, useRef, useState } from "react";
import { AlignJustify, Columns2, GitCompare } from "lucide-react";
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
type TextModel = import("monaco-editor").editor.ITextModel;

export interface ConflictDiffProps {
  /** Display only (header label / title). */
  path: string;
  /** The user's buffer — the side that holds the unsaved edits. */
  mine: string;
  /** Current contents on disk. */
  theirs: string;
  className?: string;
}

/**
 * Instance counter for the model URIs. Two diff views of the same file can be
 * mounted at once (S3's HEAD-vs-worktree next to a conflict view), and Monaco
 * throws on `createModel` when a URI is already taken — so the instance id is
 * part of the authority, exactly like the root id in `MonacoEditor`.
 */
let instanceSeq = 0;

/** `inmemory://ginno-conflict-<n>/<path>/mine` (or `/theirs`). */
function diffUri(monaco: Monaco, uid: number, path: string, side: "mine" | "theirs") {
  return monaco.Uri.from({
    scheme: "inmemory",
    authority: `ginno-conflict-${uid}`,
    path: `/${path}/${side}`,
  });
}

function basename(path: string): string {
  const parts = path.split("/");
  return parts[parts.length - 1] || path;
}

/**
 * Read-only Monaco diff: buffer vs disk. Side-by-side by default, with an
 * inline (unified) toggle for narrow panels.
 */
export function ConflictDiff({ path, mine, theirs, className }: ConflictDiffProps): JSX.Element {
  const t = useTranslations("code.diff");
  const hostRef = useRef<HTMLDivElement | null>(null);
  const diffRef = useRef<DiffEditor | null>(null);
  const mineModelRef = useRef<TextModel | null>(null);
  const theirsModelRef = useRef<TextModel | null>(null);
  const monacoRef = useRef<Monaco | null>(null);
  // Latest text lives in a ref so the async creation below always seeds the
  // models with the values from the newest render, not the ones it captured.
  const latestRef = useRef({ mine, theirs });
  const sideBySideRef = useRef(true);

  const [sideBySide, setSideBySide] = useState(true);
  const [uid] = useState<number>(() => ++instanceSeq);

  // Text updates. Runs before the awaited import resolves, so the ref is
  // already current when the models are created.
  useEffect(() => {
    latestRef.current = { mine, theirs };
    const mineModel = mineModelRef.current;
    const theirsModel = theirsModelRef.current;
    // setValue (not a new model) keeps the diff editor's own state intact;
    // both sides are read-only, so losing the undo stack costs nothing.
    if (mineModel && mineModel.getValue() !== mine) mineModel.setValue(mine);
    if (theirsModel && theirsModel.getValue() !== theirs) theirsModel.setValue(theirs);
  }, [mine, theirs]);

  // Layout toggle. `useInlineViewWhenSpaceIsLimited` is off at creation, so
  // this option is the only thing deciding the layout.
  useEffect(() => {
    sideBySideRef.current = sideBySide;
    diffRef.current?.updateOptions({ renderSideBySide: sideBySide });
  }, [sideBySide]);

  // Mount-scoped: editor + both models + theme observer, all disposed together.
  // Re-arms on `path` so a different file gets fresh models (and fresh URIs).
  useEffect(() => {
    let disposed = false;
    const observer = new MutationObserver(() => {
      monacoRef.current?.editor.setTheme(appThemeName());
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });

    void (async () => {
      // Worker factory first — shared global environment (see MonacoEditor).
      installMonacoEnvironment();

      // Dynamic import is MANDATORY. A module-scope `import * as monaco` fails
      // the static-export prerender with "ReferenceError: window is not defined".
      const monaco = await import("monaco-editor");
      if (disposed || !hostRef.current) return;
      monacoRef.current = monaco;
      ensureThemes(monaco);
      monaco.editor.setTheme(appThemeName());

      // Both sides are the same file, so both models get the same language.
      const language = languageForPath(path);
      const original = monaco.editor.createModel(
        latestRef.current.mine,
        language,
        diffUri(monaco, uid, path, "mine"),
      );
      const modified = monaco.editor.createModel(
        latestRef.current.theirs,
        language,
        diffUri(monaco, uid, path, "theirs"),
      );
      mineModelRef.current = original;
      theirsModelRef.current = modified;

      diffRef.current = monaco.editor.createDiffEditor(hostRef.current, {
        readOnly: true,
        originalEditable: false,
        renderSideBySide: sideBySideRef.current,
        // Without this Monaco silently flips to the inline view in a narrow
        // panel and the toggle above would be lying.
        useInlineViewWhenSpaceIsLimited: false,
        // A conflict is about bytes: whitespace-only edits must be visible,
        // otherwise "no difference shown" would hide a real overwrite.
        ignoreTrimWhitespace: false,
        automaticLayout: true,
        minimap: { enabled: false },
        renderOverviewRuler: false,
        // Sticky scroll steals vertical space in a narrow panel.
        stickyScroll: { enabled: false },
        fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
        fontSize: 12,
        lineNumbersMinChars: 3,
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
      // Editor first, then its models — a model still referenced by a live
      // editor must not be disposed underneath it.
      diffRef.current?.dispose();
      mineModelRef.current?.dispose();
      theirsModelRef.current?.dispose();
      diffRef.current = null;
      mineModelRef.current = null;
      theirsModelRef.current = null;
    };
  }, [path, uid]);

  // 同时标出两侧含义的一行说明，随布局（并排/行内）切换措辞。
  const hint = sideBySide ? t("hintSide") : t("hintInline");

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-[rgb(var(--code-bg))]", className)}>
      <div className="flex shrink-0 items-center gap-2 border-b border-line bg-panel px-2.5 py-1">
        <GitCompare size={12} className="shrink-0 text-violet" />
        <span className="max-w-[45%] shrink-0 truncate text-[11px] text-faint" title={path}>
          {basename(path)}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-muted" title={hint}>
          {hint}
        </span>
        <div role="group" aria-label={t("layoutGroup")} className="flex shrink-0 items-center gap-0.5">
          <button
            type="button"
            title={t("sideBySide")}
            aria-pressed={sideBySide}
            onClick={() => setSideBySide(true)}
            className={cn(
              "flex h-5 w-5 items-center justify-center rounded border transition-colors",
              sideBySide
                ? "border-violet bg-violet/10 text-violet"
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
                ? "border-violet bg-violet/10 text-violet"
                : "border-line text-faint hover:text-txt",
            )}
          >
            <AlignJustify size={12} />
          </button>
        </div>
      </div>
      <div ref={hostRef} className="min-h-0 flex-1" />
    </div>
  );
}