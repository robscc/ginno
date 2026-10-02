"use client";

/** 会话：谁花的。全部会话按用量排序；点行 → 请求日志按该会话过滤。
 * 已删除会话的用量仍保留（账单性质，设计 §3.3）。 */

import { useCallback, useEffect, useState } from "react";
import { Search } from "lucide-react";
import * as api from "@/lib/runtime";
import type { UsageSessionRow } from "@/lib/types";
import { fmt, pct } from "./charts";

function fmtTime(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const now = new Date();
  const p = (x: number) => String(x).padStart(2, "0");
  if (d.toDateString() === now.toDateString()) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

const SORTS = [
  ["total", "Total tokens"],
  ["input", "Input"],
  ["output", "Output"],
  ["hit", "Cache hit rate"],
  ["calls", "Requests"],
  ["updated", "Last active"],
] as const;

export function SessionsPanel({ onOpenRequests }: { onOpenRequests: (sessionId: string) => void }) {
  const [rows, setRows] = useState<UsageSessionRow[]>([]);
  const [sort, setSort] = useState("total");
  const [q, setQ] = useState("");
  const [loaded, setLoaded] = useState(false);

  const load = useCallback(async (s: string) => {
    try {
      const r = await api.getUsageSessions({ sort: s });
      setRows(r.sessions || []);
    } catch {
      /* runtime offline */
    } finally {
      setLoaded(true);
    }
  }, []);

  useEffect(() => {
    load(sort);
  }, [sort, load]);

  const list = rows.filter(
    (r) => !q || r.title.toLowerCase().includes(q.toLowerCase()) || (r.model || "").toLowerCase().includes(q.toLowerCase()),
  );

  return (
    <div className="overflow-hidden rounded-xl border border-line bg-card">
      <div className="flex flex-wrap items-center gap-2.5 border-b border-line px-3.5 py-3">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-faint" />
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Search sessions / models…"
            className="w-48 rounded-lg border border-line bg-card2 py-1.5 pl-8 pr-3 text-xs text-txt outline-none placeholder:text-faint focus:border-line2"
          />
        </div>
        <label className="flex items-center gap-1.5 text-[11.5px] text-faint">
          Sort
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value)}
            className="rounded-lg border border-line bg-card2 px-2.5 py-1.5 text-xs text-txt outline-none"
          >
            {SORTS.map(([v, l]) => (
              <option key={v} value={v}>{l}</option>
            ))}
          </select>
        </label>
        <div className="flex-1" />
        <span className="text-[11.5px] text-faint">All sessions within the retention period · usage of deleted sessions is kept · click a row to view requests</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[820px] border-collapse">
          <thead>
            <tr>
              {["Session / Agent", "Model", "Input", "Output", "Cache read", "Hit rate", "Requests", "Last active"].map((h, i) => (
                <th
                  key={h}
                  className={`whitespace-nowrap border-b border-line px-3 py-2 text-[11px] font-semibold tracking-wide text-faint ${i >= 2 && i <= 6 ? "text-right" : "text-left"}`}
                >
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {!loaded && (
              <tr><td colSpan={8} className="px-3 py-10 text-center text-xs text-faint">Loading…</td></tr>
            )}
            {loaded && list.length === 0 && (
              <tr><td colSpan={8} className="px-3 py-10 text-center text-xs text-faint">No session usage records</td></tr>
            )}
            {list.map((r) => (
              <tr
                key={r.session_id || "(bg)"}
                className="cursor-pointer border-b border-white/5 transition-colors last:border-b-0 hover:bg-white/5"
                onClick={() => r.session_id && onOpenRequests(r.session_id)}
                title="View the request log of this session"
              >
                <td className="px-3 py-2 text-[12.5px]">
                  <span className="text-txt">{r.title || "(background/system)"}</span>
                  {r.deleted && <span className="ml-1.5 rounded border border-line2 px-1 text-[10px] text-faint">deleted</span>}
                  {r.agent_id && <div className="text-[11px] text-faint">{r.agent_id}</div>}
                </td>
                <td className="px-3 py-2 text-[12.5px] text-muted">{r.model || "—"}</td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">{fmt(r.input_tokens)}</td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">{fmt(r.output_tokens)}</td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">{r.cache_read_tokens ? fmt(r.cache_read_tokens) : "—"}</td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums" style={{ color: r.cache_hit_ratio > 0 ? "#4ade80" : undefined }}>
                  {r.input_tokens > 0 ? `⚡ ${pct(r.cache_hit_ratio)}` : "—"}
                </td>
                <td className="px-3 py-2 text-right text-[12.5px] tabular-nums text-muted">{r.calls}</td>
                <td className="px-3 py-2 text-[12.5px] text-muted">{fmtTime(r.last_active)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
