"use client";

/**
 * Monaco surface for the code panel (design §3.4 编辑器 / §4.5). Editable when
 * the runtime says the file may be written (`editable`); read-only otherwise.
 *
 * Owns exactly one model per mounted instance, keyed by `inmemory://ginno/
 * <rootId>/<relPath>`. Same-named files in different roots must never share a
 * model — that is the pit explicitly called out in design §4.5 — so the root id
 * is part of the URI. Closing a tab unmounts this component, which disposes both
 * the model and the editor (leaking either is a stable memory leak).
 *
 * Dirty state is tracked by comparing `model.getAlternativeVersionId()` against
 * a save baseline, never by diffing text: a text diff reports "undo back to the
 * original" as still dirty (brief §1-1). The baseline is re-armed when the model
 * is (re)loaded and whenever the parent bumps `markSavedNonce` after a
 * successful save. ⌘S is registered through Monaco's command registry — a DOM
 * keydown listener on the container would race Monaco's own key handling.
 *
 * Degradation is handled here, not by the caller: `text === null` renders a
 * notice instead of an empty editor, and `editable === false` shows a banner
 * explaining *why* the file is read-only.
 */

import { useEffect, useRef } from "react";
import { AlertTriangle, Info, Lock } from "lucide-react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";
import type { CodeRead } from "@/lib/codeTypes";
import { languageForPath } from "./language";

/** Exported for `ConflictDiff` (S2), the code panel's second Monaco surface. */
export type Monaco = typeof import("monaco-editor");
type Editor = import("monaco-editor").editor.IStandaloneCodeEditor;
type Model = import("monaco-editor").editor.ITextModel;
type Disposable = import("monaco-editor").IDisposable;
type ThemeData = import("monaco-editor").editor.IStandaloneThemeData;

export interface MonacoEditorProps {
  /** Root id — part of the model URI (design §4.5). */
  rootId: string;
  /** Root-relative path — the model URI's tail. */
  path: string;
  /** Contents from /api/code/read. null = cannot be shown as text. */
  text: string | null;
  /** Monaco language id from the runtime. Falls back to the client-side
   *  extension map when absent/empty. */
  language?: string | null;
  /** Detected encoding ("utf-8" / "gbk" / "latin-1" …), surfaced when non-UTF-8. */
  encoding?: string | null;
  /** False for ro mounts, .git internals, oversized files. */
  editable?: boolean;
  /** Why the file is read-only; drives the banner copy. */
  readonlyReason?: CodeRead["readonly_reason"];
  /** File size for the "too-large" copy (from the tree listing). The `read`
   *  response does not carry it, so the caller passes it when known. */
  sizeBytes?: number | null;
  /** 1-based line to centre on once the model is ready (jump from chat). */
  revealLine?: number | null;
  /** Bumped by `openInCode` so an identical `revealLine` re-centres. */
  revealNonce?: number;
  /** A bump means the parent accepted the current buffer as the new clean
   *  baseline (save succeeded) — see brief §3.3. */
  markSavedNonce?: number;
  /** Dirty state, from `getAlternativeVersionId()` vs the baseline (brief §1-1).
   *  Fires only when the value flips, not once per keystroke. */
  onDirtyChange?: (dirty: boolean) => void;
  /** ⌘S / Ctrl+S: hands the current buffer text to the parent to save. */
  onSave?: (text: string) => void;
  /** The live buffer, on every content change (including a programmatic
   *  reload). The parent needs it to resolve a CONFLICT safely: after a refused
   *  save the user may keep typing, and overwriting with the text from the
   *  failed attempt would silently discard those keystrokes. A parent that
   *  caches this in a ref pays no re-render for it. */
  onChangeText?: (text: string) => void;
  className?: string;
}

// ── Theme ───────────────────────────────────────────────────────────────────
// Derived from the `globals.css` token palette so Monaco matches the app
// instead of clashing with a bare `vs` / `vs-dark` (design §4.5). `base` only
// seeds the token defaults we do not override — the viewer is never set to the
// built-in theme by name.
const DARK: ThemeData = {
  base: "vs-dark",
  inherit: true,
  rules: [
    { token: "comment", foreground: "62626e", fontStyle: "italic" },
    { token: "keyword", foreground: "bb9af7" },
    { token: "string", foreground: "9ece6a" },
    { token: "number", foreground: "ff9e64" },
    { token: "type", foreground: "e0af68" },
    { token: "type.identifier", foreground: "e0af68" },
    { token: "identifier", foreground: "e9e9f0" },
    { token: "function", foreground: "7aa2f7" },
    { token: "variable", foreground: "73daca" },
    { token: "variable.predefined", foreground: "73daca" },
    { token: "constant", foreground: "ff9e64" },
    { token: "operator", foreground: "9a9aa6" },
    { token: "delimiter", foreground: "9a9aa6" },
    { token: "tag", foreground: "f7768e" },
    { token: "attribute.name", foreground: "e0af68" },
    { token: "attribute.value", foreground: "9ece6a" },
    { token: "key", foreground: "7aa2f7" },
    { token: "regexp", foreground: "9ece6a" },
    { token: "metatag", foreground: "565f89" },
  ],
  colors: {
    "editor.background": "#0e0e14", // --code-bg
    "editor.foreground": "#e9e9f0", // --txt
    "editorLineNumber.foreground": "#62626e", // --faint
    "editorLineNumber.activeForeground": "#9a9aa6", // --muted
    "editorCursor.foreground": "#8b5cf6", // violet
    "editor.selectionBackground": "#8b5cf65c",
    "editor.inactiveSelectionBackground": "#8b5cf62e",
    "editor.lineHighlightBackground": "#1b1b25", // --card2
    "editor.lineHighlightBorder": "#00000000",
    "editorIndentGuide.background1": "#262632", // --line
    "editorIndentGuide.activeBackground1": "#34343f", // --line2
    "editorWhitespace.foreground": "#34343f",
    "editorWidget.background": "#15151d", // --card
    "editorWidget.border": "#262632",
    "editorGutter.background": "#0e0e14",
    "editorBracketMatch.background": "#8b5cf63d",
    "editorBracketMatch.border": "#8b5cf6",
    "editorBracketHighlight.foreground1": "#ff9e64",
    "editorBracketHighlight.foreground2": "#bb9af7",
    "editorBracketHighlight.foreground3": "#73daca",
    "editorOverviewRuler.border": "#00000000",
    "scrollbarSlider.background": "#26263280",
    "scrollbarSlider.hoverBackground": "#34343f99",
    "scrollbarSlider.activeBackground": "#34343f",
  },
};

const LIGHT: ThemeData = {
  base: "vs",
  inherit: true,
  rules: [
    { token: "comment", foreground: "82828e", fontStyle: "italic" },
    { token: "keyword", foreground: "8956d6" },
    { token: "string", foreground: "4e8a3c" },
    { token: "number", foreground: "b0622a" },
    { token: "type", foreground: "9a7422" },
    { token: "type.identifier", foreground: "9a7422" },
    { token: "identifier", foreground: "17171e" },
    { token: "function", foreground: "2f6fd0" },
    { token: "variable", foreground: "1f8a80" },
    { token: "variable.predefined", foreground: "1f8a80" },
    { token: "constant", foreground: "b0622a" },
    { token: "operator", foreground: "5a5a66" },
    { token: "delimiter", foreground: "5a5a66" },
    { token: "tag", foreground: "c24858" },
    { token: "attribute.name", foreground: "9a7422" },
    { token: "attribute.value", foreground: "4e8a3c" },
    { token: "key", foreground: "2f6fd0" },
    { token: "regexp", foreground: "4e8a3c" },
    { token: "metatag", foreground: "8a8fa3" },
  ],
  colors: {
    "editor.background": "#f5f5f9", // --code-bg
    "editor.foreground": "#17171e", // --txt
    "editorLineNumber.foreground": "#82828e",
    "editorLineNumber.activeForeground": "#5a5a66",
    "editorCursor.foreground": "#6366f1",
    "editor.selectionBackground": "#8b5cf63d",
    "editor.inactiveSelectionBackground": "#8b5cf61f",
    "editor.lineHighlightBackground": "#f3f3f7", // --card2
    "editor.lineHighlightBorder": "#00000000",
    "editorIndentGuide.background1": "#e2e2ea", // --line
    "editorIndentGuide.activeBackground1": "#d4d4de",
    "editorWhitespace.foreground": "#d4d4de",
    "editorWidget.background": "#ffffff", // --card
    "editorWidget.border": "#e2e2ea",
    "editorGutter.background": "#f5f5f9",
    "editorBracketMatch.background": "#8b5cf62e",
    "editorBracketMatch.border": "#6366f1",
    "editorBracketHighlight.foreground1": "#b0622a",
    "editorBracketHighlight.foreground2": "#8956d6",
    "editorBracketHighlight.foreground3": "#1f8a80",
    "editorOverviewRuler.border": "#00000000",
    "scrollbarSlider.background": "#e2e2ea80",
    "scrollbarSlider.hoverBackground": "#d4d4de99",
    "scrollbarSlider.activeBackground": "#d4d4de",
  },
};

/** Theme names — exported so `ConflictDiff` renders the same palette. */
export const DARK_NAME = "ginno-dark";
export const LIGHT_NAME = "ginno-light";

/** Monaco's theme registry is global; define once per page load. */
let themesReady = false;
export function ensureThemes(monaco: Monaco): void {
  if (themesReady) return;
  monaco.editor.defineTheme(DARK_NAME, DARK);
  monaco.editor.defineTheme(LIGHT_NAME, LIGHT);
  themesReady = true;
}

/** The app theme is the `light` class on <html> (see GeneralSettings.tsx). */
export function appThemeName(): string {
  if (typeof document === "undefined") return DARK_NAME;
  return document.documentElement.classList.contains("light") ? LIGHT_NAME : DARK_NAME;
}

/**
 * Install Monaco's worker factory. Global and idempotent — there is one
 * environment per page, shared by `MonacoEditor` and `ConflictDiff`.
 *
 * The catch-all factory is intentional: every label gets the generic editor
 * worker, so the TS/JSON language service stays off (design §4.5 "v1 允许无
 * worker"). The worker must be CLASSIC — do not pass `{ type: "module" }`.
 */
let envReady = false;
export function installMonacoEnvironment(): void {
  if (envReady) return;
  (globalThis as unknown as { MonacoEnvironment?: unknown }).MonacoEnvironment = {
    getWorker(): Worker {
      return new Worker(
        // Static `new URL(...)` is what webpack 5 bundles; keep this exact
        // bare-specifier + esm/vs form and never bump monaco past 0.55.x.
        new URL("monaco-editor/esm/vs/editor/editor.worker.js", import.meta.url),
      );
    },
  };
  envReady = true;
}

// ── Degradation copy (brief §4) ─────────────────────────────────────────────

/** "（8.2 MB）" when the caller supplied the size, else "". */
function sizeSuffix(sizeBytes: number | null | undefined): string {
  if (sizeBytes == null || !Number.isFinite(sizeBytes) || sizeBytes <= 0) return "";
  const mb = sizeBytes / (1024 * 1024);
  const text = mb >= 1 ? `${mb.toFixed(1)} MB` : `${Math.max(1, Math.round(sizeBytes / 1024))} KB`;
  return `(${text})`;
}

/** 非 UTF-8/ASCII 编码才提示（文案走 i18n catalog，见组件内的 encNotice）。 */
function needsEncodingNotice(encoding: string): boolean {
  const e = encoding.toLowerCase().replace("_", "-");
  return e !== "utf-8" && e !== "utf8" && e !== "ascii" && e !== "us-ascii";
}

/**
 * Code viewer/editor. Renders a notice instead of an editor when the file has
 * no text, and a read-only banner above the editor otherwise. Writable only
 * while `editable` is true; the parent owns versioning, saving and conflicts.
 */
export function MonacoEditor({
  rootId,
  path,
  text,
  language,
  encoding,
  editable = true,
  readonlyReason = null,
  sizeBytes = null,
  revealLine = null,
  revealNonce = 0,
  markSavedNonce = 0,
  onDirtyChange,
  onSave,
  onChangeText,
  className,
}: MonacoEditorProps) {
  const t = useTranslations("code.editor");
  const hostRef = useRef<HTMLDivElement | null>(null);
  const editorRef = useRef<Editor | null>(null);
  const modelRef = useRef<Model | null>(null);
  const monacoRef = useRef<Monaco | null>(null);
  const timerRef = useRef<number | null>(null);
  // Save baseline: the model's alternative version id at the last accepted save
  // (or load). Comparing ids — not text — is what makes "undo back to the
  // original" read as clean again (brief §1-1).
  const baselineRef = useRef(0);
  const dirtyRef = useRef(false);
  const changeSubRef = useRef<Disposable | null>(null);
  // Set around programmatic `setValue` so the content listener does not report
  // a reload as a user edit.
  const suppressChangeRef = useRef(false);
  // Monaco registers commands and listeners once, so they must read the live
  // props through refs instead of capturing the render that created them.
  const editableRef = useRef(editable);
  const onDirtyChangeRef = useRef(onDirtyChange);
  const onSaveRef = useRef(onSave);
  const onChangeTextRef = useRef(onChangeText);

  // Keep the refs above current. Declared before the effects that consume them
  // so it has already run by the time they do.
  useEffect(() => {
    editableRef.current = editable;
    onDirtyChangeRef.current = onDirtyChange;
    onSaveRef.current = onSave;
    onChangeTextRef.current = onChangeText;
  }, [editable, onDirtyChange, onSave, onChangeText]);

  // The runtime already resolves the language; the extension map is the
  // client-side fallback (and covers a bare/absent field).
  const resolvedLanguage =
    (language ?? "").trim() !== "" ? (language as string) : languageForPath(path);

  // ── Dirty tracking (brief §1-1) ───────────────────────────────────────────
  // All three helpers read refs only, so they are safe to call from the
  // effect-scoped listeners below (same rationale as `revealTarget`).

  /** Publish only on a flip so a keystroke does not re-render the panel. */
  function publishDirty(next: boolean): void {
    if (dirtyRef.current === next) return;
    dirtyRef.current = next;
    onDirtyChangeRef.current?.(next);
  }

  /** Recompute against the save baseline — never against the text. */
  function refreshDirty(): void {
    if (suppressChangeRef.current) return;
    const model = modelRef.current;
    if (!model) return;
    publishDirty(model.getAlternativeVersionId() !== baselineRef.current);
  }

  /** Accept whatever is in the buffer now as clean and as the save baseline. */
  function markBaseline(): void {
    const model = modelRef.current;
    if (!model) return;
    baselineRef.current = model.getAlternativeVersionId();
    publishDirty(false);
  }

  // Reveal + transient line highlight; safe to call before the editor exists.
  function revealTarget(): void {
    const monaco = monacoRef.current;
    const editor = editorRef.current;
    if (!monaco || !editor || revealLine == null || revealLine <= 0) return;
    const model = editor.getModel();
    const line = Math.min(Math.max(1, Math.floor(revealLine)), model?.getLineCount() ?? Number.MAX_SAFE_INTEGER);
    editor.revealLineInCenter(line);
    editor.setPosition({ lineNumber: line, column: 1 });
    const deco = editor.createDecorationsCollection([
      {
        range: new monaco.Range(line, 1, line, 1),
        options: {
          isWholeLine: true,
          // Tailwind scans this literal and emits the class; keeps the violet
          // selection accent without touching globals.css.
          className: "bg-[rgba(139,92,246,0.12)]",
        },
      },
    ]);
    if (timerRef.current != null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => deco.clear(), 1600);
  }

  // Mount-scoped: follow app theme changes; dispose on unmount.
  useEffect(() => {
    const observer = new MutationObserver(() => {
      monacoRef.current?.editor.setTheme(appThemeName());
    });
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
    return () => {
      observer.disconnect();
      if (timerRef.current != null) window.clearTimeout(timerRef.current);
      changeSubRef.current?.dispose();
      editorRef.current?.dispose();
      modelRef.current?.dispose();
      changeSubRef.current = null;
      editorRef.current = null;
      modelRef.current = null;
    };
  }, []);

  // Create/update the model + editor. Never disposes here so a re-render for
  // the same file keeps scroll position and undo history.
  useEffect(() => {
    let disposed = false;
    void (async () => {
      // Worker factory first — the conflict diff shares this one global
      // environment (see installMonacoEnvironment).
      installMonacoEnvironment();

      // Dynamic import is MANDATORY. A module-scope `import * as monaco` fails
      // the static-export prerender with "ReferenceError: window is not defined".
      const monaco = await import("monaco-editor");
      if (disposed) return;
      monacoRef.current = monaco;
      ensureThemes(monaco);
      monaco.editor.setTheme(appThemeName());

      if (!hostRef.current) return;
      const uri = monaco.Uri.from({
        scheme: "inmemory",
        authority: "ginno",
        path: `/${rootId}/${path}`,
      });

      if (!modelRef.current || modelRef.current.uri.toString() !== uri.toString()) {
        changeSubRef.current?.dispose();
        changeSubRef.current = null;
        modelRef.current?.dispose();
        modelRef.current = monaco.editor.createModel(text ?? "", resolvedLanguage, uri);
        // A fresh model holds what `read` returned, so it starts clean — and
        // that id is the baseline the first edit is measured against.
        changeSubRef.current = modelRef.current.onDidChangeContent(() => {
          refreshDirty();
          // Publish the live buffer. Kept OUT of `refreshDirty` on purpose:
          // refreshDirty is suppressed around a programmatic reload, but the
          // parent's cache must track the buffer in that case too.
          if (modelRef.current) onChangeTextRef.current?.(modelRef.current.getValue());
        });
        markBaseline();
        if (editorRef.current) {
          editorRef.current.setModel(modelRef.current);
          editorRef.current.setScrollTop(0);
        } else {
          editorRef.current = monaco.editor.create(hostRef.current, {
            model: modelRef.current,
            readOnly: !editableRef.current,
            automaticLayout: true,
            minimap: { enabled: false },
            wordWrap: "on",
            renderWhitespace: "boundary",
            bracketPairColorization: { enabled: true },
            // Sticky scroll steals vertical space in a narrow panel.
            stickyScroll: { enabled: false },
            fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
            fontSize: 12,
            lineNumbersMinChars: 3,
            scrollBeyondLastLine: false,
            smoothScrolling: true,
            padding: { top: 8, bottom: 8 },
            renderLineHighlight: "line",
            scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8, useShadows: false },
          });
          // ⌘S / Ctrl+S through Monaco's own command registry (brief §3.3):
          // the keybinding is then resolved by Monaco's handler rather than
          // racing it from a DOM listener on the container.
          editorRef.current.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, () => {
            const cb = onSaveRef.current;
            // Read-only buffers (ro mount, .git internals, oversized, binary)
            // have nothing to write back: swallow the key instead of asking the
            // parent to save something the server will reject.
            if (!cb || !editableRef.current) return;
            const model = editorRef.current?.getModel();
            if (!model) return;
            cb(model.getValue());
          });
        }
      } else if (modelRef.current.getValue() !== (text ?? "")) {
        // Programmatic replace (e.g. reload after a conflict). The buffer now
        // matches what the parent handed us, so it becomes the clean baseline;
        // suppressed so the reload is not reported as a user edit.
        suppressChangeRef.current = true;
        modelRef.current.setValue(text ?? "");
        suppressChangeRef.current = false;
        markBaseline();
      }

      if (disposed) return;
      revealTarget();
    })();

    return () => {
      disposed = true;
    };
    // `revealTarget`, `refreshDirty` and `markBaseline` are intentionally
    // excluded: they are recreated every render and only read refs, so they
    // never need to re-arm this effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rootId, path, text, resolvedLanguage]);

  // A bump means the parent accepted the buffer as the new clean baseline (save
  // succeeded). Re-arm from the current alt version id — see brief §3.3.
  useEffect(() => {
    markBaseline();
    // `markBaseline` reads refs only, so its per-render identity is irrelevant.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [markSavedNonce]);

  // `readOnly` is an editor option, not a construction detail: a root flipping
  // between rw and ro must take effect without remounting Monaco (which would
  // throw away scroll position and undo history).
  useEffect(() => {
    editorRef.current?.updateOptions({ readOnly: !editable });
  }, [editable]);

  // Re-centre when a jump targets an already-open file.
  useEffect(() => {
    revealTarget();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [revealLine, revealNonce, rootId, path]);

  // 降级文案（brief §4）：原先的 reason → 文案映射改为就地用 t() 构建，
  // 映射逻辑保持不变；{size} 为空串时得到与旧版一致的省略形式。
  const unavailable = text === null;
  const unavailableMsg = text === null
    ? readonlyReason === "binary"
      ? t("unavailableBinary")
      : readonlyReason === "too-large"
        ? t("unavailableTooLarge", { size: sizeSuffix(sizeBytes) })
        : t("unavailableDefault")
    : null;
  const banner = editable
    ? null
    : readonlyReason === "read-only-mount"
      ? t("bannerReadOnlyMount")
      : readonlyReason === "too-large"
        ? t("bannerTooLarge", { size: sizeSuffix(sizeBytes) })
        : readonlyReason === "git-internal"
          ? t("bannerGitInternal")
          : null;
  const encNotice =
    encoding && needsEncodingNotice(encoding)
      ? t("encodingNotice", { encoding: encoding.toUpperCase() })
      : null;

  return (
    <div className={cn("flex h-full min-h-0 flex-col bg-[rgb(var(--code-bg))]", className)}>
      {banner ? (
        <div className="flex shrink-0 items-center gap-1.5 border-b border-line bg-card px-2.5 py-1 text-[11px] text-muted">
          <Lock size={12} className="shrink-0" />
          <span className="truncate">{banner}</span>
        </div>
      ) : null}
      {encNotice ? (
        <div className="flex shrink-0 items-center gap-1.5 border-b border-line bg-card px-2.5 py-1 text-[11px] text-muted">
          <Info size={12} className="shrink-0" />
          <span className="truncate">{encNotice}</span>
        </div>
      ) : null}
      {unavailable ? (
        <div className="flex min-h-0 flex-1 flex-col items-center justify-center gap-2 px-6 text-center text-xs text-faint">
          <AlertTriangle size={20} />
          <span>{unavailableMsg}</span>
        </div>
      ) : (
        <div ref={hostRef} className="min-h-0 flex-1" />
      )}
    </div>
  );
}