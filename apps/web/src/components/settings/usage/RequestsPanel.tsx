"use client";

/** 请求日志：一次 LLM 调用一行。过滤（日期/Provider/来源/会话）+ 分页。
 * 只记计数与元数据，不记请求内容（设计 §4.4）。 */

import { useCallback, useEffect, useState } from "react";
import { X } from "lucide-react";
import * as api from "@/lib/runtime";
import type { UsageRequests } from "@/lib/types";
import { fmt } from "./charts";

const SRC_STYLE: Record<string, { bg: string; fg: string }> = {
  chat: { bg: "#6366f122", fg: "#8d90f8" },
  goal: { bg: "#8b5cf622", fg: "#b39df9" },
  compaction: { bg: "#ca8a0422", fg: "#d9a93e" },
  workflow: { bg: "#4ade8022", fg: "#4ade80" },
  memory: { bg: "#38bdf822", fg: "#38bdf8" },
  kb: { bg: "#60a5fa22", fg: "#60a5fa" },
  probe: { bg: "#62626e22", fg: "#9a9aa6" },
};

function SrcChip({ src }: { src: string }) {
  const s = SRC_STYLE[src] || SRC_STYLE.probe;
  return (
    <span className="rounded-md px-1.5 py-0.5 text-[11px]" style={{ background: s.bg, color: s.fg }}>
      {src}
    </span>
  );
}

function fmtClock(ts: number): string {
  const d = new Date(ts * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export function RequestsPanel({ sessionFilter, onClearSession }: { sessionFilter?: string; onClearSession?: () => void }) {
  const [date, setDate] = useState(""); // empty = today (server default)
  const [provider, setProvider] = useState("");
  const [source, setSource] = useState("");
  const [page, setPage] = useState(1);
  const [data, setData] = useState<UsageRequests | null>(null);
  const [loaded, setLoaded] = useState(false);
  const pageSize = 50;

  const load = useCallback(async () => {
    try {
      const r = await api.getUsageRequests({
        date: date || undefined,
        provider: provider || undefined,
        source: source || undefined,
        session_id: sessionFilter || undefined,
        page,
        page_size: pageSize,
      });
      setData(r);
    } catch {
      setData(null);
    } finally {
      setLoaded(true);
    }
  }, [date, provider, source, sessionFilter, page]);

  useEffect(() => {
    load();
  }, [load]);

  // filter changes reset pagination
  useEffect(() => {
    setPage(1);
  }, [date, provider, source, sessionFilter]);

  const pages = data ? Math.max(1, Math.ceil(data.total / pageSize)) : 1;
  const sel = "rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-txt outline-none";

  return (
    <div className="overflow-hidden rounded-xl border border-line bg-card">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-line px-3.5 py-3">
        <label className="flex items-center gap-1.5 text-[11.5px] text-faint">
          日期
          <input type="date" value={date} onChange={(e) => setDate(e.target.value)} className={sel} />
        </label>
        <label className="flex items-center gap-1.5 text-[11.5px] text-faint">
          Provider
          <select value={provider} onChange={(e) => setProvider(e.target.value)} className={sel}>
            <option value="">全部</option>
            <option value="anthropic">anthropic</option>
            <option value="openai">openai</option>
            <option value="custom">custom</option>
          </select>
        </label>
        <label className="flex items-center gap-1.5 text-[11.5px] text-faint">
          来源
          <select value={source} onChange={(e) => setSource(e.target.value)} className={sel}>
            <option value="">全部</option>
            {["chat", "goal", "compaction", "workflow", "memory", "kb", "probe"].map((s) => (
              <option key={s} value={s}>{s}</option>
            ))}
          </select>
        </label>
        {sessionFilter && (
          <span className="flex items-center gap-1.5 rounded-lg border border-line2 bg-card2 px-2.5 py-1.5 text-xs text-txt">
            会话: <code className="text-[11px] text-muted">{sessionFilter.slice(0, 8)}…</code>
            <button onClick={onClearSession} className="text-faint hover:text-txt" title="清除会话过滤">
              <X className="h-3 w-3" />
            </button>
          </span>
        )}
        <div className="flex-1" />
        <span className="text-[11.5px] text-faint">一次模型调用一行 · 仅计数与元数据，不含请求内容</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] border-collapse">
          <thead>
            <tr>
              {["时间", "会话", "来源", "Provider", "模型", "输入", "输出", "缓存读", "状态"].map((h, i) => (
                <th
                  key={h}
                  className={`whitespace-nowrap border-b border-line px-3 py-2 text-[11px] font-semibold tracking-wide text-faint ${i >= 5 && i <= 7 ? "text-right" : "text-left"}`}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {!loaded && (
              <tr><td colSpan={9} className="px-3 py-10 text-center text-xs text-faint">加载中…</td></tr>
            )}
            {loaded && (!data || data.rows.length === 0) && (
              <tr><td colSpan={9} className="px-3 py-10 text-center text-xs text-faint">没有符合过滤条件的请求</td></tr>
            )}
            {data?.rows.map((r, i) => (
              <tr key={`${r.ts}-${i}`} className="border-b border-white/5 last:border-b-0 hover:bg-white/5">
                <td className="px-3 py-2 text-[12.5px] tabular-nums text-faint">{fmtClock(r.ts)}</td>
                <td className="px-3 py-2 text-[12.5px] text-txt">
                  {r.session_id ? <code className="text-[11px]" title={r.session_id}>{r.session_id.slice(0, 8)}…</code> : <span className="text-faint">(后台)</span>}
                </td>
                <td className="px-3 py-2"><SrcChip src={r.source} /></td>
                <td className="px-3 py-2 text-[12.5px] text-muted">{r.provider || "—"}</td>
                <td className="px-3 py-2 text-[12.5px] text-muted">{r.model || "—"}</td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">{r.input_tokens ? fmt(r.input_tokens) : "—"}</td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">{r.output_tokens ? fmt(r.output_tokens) : "—"}</td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">{r.cache_read_tokens ? fmt(r.cache_read_tokens) : "—"}</td>
                <td className="px-3 py-2 text-[12.5px]">
                  {r.ok ? <span style={{ color: "#4ade80" }}>✓</span> : <span style={{ color: "#f87171" }}>✗ {r.error || "error"}</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="flex items-center justify-end gap-2 px-3.5 py-2.5 text-xs text-faint">
        <span>共 {data?.total ?? 0} 条</span>
        <span className="ml-2">第 {page}/{pages} 页</span>
        <button
          disabled={page <= 1}
          onClick={() => setPage((p) => Math.max(1, p - 1))}
          className="rounded-md border border-line px-2.5 py-1 text-muted enabled:hover:bg-card2 disabled:opacity-40"
        >
          ‹
        </button>
        <button
          disabled={page >= pages}
          onClick={() => setPage((p) => Math.min(pages, p + 1))}
          className="rounded-md border border-line px-2.5 py-1 text-muted enabled:hover:bg-card2 disabled:opacity-40"
        >
          ›
        </button>
      </div>
    </div>
  );
}
