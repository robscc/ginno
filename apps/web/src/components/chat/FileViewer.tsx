"use client";

import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { ChevronLeft, ChevronRight, Download, FileDown, FileX2, Loader2, RefreshCw, X } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import type { FilePreview } from "@/lib/types";
import type { PreviewFile } from "@/lib/store";
import { Markdown } from "./Markdown";
import { useEscClose } from "@/lib/useEscClose";

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

/** Split a drawio document into per-page shells.
 *
 *  Multi-page files are `<mxfile><diagram name="Page-1">…</diagram>…</mxfile>`;
 *  GraphViewer renders only the first page when fed the whole mxfile (the
 *  "only the first page" bug). We parse once, list the page names, and for
 *  each page build a shell whose mxfile keeps only that `<diagram>` child
 *  (compressed diagram payloads stay untouched — we never decode content,
 *  just drop sibling nodes, so the vendored viewer handles the rest).
 *  Uncompressed `<mxGraphModel>` roots pass through unchanged (single page).
 */
function splitDrawioPages(xml: string): { name: string; shell: string }[] {
  const doc = new DOMParser().parseFromString(xml, "application/xml");
  const file = doc.documentElement;
  if (!file || file.nodeName === "parsererror") return [{ name: "", shell: buildDrawioShell(xml) }];
  const diagrams = Array.from(file.children).filter((c) => c.nodeName === "diagram");
  if (file.nodeName !== "mxfile" || diagrams.length <= 1)
    return [{ name: "", shell: buildDrawioShell(xml) }];
  return diagrams.map((d, i) => {
    const clone = file.cloneNode(false) as Element;
    clone.appendChild(d.cloneNode(true));
    return {
      name: d.getAttribute("name") || `Page-${i + 1}`,
      shell: buildDrawioShell(
        `<?xml version="1.0" encoding="UTF-8"?>${new XMLSerializer().serializeToString(clone)}`,
      ),
    };
  });
}

/** .drawio preview: fetch the raw XML via the download URL and render it
 *  with the embedded GraphViewer inside a sandboxed srcdoc iframe. */
function DrawioFrame({ file, onError }: { file: PreviewFile; onError: () => void }) {
  const t = useTranslations("composer");
  const [doc, setDoc] = useState<string | null>(null);
  // 多页 drawio：每页一个 shell + 页签切换（单页文件 pages.length === 1
  // 且 name 为空 → 不渲染页签，行为与之前完全一致）。
  const [pages, setPages] = useState<{ name: string; shell: string }[]>([]);
  const [page, setPage] = useState(0);
  useLayoutEffect(() => {
    let alive = true;
    setDoc(null);
    setPages([]);
    setPage(0);
    // mtime busts the cache when the same-named diagram is regenerated.
    fetch(`${api.fileDownloadUrl(file.id)}?t=${file.mtime ?? 0}`)
      .then((r) => (r.ok ? r.text() : Promise.reject(new Error(String(r.status)))))
      .then((xml) => {
        if (!alive) return;
        const ps = splitDrawioPages(xml);
        if (ps.length > 1) setPages(ps);
        else setDoc(ps[0].shell);
      })
      .catch(() => {
        if (alive) onError();
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [file.id, file.mtime]);
  if (pages.length > 1) {
    const cur = pages[Math.min(page, pages.length - 1)];
    // 页签名剥常见编号前缀（"2-模块级关系"→"模块级关系"），title 全名。
    const short = (n: string) => n.replace(/^\d+[-._、]\s*/, "");
    const onKeyDown = (e: React.KeyboardEvent) => {
      if (e.key === "ArrowLeft") setPage((p) => Math.max(0, p - 1));
      else if (e.key === "ArrowRight") setPage((p) => Math.min(pages.length - 1, p + 1));
    };
    return (
      <div className="flex h-full min-h-[60vh] flex-col" onKeyDown={onKeyDown} tabIndex={0}>
        {/* 分段药丸页签条：自带底色（不依赖画布白底，深浅主题均可读）；
            长名截断，>6 页横向滚动 + 固定计数；←/→ 键翻页。 */}
        <div className="flex items-center gap-2 bg-card2 px-3 py-2">
          <div
            className="flex min-w-0 flex-1 items-center gap-1 overflow-x-auto"
            style={{ scrollbarWidth: "none" }}
          >
            {pages.map((p, i) => (
              <button
                key={i}
                onClick={() => setPage(i)}
                title={p.name}
                className={
                  "shrink-0 rounded-full px-3 py-1 text-xs transition-colors " +
                  (i === page
                    ? // 选中态：accent 半透明底 + accent 描边/文字（不用白字，
                      // 浅色 accent 主题下白字不可读，截图实测）。
                      "bg-accent/15 font-medium text-accent ring-1 ring-accent"
                    : "bg-line2/60 text-txt hover:bg-line2")
                }
              >
                <span className="block max-w-32 truncate">{short(p.name)}</span>
              </button>
            ))}
          </div>
          <span className="shrink-0 text-xs tabular-nums text-faint">
            {page + 1}/{pages.length}
          </span>
        </div>
        <iframe
          srcDoc={cur.shell}
          title={`${file.name} — ${cur.name}`}
          sandbox="allow-scripts"
          className="min-h-[60vh] w-full flex-1 border-0 bg-white"
        />
      </div>
    );
  }
  if (!doc) return <div className="px-4 py-6 text-center text-sm text-faint">{t("sheet.loading")}</div>;
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
  const { previewFile, previewNonce, closePreview } = useGinno();
  if (!previewFile) return null;
  return (
    <OpenFileViewer
      key={previewFile.id}
      file={previewFile}
      previewNonce={previewNonce}
      closePreview={closePreview}
    />
  );
}

function OpenFileViewer({ file, previewNonce, closePreview }: {
  file: PreviewFile;
  previewNonce: number;
  closePreview: () => void;
}) {
  const t = useTranslations("composer");
  const { id, kind } = file;
  const requestId = useRef(0);
  const invalidate = useCallback(() => { requestId.current++; }, []);
  const [refresh, setRefresh] = useState(0);
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

  // 预览打开时按 Esc 关闭（统一 hook；与页签 ←/→ 翻页不冲突）。
  const close = useCallback(() => {
    invalidate();
    closePreview();
  }, [closePreview, invalidate]);
  useEscClose(close);

  useEffect(() => () => {
    if (noteTimer.current) window.clearTimeout(noteTimer.current);
  }, []);

  function flashNote(msg: string) {
    setNote(msg);
    if (noteTimer.current) window.clearTimeout(noteTimer.current);
    noteTimer.current = window.setTimeout(() => setNote(null), 5000);
  }

  // Download the original file (fmt=raw) or export the visible sheet as CSV.
  // Desktop (WKWebView can't trigger downloads): sidecar copies into
  // ~/Downloads; browser: native blob download.
  async function save(fmt: "raw" | "csv") {
    if (busy) return;
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

  const isImage = kind === "image";
  const isHtml = kind === "html" || /\.html?$/i.test(file.name);
  const isDrawio = kind === "drawio" || /\.(drawio|dio)$/i.test(file.name);
  const rawKind = isImage ? "image" : isHtml ? "html" : isDrawio ? "drawio" : null;
  const rawVersion = `${previewNonce}:${refresh}:${file.mtime ?? 0}`;

  // Revoke ownership in the same commit that changes the selection or removes
  // the viewer. Until the next page arrives, keep only the table navigation.
  useLayoutEffect(() => {
    const request = ++requestId.current;
    setErr(null);
    setMissing(false);
    if (rawKind) {
      setPv(rawKind === "image" ? null : { ok: true, kind: rawKind });
      setLoading(false);
      return;
    }

    setLoading(true);
    async function load() {
      try {
        const result = await api.getFilePreview(id, { sheet, offset, limit: PAGE });
        if (request !== requestId.current) return;
        if (result.ok) {
          setPv(result);
        } else if (isMissingPreviewError(result.error)) {
          setPv(null);
          setMissing(true);
          notifyFileMissing(id);
        } else {
          setPv(null);
          setErr(result.error || t("sheet.previewFailed"));
        }
      } catch {
        if (request === requestId.current) {
          setPv(null);
          setErr(t("sheet.runtimeUnreachable"));
        }
      } finally {
        if (request === requestId.current) setLoading(false);
      }
    }
    void load();
    return invalidate;
  }, [id, rawKind, sheet, offset, previewNonce, refresh, t, invalidate]);

  function selectSheet(next: string) {
    if (next === (sheet ?? pv?.sheet)) return;
    invalidate();
    setSheet(next);
    setOffset(0);
    setNote(null);
  }

  function changePage(next: number) {
    invalidate();
    setOffset(next);
  }

  const isTable = pv?.kind === "spreadsheet" || pv?.kind === "table";
  const showPayload = !loading && !missing && !err;
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
  const selectedSheet = sheet ?? pv?.sheet;
  const total = pv?.sheets?.find((item) => item.name === selectedSheet)?.rows ?? pv?.total_rows ?? 0;
  const shown = pv?.rows?.length ?? 0;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-6"
      onClick={close}
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
          {isTable && showPayload && (
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
              onClick={() => {
                invalidate();
                setRefresh((version) => version + 1);
              }}
              title={t("sheet.refresh")}
              className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button
              onClick={close}
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
                onClick={() => selectSheet(s.name)}
                className={`whitespace-nowrap rounded-lg px-2.5 py-1 text-xs ${
                  selectedSheet === s.name
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
          {loading && !rawKind && (
            <div className="px-4 py-6 text-center text-sm text-faint">{t("sheet.loading")}</div>
          )}
          {isDrawio && !missing && !err && (
            <DrawioFrame
              key={rawVersion}
              file={file}
              onError={() => {
                setMissing(true);
                notifyFileMissing(file.id);
              }}
            />
          )}
          {isHtml && !missing && (
            <iframe
              key={rawVersion}
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
                key={rawVersion}
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
          {pv && isTable && showPayload && (
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
          {pv && !isTable && showPayload && pv.markdown !== undefined && (
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
              onClick={() => changePage(offset - PAGE)}
              className="flex items-center gap-1 rounded-lg px-2 py-1 hover:bg-card disabled:opacity-40"
            >
              <ChevronLeft className="h-3.5 w-3.5" /> {t("sheet.previous")}
            </button>
            <span>
              {Math.floor(offset / PAGE) + 1} / {Math.ceil(total / PAGE)}
            </span>
            <button
              disabled={offset + PAGE >= total}
              onClick={() => changePage(offset + PAGE)}
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
