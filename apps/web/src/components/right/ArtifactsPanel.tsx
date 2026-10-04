"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import {
  AlertTriangle,
  Boxes,
  Check,
  Copy,
  Download,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  Image as ImageIcon,
  Link2,
  Loader2,
  Pencil,
  Trash2,
  Workflow,
  X,
} from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import { ConfirmModal } from "@/components/ConfirmModal";
import type { Artifact, ArtifactMeta, FileEntry } from "@/lib/types";

export const ICON: Record<string, typeof FileText> = {
  workflow: Workflow,
  link: Link2,
  doc: FileText,
  file: FileSpreadsheet,
  image: ImageIcon,
};

// kind → 文案 key 的固定映射（值是 right.artifacts 下的 key，翻译在渲染时按
// locale 取——模块层不能调用 hook）。TodoPanel 等外部引用同一映射。
export const KIND_LABEL: Record<
  string,
  "kind.file" | "kind.doc" | "kind.link" | "kind.workflow" | "kind.image"
> = {
  file: "kind.file",
  doc: "kind.doc",
  link: "kind.link",
  workflow: "kind.workflow",
  image: "kind.image",
};

// Registry file kinds — the classification that steers prompt tool guidance
// (analyze_table for spreadsheet/table, parse_document otherwise).
const FILE_KINDS = ["spreadsheet", "table", "document", "presentation", "pdf", "data", "text", "image"];
// 同上：file_kind → 文案 key 映射。
const FILE_KIND_LABEL: Record<string, "fileKind.spreadsheet" | "fileKind.table" | "fileKind.document" | "fileKind.presentation" | "fileKind.pdf" | "fileKind.data" | "fileKind.text" | "fileKind.image" | "fileKind.unknown"> = {
  spreadsheet: "fileKind.spreadsheet",
  table: "fileKind.table",
  document: "fileKind.document",
  presentation: "fileKind.presentation",
  pdf: "fileKind.pdf",
  data: "fileKind.data",
  text: "fileKind.text",
  image: "fileKind.image",
  unknown: "fileKind.unknown",
};

function fmtBytes(n?: number): string {
  if (n === undefined || n === null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// Artifact refs created before server-side path normalization can carry an
// unresolved prefix (macOS: /tmp vs the registry's /private/tmp). Match
// exactly first, then fall back to the trailing <session-dir>/<uuid-file>
// segments, which are collision-resistant.
function matchByRef(files: FileEntry[], ref: string): FileEntry | undefined {
  return (
    files.find((f) => f.path === ref) ??
    files.find((f) => f.path.endsWith("/" + ref.split("/").slice(-2).join("/")))
  );
}

function MetaRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="w-14 shrink-0 text-[11px] text-faint">{label}</span>
      <div className="min-w-0 flex-1">{children}</div>
    </div>
  );
}

export interface Rect {
  top: number;
  bottom: number;
  left: number;
}

/** Hover inspector for one artifact. Shows exactly what prompt injection
 *  would use (schema summary + provenance), and lets the user correct
 *  name / kinds / the summary — corrections persist and win over recomputation.
 *  Exported so other panels (e.g. TODO associations) can reuse the same card. */
export function ArtifactMetaCard({
  artifact,
  rect,
  editing,
  setEditing,
  onClose,
}: {
  artifact: Artifact;
  rect: Rect;
  editing: boolean;
  setEditing: (v: boolean) => void;
  onClose: () => void;
}) {
  const g = useGinno();
  // i18n：t 元数据卡片框架文案（名称/路径/摘要内容是用户数据，不翻译），tc 通用按钮
  const t = useTranslations("right.artifacts");
  const tc = useTranslations("right.common");
  const [meta, setMeta] = useState<ArtifactMeta | null>(null);
  const [loadError, setLoadError] = useState("");
  // edit draft + status
  const [draft, setDraft] = useState({ name: "", kind: "", schema: "", file_kind: "" });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedFlash, setSavedFlash] = useState(false);
  const [copied, setCopied] = useState(false);

  // 相对时间文案（common 域，按当前 locale 组装）
  const rel = (ts: number): string => {
    const d = Date.now() / 1000 - ts;
    if (d < 60) return tc("relNow");
    if (d < 3600) return tc("relMinutes", { n: Math.floor(d / 60) });
    if (d < 86400) return tc("relHours", { n: Math.floor(d / 3600) });
    if (d < 86400 * 30) return tc("relDays", { n: Math.floor(d / 86400) });
    return new Date(ts * 1000).toLocaleDateString();
  };
  // kind / file_kind → 当前语言文案（未登记的值原样回显）
  const kindLabel = (k: string): string => {
    const key = KIND_LABEL[k];
    return key ? t(key) : k;
  };
  const fileKindLabel = (k: string | undefined): string => {
    if (!k) return "—";
    const key = FILE_KIND_LABEL[k];
    return key ? t(key) : k;
  };

  async function load() {
    setLoadError("");
    try {
      const m = await api.getArtifactMetadata(artifact.id);
      if (!m.ok) throw new Error(m.error || t("meta.loadFailedShort"));
      setMeta(m);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : t("meta.runtimeNotReady"));
    }
  }
  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [artifact.id]);

  // Escape: in edit mode → discard draft back to view; in view mode → close.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.stopPropagation();
      if (editing) setEditing(false);
      else onClose();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, [editing, setEditing, onClose]);

  const enterEdit = () => {
    if (!meta) return;
    setDraft({
      name: meta.artifact?.name ?? "",
      kind: meta.artifact?.kind ?? "file",
      // Start from whatever currently gets injected (override or computed),
      // so a correction is an edit, not a rewrite from scratch.
      schema: meta.schema ?? "",
      file_kind: meta.file?.kind ?? "",
    });
    setSaveError("");
    setEditing(true);
  };

  const dirty =
    !!meta &&
    (draft.name !== (meta.artifact?.name ?? "") ||
      draft.kind !== (meta.artifact?.kind ?? "") ||
      draft.schema !== (meta.schema ?? "") ||
      draft.file_kind !== (meta.file?.kind ?? ""));

  async function save() {
    setSaving(true);
    setSaveError("");
    const patch: Record<string, string> = { name: draft.name, kind: draft.kind, schema: draft.schema };
    if (meta?.file) patch.file_kind = draft.file_kind;
    const r = await g.patchArtifact(artifact.id, patch);
    setSaving(false);
    if (!r.ok) {
      setSaveError(r.error || t("meta.saveFailed"));
      return;
    }
    setEditing(false);
    setSavedFlash(true);
    window.setTimeout(() => setSavedFlash(false), 1800);
    await load(); // reflect the canonical (trimmed/whitelisted) record + new provenance
  }

  async function copyPath(p: string) {
    try {
      await navigator.clipboard.writeText(p);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1600);
    } catch {
      /* clipboard unavailable — ignore */
    }
  }

  // Flip above/below so the card never overflows the viewport vertically.
  const vh = typeof window !== "undefined" ? window.innerHeight : 900;
  const flip = rect.top > vh * 0.55;
  const maxW = Math.max(280, Math.min(380, rect.left - 32));
  const posStyle: React.CSSProperties = flip
    ? { right: window.innerWidth - rect.left + 10, bottom: vh - rect.bottom + 6, maxHeight: rect.bottom - 16 }
    : { right: window.innerWidth - rect.left + 10, top: Math.max(8, rect.top - 6), maxHeight: vh - rect.top - 16 };

  const Ic = ICON[artifact.kind] || FileText;
  const isFile = artifact.kind === "file" || artifact.kind === "image";

  return (
    <div
      className="meta-pop-in fixed z-50 flex flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-2xl"
      style={{ ...posStyle, width: maxW }}
      role="dialog"
      aria-label={t("meta.cardLabel", { name: artifact.name })}
    >
      {/* header */}
      <div className="flex items-center gap-2 border-b border-line px-3.5 py-2.5">
        <Ic className="h-4 w-4 shrink-0 text-violet" />
        {editing ? (
          <input
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            className="min-w-0 flex-1 rounded border border-line2 bg-base/60 px-1.5 py-0.5 text-sm text-txt outline-none focus:border-violet"
            placeholder={t("meta.namePlaceholder")}
          />
        ) : (
          <span className="min-w-0 flex-1 truncate text-sm font-semibold text-txt">{artifact.name}</span>
        )}
        <span className="shrink-0 rounded-full bg-violet/15 px-2 py-0.5 text-[10px] text-violet">
          {editing ? (
            <select
              value={draft.kind}
              onChange={(e) => setDraft((d) => ({ ...d, kind: e.target.value }))}
              className="bg-transparent text-[10px] outline-none"
            >
              {Object.entries(KIND_LABEL).map(([k, key]) => (
                <option key={k} value={k} className="bg-panel text-txt">
                  {t(key)}
                </option>
              ))}
            </select>
          ) : (
            kindLabel(artifact.kind)
          )}
        </span>
        {savedFlash && <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />}
        {!editing && (
          <button
            onClick={enterEdit}
            disabled={!meta}
            title={t("meta.editTitle")}
            className="shrink-0 rounded-md p-1 text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
        )}
        <button
          onClick={onClose}
          title={tc("close")}
          className="shrink-0 rounded-md p-1 text-muted hover:bg-card2 hover:text-txt"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto px-3.5 py-3">
        {loadError && <div className="text-xs text-red">{t("meta.loadFailed", { e: loadError })}</div>}
        {!meta && !loadError && (
          <div className="space-y-2 py-1">
            <div className="h-3 w-2/3 animate-pulse rounded bg-card2" />
            <div className="h-3 w-1/2 animate-pulse rounded bg-card2" />
            <div className="h-16 w-full animate-pulse rounded bg-card2" />
          </div>
        )}

        {meta && (
          <>
            {/* missing-file warning — the first thing to verify */}
            {isFile && meta.exists === false && (
              <div className="flex items-center gap-2 rounded-lg border border-yellow/40 bg-yellow/10 px-2.5 py-1.5 text-[11px] text-yellow">
                <AlertTriangle className="h-3.5 w-3.5 shrink-0" />
                {t("meta.fileMissing")}
              </div>
            )}

            <div className="space-y-1.5">
              <MetaRow label={t("meta.path")}>
                <span className="flex items-center gap-1">
                  <code
                    className="min-w-0 flex-1 truncate font-mono text-[11px] text-muted"
                    title={meta.file?.path || artifact.ref || "—"}
                  >
                    {meta.file?.path || artifact.ref || "—"}
                  </code>
                  {(meta.file?.path || artifact.ref) && (
                    <button
                      onClick={() => void copyPath(meta.file?.path || artifact.ref)}
                      title={t("meta.copyPath")}
                      className="shrink-0 rounded p-0.5 text-faint hover:bg-card2 hover:text-txt"
                    >
                      {copied ? (
                        <Check className="h-3 w-3 text-emerald-400" />
                      ) : (
                        <Copy className="h-3 w-3" />
                      )}
                    </button>
                  )}
                </span>
              </MetaRow>
              {isFile && meta.file && (
                <>
                  <MetaRow label={t("meta.size")}>
                    <span className="text-[11px] text-muted">
                      {fmtBytes(meta.file.size)}
                      {meta.file.mime ? ` · ${meta.file.mime}` : ""}
                    </span>
                  </MetaRow>
                  <MetaRow label={t("meta.detectedType")}>
                    {editing ? (
                      <select
                        value={draft.file_kind}
                        onChange={(e) => setDraft((d) => ({ ...d, file_kind: e.target.value }))}
                        className="rounded border border-line2 bg-base/60 px-1 py-0.5 text-[11px] text-txt outline-none focus:border-violet"
                      >
                        {FILE_KINDS.map((k) => (
                          <option key={k} value={k} className="bg-panel text-txt">
                            {fileKindLabel(k)}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className="text-[11px] text-muted">{fileKindLabel(meta.file.kind)}</span>
                    )}
                  </MetaRow>
                </>
              )}
              <MetaRow label={t("meta.session")}>
                <span className="font-mono text-[11px] text-muted">
                  {artifact.session_id ? artifact.session_id.slice(0, 8) : "—"}
                </span>
              </MetaRow>
              <MetaRow label={t("meta.added")}>
                <span className="text-[11px] text-muted">{rel(artifact.created)}</span>
              </MetaRow>
            </div>

            {/* schema summary — what actually lands in the model context */}
            <div className="rounded-lg border border-line bg-card/50">
              <div className="flex items-center gap-2 border-b border-line px-2.5 py-1.5">
                <span className="text-[11px] font-medium text-txt">{t("meta.schemaSummary")}</span>
                {meta.schema ? (
                  <span
                    className={`rounded-full px-1.5 py-px text-[10px] ${
                      meta.schema_source === "override"
                        ? "bg-emerald-500/15 text-emerald-400"
                        : "bg-card2 text-faint"
                    }`}
                  >
                    {meta.schema_source === "override" ? t("meta.manualOverride") : t("meta.autoComputed")}
                  </span>
                ) : (
                  <span className="rounded-full bg-card2 px-1.5 py-px text-[10px] text-faint">
                    {t("meta.schemaNone")}
                  </span>
                )}
              </div>
              {editing ? (
                <textarea
                  value={draft.schema}
                  onChange={(e) => setDraft((d) => ({ ...d, schema: e.target.value }))}
                  rows={5}
                  placeholder={t("meta.schemaPlaceholder")}
                  className="w-full resize-y bg-transparent px-2.5 py-2 font-mono text-[11px] leading-relaxed text-txt outline-none"
                />
              ) : (
                <div className="max-h-32 overflow-y-auto px-2.5 py-2">
                  {meta.schema ? (
                    <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted">
                      {meta.schema}
                    </pre>
                  ) : (
                    <span className="text-[11px] text-faint">{t("meta.noSchema")}</span>
                  )}
                </div>
              )}
              <div className="border-t border-line px-2.5 py-1.5 text-[10px] leading-snug text-faint">
                {editing ? t("meta.editHint") : t("meta.viewHint")}
              </div>
            </div>
          </>
        )}
      </div>

      {/* edit footer */}
      {editing && (
        <div className="flex items-center gap-2 border-t border-line px-3.5 py-2">
          {saveError && <span className="min-w-0 flex-1 truncate text-[11px] text-red">{saveError}</span>}
          <div className="ml-auto flex gap-2">
            <button
              onClick={() => setEditing(false)}
              disabled={saving}
              className="rounded-lg border border-line2 px-3 py-1 text-xs text-muted hover:text-txt disabled:opacity-40"
            >
              {tc("cancel")}
            </button>
            <button
              onClick={() => void save()}
              disabled={saving || !dirty}
              title={!dirty ? t("meta.noChanges") : undefined}
              className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
            >
              {saving && <Loader2 className="h-3 w-3 animate-spin" />}
              {t("meta.save")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function ArtifactsPanel() {
  const g = useGinno();
  // i18n：产物列表框架文案（产物名/路径是用户数据，不翻译）
  const t = useTranslations("right.artifacts");
  const tc = useTranslations("right.common");
  const items = g.artifacts;
  const flashing = new Set(g.flashArtifactIds);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [doneId, setDoneId] = useState<string | null>(null);
  // Delete is confirmed via an in-app modal (window.confirm is unreliable in
  // the Tauri webview), so an accidental trash click never deletes anything.
  const [deleteTarget, setDeleteTarget] = useState<Artifact | null>(null);
  // Hover inspector: {artifact snapshot, anchor rect, edit flag}
  const [hover, setHover] = useState<{ a: Artifact; rect: Rect } | null>(null);
  const [inspectorEditing, setInspectorEditing] = useState(false);
  const enterTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const leaveTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearTimers = () => {
    if (enterTimer.current) clearTimeout(enterTimer.current);
    if (leaveTimer.current) clearTimeout(leaveTimer.current);
  };

  function scheduleOpen(a: Artifact, el: HTMLElement) {
    clearTimers();
    enterTimer.current = setTimeout(() => {
      const r = el.getBoundingClientRect();
      setHover({ a, rect: { top: r.top, bottom: r.bottom, left: r.left } });
    }, 200);
  }
  function scheduleClose() {
    // Never auto-close while the user has unsaved edits.
    if (inspectorEditing) return;
    clearTimers();
    leaveTimer.current = setTimeout(() => setHover(null), 250);
  }
  function cancelClose() {
    if (leaveTimer.current) clearTimeout(leaveTimer.current);
  }

  // File artifacts open in the SheetViewer/document preview. The artifact
  // only carries a path (ref), so resolve the registry entry to get its id.
  async function openArtifact(a: (typeof items)[number]) {
    if ((a.kind !== "file" && a.kind !== "image") || !a.ref) return;
    try {
      const entry = matchByRef(await api.listFiles(), a.ref);
      if (entry) {
        g.openPreview({ id: entry.id, name: entry.name, path: entry.path, kind: entry.kind, mtime: entry.mtime });
      }
    } catch {
      /* sidecar hiccup — ignore */
    }
  }

  // Save a copy of the file into the OS Downloads folder (desktop) or trigger
  // a browser download (dev). Independent of opening the preview.
  async function downloadArtifact(e: React.MouseEvent, a: (typeof items)[number]) {
    e.stopPropagation();
    if ((a.kind !== "file" && a.kind !== "image") || !a.ref || busyId) return;
    setBusyId(a.id);
    try {
      const entry = matchByRef(await api.listFiles(), a.ref);
      if (!entry) return;
      if (isDesktop()) {
        const r = await api.saveFileToDownloads(entry.id);
        if (r.ok) {
          setDoneId(a.id);
          window.setTimeout(() => setDoneId((d) => (d === a.id ? null : d)), 2500);
        }
      } else {
        await api.downloadFile(entry.id, entry.name);
      }
    } catch {
      /* sidecar hiccup — ignore */
    } finally {
      setBusyId(null);
    }
  }

  // Open the file with the OS default application (Preview, Excel, etc.).
  // Works in both desktop and dev modes — the sidecar runs on the same host.
  async function openExternalArtifact(e: React.MouseEvent, a: (typeof items)[number]) {
    e.stopPropagation();
    if ((a.kind !== "file" && a.kind !== "image") || !a.ref || busyId) return;
    setBusyId(a.id);
    try {
      const entry = matchByRef(await api.listFiles(), a.ref);
      if (!entry) return;
      await api.openFileExternal(entry.id);
    } catch {
      /* sidecar hiccup — ignore */
    } finally {
      setBusyId(null);
    }
  }

  const confirmDelete = () => {
    if (deleteTarget) void g.removeArtifact(deleteTarget.id);
    setDeleteTarget(null);
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Boxes className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">{t("title")}</span>
        <span className="ml-2 rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{items.length}</span>
      </div>
      <div
        className="flex-1 space-y-1 overflow-y-auto px-3"
        onScroll={() => {
          clearTimers();
          if (!inspectorEditing) setHover(null);
        }}
      >
        {items.length === 0 && (
          <div className="px-1 py-6 text-center text-xs text-faint">{t("empty")}</div>
        )}
        {items.map((a) => {
          const Ic = ICON[a.kind] || FileText;
          const clickable = (a.kind === "file" || a.kind === "image") && !!a.ref;
          return (
            <div
              key={a.id}
              onClick={(e) => {
                if (clickable) {
                  setHover(null);
                  void openArtifact(a);
                }
                e.stopPropagation();
              }}
              onMouseEnter={(e) => scheduleOpen(a, e.currentTarget)}
              onMouseLeave={scheduleClose}
              title={clickable ? t("clickPreview") : t("hoverMeta")}
              className={`group flex items-center gap-2 rounded-lg px-2 py-2 text-sm transition-colors ${
                flashing.has(a.id) ? "bg-violet/15 ring-1 ring-violet/40" : "hover:bg-card/50"
              } ${clickable ? "cursor-pointer" : ""}`}
            >
              <Ic className="h-4 w-4 shrink-0 text-violet" />
              <span className="truncate text-txt">{a.name}</span>
              {a.ref && <span className="truncate text-xs text-faint">{a.ref}</span>}
              <span className="ml-auto flex shrink-0 items-center gap-0.5">
                {clickable && (
                  <button
                    onClick={(e) => void openExternalArtifact(e, a)}
                    title={t("openExternal")}
                    className={`rounded-md p-1 text-muted hover:bg-card2 hover:text-txt ${
                      busyId === a.id ? "visible" : "invisible group-hover:visible"
                    }`}
                  >
                    {busyId === a.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : (
                      <ExternalLink className="h-3.5 w-3.5" />
                    )}
                  </button>
                )}
                {clickable && (
                  <button
                    onClick={(e) => void downloadArtifact(e, a)}
                    title={t("download")}
                    className={`rounded-md p-1 text-muted hover:bg-card2 hover:text-txt ${
                      doneId === a.id
                        ? "visible"
                        : "invisible group-hover:visible"
                    }`}
                  >
                    {doneId === a.id ? (
                      <Check className="h-3.5 w-3.5 text-emerald-400" />
                    ) : (
                      <Download className="h-3.5 w-3.5" />
                    )}
                  </button>
                )}
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    setHover(null);
                    setDeleteTarget(a);
                  }}
                  aria-label={t("deleteAria", { name: a.name })}
                  title={t("removeHint")}
                  className="rounded-md p-1 text-muted hover:bg-card2 hover:text-red invisible group-hover:visible"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </span>
            </div>
          );
        })}
      </div>

      {hover && (() => {
        // Always render against the LIVE store record, not the snapshot taken
        // on hover — otherwise edits (rename etc.) wouldn't show until close.
        // If it was deleted while open, drop the popover.
        const live = items.find((x) => x.id === hover.a.id);
        if (!live) return null;
        return (
          <div onMouseEnter={cancelClose} onMouseLeave={scheduleClose}>
            <ArtifactMetaCard
              artifact={live}
              rect={hover.rect}
              editing={inspectorEditing}
              setEditing={setInspectorEditing}
              onClose={() => {
                setInspectorEditing(false);
                setHover(null);
              }}
            />
          </div>
        );
      })()}

      {deleteTarget && (
        <ConfirmModal
          title={t("removeTitle")}
          message={t("removeMessage", { name: deleteTarget.name })}
          confirmLabel={t("remove")}
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
