"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronLeft, ChevronRight, Download, FileDown, FileX2, Loader2, RefreshCw, X } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import type { FilePreview } from "@/lib/types";
import type { PreviewFile } from "@/lib/store";
import { Markdown } from "./Markdown";

const PAGE = 100;

/** P3-2: fired when a preview/download discovers the file is gone. The
 *  Artifacts panel listens and marks that row 「已失效」 without a red error. */
export const FILE_MISSING_EVENT = "ginno:file-missing";

function notifyFileMissing(fileId: string) {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(FILE_MISSING_EVENT, { detail: { fileId } }));
}

/** Backend 404 bodies for a missing file: `file not found: <id>` (registry
 *  entry gone) or `File has been moved or deleted` (path gone). Anything else
 *  is a real preview failure and keeps the red error. */
function isMissingPreviewError(msg?: string | null): boolean {
  if (!msg) return false;
  const m = msg.toLowerCase();
  return (
    m.includes("file not found") ||
    m.includes("moved or deleted") ||
    m.includes("no such file") ||
    m.includes("文件不存在") ||
    m.includes("找不到该文件")
  );
}

/** Build the self-contained host document for the vendored diagrams.net
 *  GraphViewer. The drawio XML rides in the data-mxgraph attribute (JSON),
 *  HTML-escaped for a double-quoted attribute; the script is served from
 *  the web bundle (/vendor — srcdoc iframes inherit the parent base URL),
 *  so preview works fully offline, no CDN dependency. */
function buildDrawioShell(xml: string): string {
  const cfg = JSON.stringify({
    highlight: "none",
    nav: true,
    resize: true,
    "auto-fit": true,
    toolbar: "zoom layers",
    xml,
  });
  const attr = cfg.replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;");
  return `<!doctype html><html><head><meta charset="utf-8"><style>html,body{margin:0;height:100%;background:#fff}.mxgraph{width:100%;height:100%}</style></head><body><div class="mxgraph" data-mxgraph="${attr}"></div><script src="/vendor/viewer-static.min.js"></script></body></html>`;
}

/** .drawio preview: fetch the raw XML via the download URL and render it
 *  with the embedded GraphViewer inside a sandboxed srcdoc iframe. */
function DrawioFrame({ file, onError }: { file: PreviewFile; onError: () => void }) {
  const [doc, setDoc] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setDoc(null);
    // mtime busts the cache when the same-named diagram is regenerated.
    fetch(`${api.fileDownloadUrl(file.id)}?t=${file.mtime ?? 0}`)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((xml) => {
        if (alive) setDoc(buildDrawioShell(xml));
      })
      .catch(() => {
        if (alive) onError();
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file.id, file.mtime]);
  if (!doc) return <div className="px-4 py-6 text-center text-sm text-faint">Loading…</div>;
  return (
    <iframe
      srcDoc={doc}
      title={file.name}
      sandbox="allow-scripts"
      className="h-full min-h-[60vh] w-full border-0 bg-white"
    />
  );
}

/**
 * Full-width modal preview for attached/produced files (docs §7.2).
 * Tables → paginated grid with sheet tabs + dtype badges; documents →
 * extracted markdown. Refetches whenever the store's previewNonce bumps
 * (preview.invalidate for the open file, or a fresh open).
 */
export function FileViewer() {
  const g = useGinno();
  // composer 域 catalog（本组件挂 AppShell 下，文案统一收在 composer.sheet）
  const t = useTranslations("composer");
  const file = g.previewFile;
  const [pv, setPv] = useState<FilePreview | null>(null);
  const [sheet, setSheet] = useState<string | undefined>(undefined);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  // P3-2: file vanished on disk → neutral placeholder instead of a red error.
  const [missing, setMissing] = useState(false);
  const [busy, setBusy] = useState<"raw" | "csv" | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const noteTimer = useRef<number | null>(null);

  function flashNote(msg: string) {
    setNote(msg);
    if (noteTimer.current) window.clearTimeout(noteTimer.current);
    noteTimer.current = window.setTimeout(() => setNote(null), 5000);
  }

  // Download the original file (fmt=raw) or export the visible sheet as CSV.
  // Desktop (WKWebView can't trigger downloads): sidecar copies into
  // ~/Downloads; browser: native blob download.
  async function save(fmt: "raw" | "csv") {
    if (!file || busy) return;
    const opts = { fmt, sheet: fmt === "csv" ? (sheet ?? pv?.sheet) : undefined };
    setBusy(fmt);
    try {
      if (isDesktop()) {
        const r = await api.saveFileToDownloads(file.id, opts);
        // path 理论上必有（ok 时），缺 path 也按失败提示而非渲染 "undefined"
        flashNote(
          r.ok && r.path
            ? t("sheet.savedTo", { path: r.path })
            : (r.error ?? t("sheet.saveFailed")),
        );
      } else {
        const r = await api.downloadFile(file.id, file.name, opts);
        flashNote(r.ok ? t("sheet.downloadStarted") : (r.error ?? t("sheet.downloadFailed")));
      }
    } finally {
      setBusy(null);
    }
  }

  const load = useCallback(async () => {
    if (!file) return;
    // Images are served raw by the sidecar (no extraction step) — the body
    // renders them directly from the download URL, so skip the preview fetch.
    if (file.kind === "image" || file.kind === "html" || file.kind === "drawio") {
      setErr(null);
      setPv(
        file.kind === "html" || file.kind === "drawio"
          ? ({ ok: true, kind: file.kind } as FilePreview)
          : null,
      );
      setMissing(false);
      return;
    }
    setLoading(true);
    setErr(null);
    // Reset the previous file's payload — switching targets must not leave
    // the old document rendered below the new file's error/placeholder
    // (seen with .md → .html, 2026-11-04).
    setPv(null);
    try {
      const r = await api.getFilePreview(file.id, { sheet, offset, limit: PAGE });
      if (!r.ok) {
        if (isMissingPreviewError(r.error)) {
          setPv(null);
          setMissing(true);
          notifyFileMissing(file.id);
        } else {
          setErr(r.error || t("sheet.previewFailed"));
        }
      } else {
        setPv(r);
        setMissing(false);
      }
    } catch {
      setErr(t("sheet.runtimeUnreachable"));
    } finally {
      setLoading(false);
    }
    // g.previewNonce is an intentional refetch trigger (preview.invalidate for
    // the open file, or a fresh auto-open) — not read in the body on purpose.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file, sheet, offset, g.previewNonce]);

  useEffect(() => {
    void load();
  }, [load]);

  // reset paging when the target file or sheet changes
  useEffect(() => {
    setOffset(0);
    setNote(null);
    setMissing(false);
  }, [file?.id, sheet]);

  if (!file) return null;

  const isTable = pv?.kind === "spreadsheet" || pv?.kind === "table";
  const isImage = file?.kind === "image";
  const isHtml = file?.kind === "html" || /\.html?$/i.test(file?.name ?? "");
  // .drawio sources render via the embedded GraphViewer (see DrawioFrame);
  // the name-pattern fallback covers registry rows classified before the
  // drawio kind existed.
  const isDrawio =
    file?.kind === "drawio" || /\.(drawio|dio)$/i.test(file?.name ?? "");
  // 服务端数据徽标兜底词：pv.kind 分类法值 → 本地化徽标（composer.badges.fileKind）；
  // 未知值仍回退英文原值（服务端新增 kind 无需前端同步）。
  const FILE_KINDS = [
    "spreadsheet",
    "table",
    "document",
    "presentation",
    "pdf",
    "data",
    "text",
    "image",
    "html",
    "drawio",
    "unknown",
  ] as const;
  const fileKindBadge = (kind: string): string =>
    (FILE_KINDS as readonly string[]).includes(kind)
      ? t(`badges.fileKind.${kind as (typeof FILE_KINDS)[number]}`)
      : kind;
  const total = pv?.total_rows ?? 0;
  const shown = pv?.rows?.length ?? 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
      onClick={() => g.closePreview()}
    >
      <div
        className="flex h-[85vh] w-[90vw] max-w-6xl flex-col overflow-hidden rounded-xl border border-line bg-panel shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        {/* header */}
        <div className="flex items-center gap-2 border-b border-line px-4 py-3">
          <span className="truncate text-sm font-semibold text-txt">{file.name}</span>
          {(pv?.kind || (isImage ? "image" : "")) && (
            <span className="rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">
              {fileKindBadge(pv?.kind || "image")}
            </span>
          )}
          {isTable && (
            <span className="text-xs text-faint">
              {total > 0
                ? t("sheet.rowsRange", { from: offset + 1, to: offset + shown, total })
                : t("sheet.emptySheet")}
            </span>
          )}
          <div className="ml-auto flex items-center gap-1">
            {note && (
              <span className="mr-1 max-w-[280px] truncate text-xs text-faint" title={note}>
                {note}
              </span>
            )}
            <button
              onClick={() => void save("raw")}
              disabled={busy !== null}
              title={t("sheet.downloadOriginal")}
              className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt disabled:opacity-40"
            >
              {busy === "raw" ? (
                <Loader2 className="h-4 w-4 animate-spin" />
              ) : (
                <Download className="h-4 w-4" />
              )}
            </button>
            {isTable && (
              <button
                onClick={() => void save("csv")}
                disabled={busy !== null}
                title={t("sheet.exportCsv")}
                className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt disabled:opacity-40"
              >
                {busy === "csv" ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <FileDown className="h-4 w-4" />
                )}
              </button>
            )}
            <button
              onClick={() => void load()}
              title={t("sheet.refresh")}
              className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button
              onClick={() => g.closePreview()}
              title={t("sheet.close")}
              className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt"
            >
              <X className="h-4 w-4" />
            </button>
          </div>
        </div>

        {/* sheet tabs */}
        {isTable && (pv?.sheets?.length ?? 0) > 1 && (
          <div className="flex gap-1 overflow-x-auto border-b border-line px-3 py-2">
            {pv!.sheets!.map((s) => (
              <button
                key={s.name}
                onClick={() => setSheet(s.name)}
                className={`whitespace-nowrap rounded-lg px-2.5 py-1 text-xs ${
                  (sheet ?? pv!.sheet) === s.name
                    ? "bg-violet/20 text-txt"
                    : "text-muted hover:bg-card hover:text-txt"
                }`}
              >
                {s.name} <span className="text-faint">({s.rows}×{s.cols})</span>
              </button>
            ))}
          </div>
        )}

        {/* body */}
        <div className="min-h-0 flex-1 overflow-auto">
          {missing && (
            <div className="flex min-h-full flex-col items-center justify-center gap-2 px-5 py-10 text-center">
              <FileX2 className="h-9 w-9 text-faint" />
              <div className="text-sm text-muted">{t("sheet.fileDeleted")}</div>
              <div className="max-w-full truncate text-xs text-faint">{file.name}</div>
            </div>
          )}
          {!missing && err && <div className="selectable px-4 py-6 text-center text-sm text-red-400">{err}</div>}
          {!missing && !err && !pv && !isImage && !isHtml && !isDrawio && (
            <div className="px-4 py-6 text-center text-sm text-faint">{t("sheet.loading")}</div>
          )}
          {isDrawio && !missing && !err && (
            <DrawioFrame
              file={file}
              onError={() => {
                setMissing(true);
                notifyFileMissing(file.id);
              }}
            />
          )}
          {isHtml && !missing && (
            <iframe
              src={api.fileInlineUrl(file.id)}
              title={file.name}
              sandbox="allow-scripts"
              className="h-full min-h-[60vh] w-full border-0 bg-white"
            />
          )}
          {isImage && !missing && (
            <div className="flex min-h-full items-center justify-center px-5 py-4">
              {/* mtime busts the cache when a same-named image is regenerated */}
              <img
                src={`${api.fileDownloadUrl(file.id)}?t=${file.mtime ?? 0}`}
                alt={file.name}
                className="max-h-[75vh] max-w-full rounded-lg object-contain"
                onError={() => {
                  setMissing(true);
                  notifyFileMissing(file.id);
                }}
              />
            </div>
          )}
          {pv && isTable && (
            <table className="selectable w-full border-collapse text-xs">
              <thead className="sticky top-0 z-10 bg-card2">
                <tr>
                  <th className="border-b border-line px-2 py-1.5 text-left text-faint">#</th>
                  {(pv.columns ?? []).map((c) => (
                    <th key={c.name} className="border-b border-line px-2 py-1.5 text-left">
                      <div className="font-medium text-txt">{c.name}</div>
                      <div className="text-[10px] font-normal text-faint">{c.dtype}</div>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {(pv.rows ?? []).map((row, i) => (
                  <tr key={i} className="hover:bg-card/40">
                    <td className="border-b border-line/50 px-2 py-1 text-faint">{offset + i + 1}</td>
                    {row.map((cell, j) => (
                      <td key={j} className="max-w-[320px] truncate border-b border-line/50 px-2 py-1 text-txt">
                        {cell}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {pv && !isTable && pv.markdown !== undefined && (
            <div className="px-5 py-4">
              <Markdown text={pv.markdown || t("sheet.emptyDocument")} />
            </div>
          )}
        </div>

        {/* pagination footer */}
        {isTable && total > PAGE && (
          <div className="flex items-center justify-center gap-3 border-t border-line px-4 py-2 text-xs text-muted">
            <button
              disabled={offset === 0}
              onClick={() => setOffset((o) => Math.max(0, o - PAGE))}
              className="flex items-center gap-1 rounded-lg px-2 py-1 hover:bg-card disabled:opacity-40"
            >
              <ChevronLeft className="h-3.5 w-3.5" /> {t("sheet.previous")}
            </button>
            <span>
              {Math.floor(offset / PAGE) + 1} / {Math.ceil(total / PAGE)}
            </span>
            <button
              disabled={offset + PAGE >= total}
              onClick={() => setOffset((o) => o + PAGE)}
              className="flex items-center gap-1 rounded-lg px-2 py-1 hover:bg-card disabled:opacity-40"
            >
              {t("sheet.next")} <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
