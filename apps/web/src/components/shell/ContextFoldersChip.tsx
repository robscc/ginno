"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { FolderOpen, Star, X, Plus, Settings2 } from "lucide-react";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";
import type { FolderEntry, SessionMeta } from "@/lib/types";

/** TopBar chip: which local folders this session can see (context-folders-
 * design.md §3.4). Answers the user's constant question "what can the agent
 * see right now?" and is the mount/unmount/primary entry point. */
export function ContextFoldersChip({ session }: { session: SessionMeta | null }) {
  const g = useGinno();
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [library, setLibrary] = useState<FolderEntry[]>([]);
  const [path, setPath] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const ids = session?.context_folders ?? [];
  const primary = session?.primary_folder ?? null;

  const refreshLibrary = useCallback(async () => {
    try {
      setLibrary((await api.listFolders()).folders || []);
    } catch {
      /* sidecar unreachable — chip degrades to counts only */
    }
  }, []);
  useEffect(() => {
    if (open) void refreshLibrary();
  }, [open, refreshLibrary]);

  if (!session) return null;

  async function applyContext(folder_ids: string[], primary_id: string | null) {
    const r = await api.putSessionContext(session!.id, { folder_ids, primary_id });
    if (r.ok) {
      g.applySessionPatch(session!.id, {
        context_folders: folder_ids,
        primary_folder: primary_id,
      });
    } else {
      setErr(r.error || "操作失败");
    }
    return r;
  }

  async function attach() {
    const p = path.trim();
    if (!p || busy) return;
    setBusy(true);
    setErr("");
    try {
      const c = await api.createFolder({ path: p, access: "rw", load_rules: true });
      if (!c.ok || !c.folder) {
        setErr(c.error || "挂载失败");
        return;
      }
      const newIds = ids.includes(c.folder.id) ? [...ids] : [...ids, c.folder.id];
      // First mount auto-becomes primary (bash cwd switches to it).
      const newPrimary = primary ?? (ids.length === 0 ? c.folder.id : null);
      await applyContext(newIds, newPrimary);
      setPath("");
      await refreshLibrary();
    } finally {
      setBusy(false);
    }
  }

  async function toggleAccess(f: FolderEntry) {
    await api.updateFolder(f.id, { access: f.access === "rw" ? "ro" : "rw" });
    await refreshLibrary();
  }

  const rows = ids.map((id) => {
    const f = library.find((x) => x.id === id);
    return { id, folder: f ?? null };
  });

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((o) => !o)}
        title="本会话挂载的上下文目录"
        className="pill border border-line2 bg-card text-muted transition-colors hover:text-txt"
        style={ids.length ? { color: "#34d399" } : undefined}
      >
        <FolderOpen className="h-3 w-3" />
        {ids.length > 0 ? ids.length : "挂载"}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-50 mt-1.5 w-96 overflow-hidden rounded-xl border border-line bg-card shadow-xl">
            <div className="border-b border-line px-3 py-2 text-xs font-medium text-muted">
              本会话上下文目录
            </div>

            <div className="max-h-64 overflow-y-auto px-2 py-1.5">
              {rows.length === 0 && (
                <div className="px-2 py-3 text-xs text-faint">
                  未挂载任何目录。挂载后 Agent 可直接读写其中的文件（代码仓库、笔记等）。
                </div>
              )}
              {rows.map(({ id, folder }) => (
                <div key={id} className="group flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-card2">
                  {folder ? (
                    <>
                      <button
                        onClick={() => applyContext(ids, primary === id ? null : id)}
                        title={primary === id ? "取消主工作目录" : "设为主工作目录（bash 的 cwd）"}
                        className="shrink-0"
                        style={{ color: primary === id ? "#fbbf24" : "#52525b" }}
                      >
                        <Star className="h-3.5 w-3.5" fill={primary === id ? "#fbbf24" : "none"} />
                      </button>
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-xs text-txt">{folder.name}</div>
                        <div className="truncate font-mono text-[10px] text-faint" title={folder.path}>
                          {folder.path}
                        </div>
                      </div>
                      <button
                        onClick={() => toggleAccess(folder)}
                        title="切换访问级（库级属性）：rw 可读写 / ro 只读"
                        className="shrink-0 rounded border border-line2 px-1 py-px font-mono text-[10px]"
                        style={{
                          color: folder.access === "rw" ? "#4ade80" : "#fbbf24",
                        }}
                      >
                        {folder.access}
                      </button>
                      <button
                        onClick={() => applyContext(ids.filter((x) => x !== id), primary === id ? null : primary)}
                        title="从本会话卸载"
                        className="shrink-0 rounded p-0.5 text-faint hover:text-red-400"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </>
                  ) : (
                    <>
                      <span className="min-w-0 flex-1 truncate text-xs text-faint">
                        {id}（目录库中缺失）
                      </span>
                      <button
                        onClick={() => applyContext(ids.filter((x) => x !== id), primary === id ? null : primary)}
                        className="shrink-0 rounded p-0.5 text-faint hover:text-red-400"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </>
                  )}
                </div>
              ))}
            </div>

            <div className="border-t border-line px-3 py-2">
              <div className="flex gap-1.5">
                <input
                  value={path}
                  onChange={(e) => {
                    setPath(e.target.value);
                    setErr("");
                  }}
                  onKeyDown={(e) => e.key === "Enter" && attach()}
                  placeholder="输入目录路径挂载，如 ~/workspace/my-repo"
                  className="field min-w-0 flex-1 py-1 text-xs"
                />
                <button
                  onClick={attach}
                  disabled={busy || !path.trim()}
                  title="注册进目录库并挂载（默认读写；首个挂载自动成为主工作目录）"
                  className="flex shrink-0 items-center gap-1 rounded-lg bg-violet px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                >
                  <Plus className="h-3 w-3" /> 挂载
                </button>
              </div>
              {err && <div className="mt-1 text-[11px] text-red-400">{err}</div>}
              <button
                onClick={() => {
                  setOpen(false);
                  router.push("/settings/folders");
                }}
                className="mt-1.5 flex items-center gap-1 text-[11px] text-faint hover:text-txt"
              >
                <Settings2 className="h-3 w-3" /> 管理目录库（访问级 / 规则开关）
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
