"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Download, FileDown, Loader2, RefreshCw, X } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import type { FilePreview } from "@/lib/types";
import { Markdown } from "./Markdown";

const PAGE = 100;

/**
 * Full-width modal preview for attached/produced files (docs §7.2).
 * Tables → paginated grid with sheet tabs + dtype badges; documents →
 * extracted markdown. Refetches whenever the store's previewNonce bumps
 * (preview.invalidate for the open file, or a fresh open).
 */
export function SheetViewer() {
  const g = useGinno();
  const file = g.previewFile;
  const [pv, setPv] = useState<FilePreview | null>(null);
  const [sheet, setSheet] = useState<string | undefined>(undefined);
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState<string | null>(null);
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
        flashNote(r.ok ? `已保存到 ${r.path}` : (r.error ?? "保存失败"));
      } else {
        const r = await api.downloadFile(file.id, file.name, opts);
        flashNote(r.ok ? "已开始下载" : (r.error ?? "下载失败"));
      }
    } finally {
      setBusy(null);
    }
  }

  const load = useCallback(async () => {
    if (!file) return;
    setLoading(true);
    setErr(null);
    try {
      const r = await api.getFilePreview(file.id, { sheet, offset, limit: PAGE });
      if (!r.ok) setErr(r.error || "预览失败");
      else setPv(r);
    } catch {
      setErr("无法连接运行时");
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
  }, [file?.id, sheet]);

  if (!file) return null;

  const isTable = pv?.kind === "spreadsheet" || pv?.kind === "table";
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
          {pv?.kind && (
            <span className="rounded-full bg-card2 px-2 py-0.5 text-[11px] text-muted">{pv.kind}</span>
          )}
          {isTable && (
            <span className="text-xs text-faint">
              {total > 0 ? `${offset + 1}–${offset + shown} / ${total} 行` : "空表"}
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
              title="下载原文件"
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
                title="将当前 sheet 导出为 CSV"
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
              title="刷新"
              className="rounded-lg p-1.5 text-muted hover:bg-card hover:text-txt"
            >
              <RefreshCw className={`h-4 w-4 ${loading ? "animate-spin" : ""}`} />
            </button>
            <button
              onClick={() => g.closePreview()}
              title="关闭"
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
          {err && <div className="px-4 py-6 text-center text-sm text-red-400">{err}</div>}
          {!err && !pv && <div className="px-4 py-6 text-center text-sm text-faint">加载中…</div>}
          {pv && isTable && (
            <table className="w-full border-collapse text-xs">
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
              <Markdown text={pv.markdown || "_(空文档)_"} />
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
              <ChevronLeft className="h-3.5 w-3.5" /> 上一页
            </button>
            <span>
              {Math.floor(offset / PAGE) + 1} / {Math.ceil(total / PAGE)}
            </span>
            <button
              disabled={offset + PAGE >= total}
              onClick={() => setOffset((o) => o + PAGE)}
              className="flex items-center gap-1 rounded-lg px-2 py-1 hover:bg-card disabled:opacity-40"
            >
              下一页 <ChevronRight className="h-3.5 w-3.5" />
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
