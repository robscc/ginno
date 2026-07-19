"use client";

import { useEffect, useMemo, useState } from "react";
import * as api from "@/lib/runtime";
import { BookOpen, FileText, Folder } from "lucide-react";

interface Entry {
  kind: "file" | "dir";
  name: string;
}

function parse(results: string[]): Entry[] {
  const out: Entry[] = [];
  for (const blob of results) {
    for (const line of blob.split("\n")) {
      const m = line.match(/^\[(FILE|DIR)\]\s*(.+?)\s*$/);
      if (m) out.push({ kind: m[1] === "DIR" ? "dir" : "file", name: m[2] });
    }
  }
  return out;
}

export default function KnowledgeBasePage() {
  const [filter, setFilter] = useState("");
  const [entries, setEntries] = useState<Entry[]>([]);
  const [servers, setServers] = useState<{ name: string; tools: string[] }[]>([]);

  useEffect(() => {
    api.kbServers().then(setServers).catch(() => {});
    api
      .kbList()
      .then((r) => setEntries(parse(r.results || [])))
      .catch(() => {});
  }, []);

  const filtered = useMemo(
    () =>
      filter
        ? entries.filter((e) => e.name.toLowerCase().includes(filter.toLowerCase()))
        : entries,
    [entries, filter],
  );

  return (
    <div className="flex min-w-0 flex-1 flex-col px-8 py-7">
      <div className="flex items-center gap-2">
        <BookOpen className="h-5 w-5 text-violet" />
        <h2 className="text-lg font-semibold text-txt">Knowledge Base</h2>
      </div>
      <p className="mt-1 text-sm text-muted">通过 MCP vault 索引的 Obsidian 知识库浏览 / 过滤。</p>
      <div className="mt-3 flex flex-wrap gap-2 text-xs text-faint">
        {servers.length === 0 ? (
          <span>无已连接的 vault server（在 Settings → MCP 工具 配置）。</span>
        ) : (
          servers.map((s) => (
            <span key={s.name} className="pill border border-line2 text-muted">
              {s.name} · {s.tools.length} tools
            </span>
          ))
        )}
      </div>
      <input
        className="field mt-4 max-w-xl"
        placeholder="filter files…"
        value={filter}
        onChange={(e) => setFilter(e.target.value)}
      />
      <div className="mt-4 max-w-xl space-y-1">
        {filtered.length === 0 && <div className="text-xs text-faint">No files.</div>}
        {filtered.map((e, i) => (
          <div key={i} className="flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm hover:bg-card/50">
            {e.kind === "dir" ? (
              <Folder className="h-4 w-4 text-violet" />
            ) : (
              <FileText className="h-4 w-4 text-muted" />
            )}
            <span className="text-txt">{e.name}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
