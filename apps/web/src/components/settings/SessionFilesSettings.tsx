"use client";

/** Settings → 会话文件. Every session owns a files directory
 * (`~/.ginno/projects/<slug>/sessions/<session_id>/`) that is created with the
 * session and PRESERVED when the session is deleted. This module lists those
 * directories (including orphaned ones whose session was deleted) and lets you
 * browse, reveal, and delete files / whole directories. */

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight, FileText, Folder, FolderOpen, Trash2 } from "lucide-react";
import * as api from "@/lib/runtime";
import type { SessionDirEntry, SessionDirSummary } from "@/lib/types";
import { ConfirmModal } from "../ConfirmModal";

function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const u = ["B", "KB", "MB", "GB"];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < u.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
}

function fmtTime(ts: number): string {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (x: number) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

type ConfirmTarget =
  | { kind: "file"; slug: string; sid: string; path: string; label: string; parentKey: string }
  | { kind: "dir"; slug: string; sid: string; path: string; label: string };

export function SessionFilesSettings() {
  const [dirs, setDirs] = useState<SessionDirSummary[]>([]);
  const [listings, setListings] = useState<Record<string, SessionDirEntry[]>>({});
  const [open, setOpen] = useState<Record<string, boolean>>({});
  const [confirm, setConfirm] = useState<ConfirmTarget | null>(null);
  const [msg, setMsg] = useState("");

  const key = (slug: string, sid: string, sub: string) => `${slug}/${sid}|${sub}`;

  const load = useCallback(async () => {
    try {
      const r = await api.listSessionFileDirs();
      setDirs(r.sessions || []);
    } catch {
      /* ignore */
    }
  }, []);

  // Re-fetch every currently-open listing (after a delete mutates the tree).
  const reloadOpen = useCallback(async () => {
    await load();
    const entries = Object.entries(open);
    for (const [k, isOpen] of entries) {
      if (!isOpen) continue;
      const [head, sub] = k.split("|");
      const [slug, sid] = head.split("/");
      try {
        const r = await api.listSessionDirFiles(slug, sid, sub || undefined);
        setListings((p) => ({ ...p, [k]: r.entries || [] }));
      } catch {
        /* ignore */
      }
    }
  }, [load, open]);

  useEffect(() => {
    load();
  }, [load]);

  async function toggle(slug: string, sid: string, sub: string) {
    const k = key(slug, sid, sub);
    if (open[k]) {
      setOpen((p) => ({ ...p, [k]: false }));
      return;
    }
    if (!listings[k]) {
      try {
        const r = await api.listSessionDirFiles(slug, sid, sub || undefined);
        setListings((p) => ({ ...p, [k]: r.entries || [] }));
      } catch {
        setListings((p) => ({ ...p, [k]: [] }));
      }
    }
    setOpen((p) => ({ ...p, [k]: true }));
  }

  async function doDelete(t: ConfirmTarget) {
    setConfirm(null);
    try {
      if (t.kind === "file") {
        const r = await api.deleteSessionFile(t.slug, t.sid, t.path);
        setMsg(r.ok ? `已删除 ${t.label}` : r.error || "删除失败");
      } else {
        const r = await api.deleteSessionDir(t.slug, t.sid, t.path || undefined);
        setMsg(r.ok ? `已删除 ${t.label}` : r.error || "删除失败");
      }
      await reloadOpen();
    } catch {
      setMsg("删除失败");
    }
  }

  function reveal(slug: string, sid: string, path: string) {
    api.revealSessionFile(slug, sid, path).catch(() => {});
  }

  // Render the entries of one directory level; recurses into subdirectories.
  // ``orphaned`` = the session was deleted; only then are files deletable.
  function renderLevel(slug: string, sid: string, sub: string, depth: number, orphaned: boolean) {
    const k = key(slug, sid, sub);
    const entries = listings[k];
    if (!open[k] || !entries) return null;
    if (!entries.length)
      return (
        <div className="py-1 text-xs text-faint" style={{ paddingLeft: depth * 18 + 26 }}>
          （空）
        </div>
      );
    return (
      <div>
        {entries.map((e) => {
          const subPath = sub ? `${sub}/${e.name}` : e.name;
          if (e.type === "dir") {
            const isOpen = !!open[key(slug, sid, subPath)];
            return (
              <div key={subPath}>
                <button
                  onClick={() => toggle(slug, sid, subPath)}
                  className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-xs text-muted hover:bg-line2/40"
                  style={{ paddingLeft: depth * 18 + 8 }}
                >
                  {isOpen ? (
                    <ChevronDown className="h-3.5 w-3.5 shrink-0" />
                  ) : (
                    <ChevronRight className="h-3.5 w-3.5 shrink-0" />
                  )}
                  <Folder className="h-3.5 w-3.5 shrink-0 text-amber" />
                  <span className="truncate">{e.name}</span>
                </button>
                {renderLevel(slug, sid, subPath, depth + 1, orphaned)}
              </div>
            );
          }
          return (
            <div
              key={subPath}
              className="group flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-muted hover:bg-line2/40"
              style={{ paddingLeft: depth * 18 + 26 }}
            >
              <FileText className="h-3.5 w-3.5 shrink-0 text-faint" />
              <span className="min-w-0 flex-1 truncate">{e.name}</span>
              <span className="shrink-0 text-[10px] text-faint">{fmtBytes(e.size)}</span>
              <button
                title="在 Finder 中显示"
                onClick={() => reveal(slug, sid, subPath)}
                className="shrink-0 rounded px-1 text-[10px] text-faint opacity-0 transition-opacity hover:text-txt group-hover:opacity-100"
              >
                显示
              </button>
              {orphaned && (
                <button
                  title="删除文件"
                  onClick={() =>
                    setConfirm({ kind: "file", slug, sid, path: subPath, label: e.name, parentKey: k })
                  }
                  className="shrink-0 rounded p-0.5 text-faint opacity-0 transition-opacity hover:text-red group-hover:opacity-100"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              )}
            </div>
          );
        })}
      </div>
    );
  }

  return (
    <div className="px-8 py-7">
      <h2 className="text-lg font-semibold text-txt">会话文件</h2>
      <p className="mt-1 text-sm text-muted">
        每个会话都有一个专属文件目录，创建会话时自动生成；删除会话只清除对话历史，文件会保留在这里，可浏览或手动清理。
      </p>

      {dirs.length === 0 ? (
        <div className="mt-6 rounded-xl border border-line bg-card p-6 text-center text-sm text-faint">
          暂无会话文件目录
        </div>
      ) : (
        <div className="mt-4 space-y-2">
          {dirs.map((d) => {
            const rootKey = key(d.project_slug, d.session_id, "");
            const isOpen = !!open[rootKey];
            return (
              <div key={rootKey} className="rounded-xl border border-line bg-card p-3">
                <div className="flex items-center gap-2">
                  <button
                    onClick={() => toggle(d.project_slug, d.session_id, "")}
                    className="flex min-w-0 flex-1 items-center gap-2 text-left"
                  >
                    {isOpen ? (
                      <ChevronDown className="h-4 w-4 shrink-0 text-muted" />
                    ) : (
                      <ChevronRight className="h-4 w-4 shrink-0 text-muted" />
                    )}
                    <FolderOpen className="h-4 w-4 shrink-0" style={{ color: "#38bdf8" }} />
                    <span className="truncate text-sm text-txt">
                      {d.title || "(未命名会话)"}
                    </span>
                    <span className="truncate font-mono text-[10px] text-faint">{d.session_id}</span>
                    {d.orphaned && (
                      <span className="shrink-0 rounded-full bg-amber/15 px-2 py-0.5 text-[10px] font-medium text-amber">
                        已删除会话
                      </span>
                    )}
                  </button>
                  <span className="shrink-0 text-[11px] text-faint">
                    {d.file_count} 个文件 · {fmtBytes(d.total_bytes)} · {fmtTime(d.mtime)}
                  </span>
                  <button
                    title="在 Finder 中显示"
                    onClick={() => reveal(d.project_slug, d.session_id, "")}
                    className="shrink-0 rounded px-1.5 py-0.5 text-[11px] text-muted hover:text-txt"
                  >
                    打开
                  </button>
                  {d.orphaned ? (
                    <button
                      title="删除整个会话目录"
                      onClick={() =>
                        setConfirm({
                          kind: "dir",
                          slug: d.project_slug,
                          sid: d.session_id,
                          path: "",
                          label: d.title || d.session_id,
                        })
                      }
                      className="shrink-0 rounded p-1 text-muted hover:text-red"
                    >
                      <Trash2 className="h-4 w-4" />
                    </button>
                  ) : (
                    <span
                      title="进行中的会话文件受保护，不能在这里删除；删除会话后其文件才可清理"
                      className="shrink-0 rounded-full bg-line2/60 px-2 py-0.5 text-[10px] text-faint"
                    >
                      使用中
                    </span>
                  )}
                </div>
                {renderLevel(d.project_slug, d.session_id, "", 0, d.orphaned)}
              </div>
            );
          })}
        </div>
      )}

      {msg && <div className="mt-3 text-xs text-muted">{msg}</div>}

      {confirm && (
        <ConfirmModal
          title={confirm.kind === "file" ? "删除文件" : "删除会话目录"}
          message={
            confirm.kind === "file"
              ? `确定删除文件「${confirm.label}」？磁盘上的文件将被移除，且无法恢复。`
              : `确定删除「${confirm.label}」的整个文件目录？其中所有文件将被移除，且无法恢复。对话历史不受影响。`
          }
          confirmLabel="删除"
          onConfirm={() => doDelete(confirm)}
          onCancel={() => setConfirm(null)}
        />
      )}
    </div>
  );
}
