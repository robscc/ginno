"use client";

import { useEffect, useRef, useState } from "react";
import {
  AlertTriangle,
  Boxes,
  Check,
  Copy,
  Download,
  FileSpreadsheet,
  FileText,
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

const ICON: Record<string, typeof FileText> = {
  workflow: Workflow,
  link: Link2,
  doc: FileText,
  file: FileSpreadsheet,
};

const KIND_LABEL: Record<string, string> = {
  file: "文件",
  doc: "文档",
  link: "链接",
  workflow: "工作流",
};

// Registry file kinds — the classification that steers prompt tool guidance
// (analyze_table for spreadsheet/table, parse_document otherwise).
const FILE_KINDS = ["spreadsheet", "table", "document", "presentation", "pdf", "data", "text"];
const FILE_KIND_LABEL: Record<string, string> = {
  spreadsheet: "Excel 表格",
  table: "CSV 表格",
  document: "文档",
  presentation: "演示文稿",
  pdf: "PDF",
  data: "结构化数据",
  text: "纯文本",
  unknown: "未知",
};

function relTime(ts: number): string {
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return `${Math.floor(d / 60)} 分钟前`;
  if (d < 86400) return `${Math.floor(d / 3600)} 小时前`;
  if (d < 86400 * 30) return `${Math.floor(d / 86400)} 天前`;
  return new Date(ts * 1000).toLocaleDateString();
}

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

interface Rect {
  top: number;
  bottom: number;
  left: number;
}

/** Hover inspector for one artifact. Shows exactly what prompt injection
 *  would use (schema summary + provenance), and lets the user correct
 *  name / kinds / the summary — corrections persist and win over recomputation. */
function ArtifactMetaCard({
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
  const [meta, setMeta] = useState<ArtifactMeta | null>(null);
  const [loadError, setLoadError] = useState("");
  // edit draft + status
  const [draft, setDraft] = useState({ name: "", kind: "", schema: "", file_kind: "" });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState("");
  const [savedFlash, setSavedFlash] = useState(false);
  const [copied, setCopied] = useState(false);

  async function load() {
    setLoadError("");
    try {
      const m = await api.getArtifactMetadata(artifact.id);
      if (!m.ok) throw new Error(m.error || "加载失败");
      setMeta(m);
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : "运行时未就绪");
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
      setSaveError(r.error || "保存失败");
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
  const isFile = artifact.kind === "file";

  return (
    <div
      className="meta-pop-in fixed z-50 flex flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-2xl"
      style={{ ...posStyle, width: maxW }}
      role="dialog"
      aria-label={`Artifact 元数据：${artifact.name}`}
    >
      {/* header */}
      <div className="flex items-center gap-2 border-b border-line px-3.5 py-2.5">
        <Ic className="h-4 w-4 shrink-0 text-violet" />
        {editing ? (
          <input
            value={draft.name}
            onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))}
            className="min-w-0 flex-1 rounded border border-line2 bg-base/60 px-1.5 py-0.5 text-sm text-txt outline-none focus:border-violet"
            placeholder="名称（必填）"
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
              {Object.entries(KIND_LABEL).map(([k, v]) => (
                <option key={k} value={k} className="bg-panel text-txt">
                  {v}
                </option>
              ))}
            </select>
          ) : (
            KIND_LABEL[artifact.kind] || artifact.kind
          )}
        </span>
        {savedFlash && <Check className="h-3.5 w-3.5 shrink-0 text-emerald-400" />}
        {!editing && (
          <button
            onClick={enterEdit}
            disabled={!meta}
            title="修改元数据"
            className="shrink-0 rounded-md p-1 text-muted hover:bg-card2 hover:text-txt disabled:opacity-40"
          >
            <Pencil className="h-3.5 w-3.5" />
          </button>
        )}
        <button
          onClick={onClose}
          title="关闭"
          className="shrink-0 rounded-md p-1 text-muted hover:bg-card2 hover:text-txt"
        >
          <X className="h-3.5 w-3.5" />
        </button>
      </div>

      <div className="flex-1 space-y-3 overflow-y-auto px-3.5 py-3">
        {loadError && <div className="text-xs text-red">元数据加载失败：{loadError}</div>}
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
                磁盘上找不到该文件（可能已被移动或删除），预览与注入将不可用。
              </div>
            )}

            <div className="space-y-1.5">
              <MetaRow label="路径">
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
                      title="复制路径"
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
                  <MetaRow label="大小">
                    <span className="text-[11px] text-muted">
                      {fmtBytes(meta.file.size)}
                      {meta.file.mime ? ` · ${meta.file.mime}` : ""}
                    </span>
                  </MetaRow>
                  <MetaRow label="识别类型">
                    {editing ? (
                      <select
                        value={draft.file_kind}
                        onChange={(e) => setDraft((d) => ({ ...d, file_kind: e.target.value }))}
                        className="rounded border border-line2 bg-base/60 px-1 py-0.5 text-[11px] text-txt outline-none focus:border-violet"
                      >
                        {FILE_KINDS.map((k) => (
                          <option key={k} value={k} className="bg-panel text-txt">
                            {FILE_KIND_LABEL[k] || k}
                          </option>
                        ))}
                      </select>
                    ) : (
                      <span className="text-[11px] text-muted">
                        {FILE_KIND_LABEL[meta.file.kind] || meta.file.kind || "—"}
                      </span>
                    )}
                  </MetaRow>
                </>
              )}
              <MetaRow label="会话">
                <span className="font-mono text-[11px] text-muted">
                  {artifact.session_id ? artifact.session_id.slice(0, 8) : "—"}
                </span>
              </MetaRow>
              <MetaRow label="登记于">
                <span className="text-[11px] text-muted">{relTime(artifact.created)}</span>
              </MetaRow>
            </div>

            {/* schema summary — what actually lands in the model context */}
            <div className="rounded-lg border border-line bg-card/50">
              <div className="flex items-center gap-2 border-b border-line px-2.5 py-1.5">
                <span className="text-[11px] font-medium text-txt">Schema 摘要</span>
                {meta.schema ? (
                  <span
                    className={`rounded-full px-1.5 py-px text-[10px] ${
                      meta.schema_source === "override"
                        ? "bg-emerald-500/15 text-emerald-400"
                        : "bg-card2 text-faint"
                    }`}
                  >
                    {meta.schema_source === "override" ? "已人工修正" : "自动计算"}
                  </span>
                ) : (
                  <span className="rounded-full bg-card2 px-1.5 py-px text-[10px] text-faint">无</span>
                )}
              </div>
              {editing ? (
                <textarea
                  value={draft.schema}
                  onChange={(e) => setDraft((d) => ({ ...d, schema: e.target.value }))}
                  rows={5}
                  placeholder="留空则恢复自动计算"
                  className="w-full resize-y bg-transparent px-2.5 py-2 font-mono text-[11px] leading-relaxed text-txt outline-none"
                />
              ) : (
                <div className="max-h-32 overflow-y-auto px-2.5 py-2">
                  {meta.schema ? (
                    <pre className="whitespace-pre-wrap break-all font-mono text-[11px] leading-relaxed text-muted">
                      {meta.schema}
                    </pre>
                  ) : (
                    <span className="text-[11px] text-faint">
                      （非表格文件，或解析未产出摘要）
                    </span>
                  )}
                </div>
              )}
              <div className="border-t border-line px-2.5 py-1.5 text-[10px] leading-snug text-faint">
                {editing
                  ? "保存后，后续会话附加此文件时将注入这份修正版（留空恢复自动计算）。"
                  : "附加此文件对话时，这段摘要会注入模型上下文——在这里核对并修正它。"}
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
              取消
            </button>
            <button
              onClick={() => void save()}
              disabled={saving || !dirty}
              title={!dirty ? "没有改动" : undefined}
              className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-40"
            >
              {saving && <Loader2 className="h-3 w-3 animate-spin" />}
              保存
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function ArtifactsPanel() {
  const g = useGinno();
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
    if (a.kind !== "file" || !a.ref) return;
    try {
      const entry = matchByRef(await api.listFiles(), a.ref);
      if (entry) {
        g.openPreview({ id: entry.id, name: entry.name, path: entry.path, kind: entry.kind });
      }
    } catch {
      /* sidecar hiccup — ignore */
    }
  }

  // Save a copy of the file into the OS Downloads folder (desktop) or trigger
  // a browser download (dev). Independent of opening the preview.
  async function downloadArtifact(e: React.MouseEvent, a: (typeof items)[number]) {
    e.stopPropagation();
    if (a.kind !== "file" || !a.ref || busyId) return;
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

  const confirmDelete = () => {
    if (deleteTarget) void g.removeArtifact(deleteTarget.id);
    setDeleteTarget(null);
  };

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center px-4 pb-2 pt-4">
        <Boxes className="mr-2 h-4 w-4 text-muted" />
        <span className="text-sm font-semibold text-txt">Artifacts</span>
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
          <div className="px-1 py-6 text-center text-xs text-faint">
            No artifacts yet. Files / docs / workflows you produce or attach show up here.
          </div>
        )}
        {items.map((a) => {
          const Ic = ICON[a.kind] || FileText;
          const clickable = a.kind === "file" && !!a.ref;
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
              title={clickable ? "点击预览 · 悬停查看元数据" : "悬停查看元数据"}
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
                    onClick={(e) => void downloadArtifact(e, a)}
                    title="下载到 Downloads"
                    className={`rounded-md p-1 text-muted hover:bg-card2 hover:text-txt ${
                      busyId === a.id || doneId === a.id
                        ? "visible"
                        : "invisible group-hover:visible"
                    }`}
                  >
                    {busyId === a.id ? (
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                    ) : doneId === a.id ? (
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
                  aria-label={`删除 ${a.name}`}
                  title="从面板移除（不删除磁盘文件）"
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
          title="移除 Artifact"
          message={`确定从 Artifacts 面板移除「${deleteTarget.name}」？这只是移除面板中的引用，磁盘上的文件不会被删除，随时可以重新附加。`}
          confirmLabel="移除"
          onConfirm={confirmDelete}
          onCancel={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
