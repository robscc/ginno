"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { FolderOpen, Star, X, Plus, Settings2, HardDriveDownload } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import { useGinno } from "@/lib/store";
import type { FolderEntry, SessionMeta } from "@/lib/types";

/** Mount chip (context-folders-design.md §3.4). Answers the user's constant
 * question "what can the agent see right now?" and is the mount/unmount/
 * primary entry point. Two modes share one panel (home-mount-picker-design.md):
 *  - live (TopBar): session-bound, mutations PUT /api/sessions/{id}/context
 *  - pending (home composer): controlled by the caller, no session exists yet —
 *    the choice rides the createSession request on first send. */

type Pending = { ids: string[]; primary: string | null };
type ChangeFn = (ids: string[], primary: string | null) => void;

export function ContextFoldersChip({
  session,
  ids: pendingIds,
  primary: pendingPrimary,
  onChange,
}: {
  /** live 模式：TopBar 传入（可为 null，此时整个 chip 隐藏）。 */
  session?: SessionMeta | null;
  /** pending 模式（首页）：受控值 + 回调，与 session 互斥。 */
  ids?: string[];
  primary?: string | null;
  onChange?: ChangeFn;
}) {
  const g = useGinno();
  const tr = useTranslations("shell");
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const [library, setLibrary] = useState<FolderEntry[]>([]);
  const [path, setPath] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);

  const live = session !== undefined;
  const ids = live ? session?.context_folders ?? [] : pendingIds ?? [];
  const primary = live ? session?.primary_folder ?? null : pendingPrimary ?? null;

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

  if (live && !session) return null;

  /** live: 落库并广播；pending: 只更新本地待生效列表，发送时随建会话请求生效。 */
  async function applyContext(folder_ids: string[], primary_id: string | null) {
    if (!live) {
      onChange?.(folder_ids, primary_id);
      return;
    }
    const r = await api.putSessionContext(session!.id, { folder_ids, primary_id });
    if (r.ok) {
      g.applySessionPatch(session!.id, {
        context_folders: folder_ids,
        primary_folder: primary_id,
      });
    } else {
      setErr(r.error || tr("folders.errOperation"));
    }
  }

  /** 挂载一个路径（快选/手输/浏览共用）：入库幂等，首个挂载自动成为 primary。 */
  async function mountPath(p: string, auto = false) {
    if (!p || busy) return;
    setBusy(true);
    setErr("");
    try {
      const c = await api.createFolder({ path: p, access: "rw", load_rules: true, auto_mount: auto });
      if (!c.ok || !c.folder) {
        setErr(c.error || tr("folders.errMount"));
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

  /** 原生目录选择器（tauri-plugin-dialog）。纯浏览器无桥接时按钮本就不渲染。 */
  async function browse() {
    try {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const picked = await open({ directory: true, multiple: false });
      if (typeof picked === "string") await mountPath(picked);
    } catch {
      setErr(tr("folders.errBrowse"));
    }
  }

  async function toggleAccess(f: FolderEntry) {
    await api.updateFolder(f.id, { access: f.access === "rw" ? "ro" : "rw" });
    await refreshLibrary();
  }

  /** 「自动挂载」是库级标记（新建会话预填），对已建会话无追溯效果。 */
  async function toggleAuto(f: FolderEntry) {
    await api.updateFolder(f.id, { auto_mount: !f.auto_mount });
    await refreshLibrary();
  }

  const rows = ids.map((id) => {
    const f = library.find((x) => x.id === id);
    return { id, folder: f ?? null };
  });
  // 快选区 = 库里还没挂载的条目；已在挂载列表里的不重复出现。
  const quick = library.filter((f) => !ids.includes(f.id));

  return (
    <div className="relative shrink-0">
      <button
        onClick={() => setOpen((o) => !o)}
        title={tr("folders.chipTitle")}
        className="pill border border-line2 bg-card text-muted transition-colors hover:text-txt"
        style={ids.length ? { color: "#34d399" } : undefined}
      >
        <FolderOpen className="h-3 w-3" />
        {ids.length > 0 ? ids.length : tr("folders.mount")}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-50 mt-1.5 w-96 max-w-[min(24rem,calc(100vw-2rem))] overflow-hidden rounded-xl border border-line bg-card shadow-xl">
            <div className="border-b border-line px-3 py-2 text-xs font-medium text-muted">
              {tr("folders.title")}
            </div>

            <div className="max-h-72 overflow-y-auto px-2 py-1.5">
              {rows.length === 0 && (
                <div className="px-2 py-3 text-xs text-faint">
                  {tr("folders.empty")}
                </div>
              )}
              {rows.map(({ id, folder }) => (
                <div key={id} className="group flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-card2">
                  {folder ? (
                    <>
                      <button
                        onClick={() => applyContext(ids, primary === id ? null : id)}
                        title={primary === id ? tr("folders.unsetPrimary") : tr("folders.setPrimary")}
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
                        title={tr("folders.accessTitle")}
                        className="shrink-0 rounded border border-line2 px-1 py-px font-mono text-[10px]"
                        style={{
                          color: folder.access === "rw" ? "#4ade80" : "#fbbf24",
                        }}
                      >
                        {folder.access}
                      </button>
                      <button
                        onClick={() => applyContext(ids.filter((x) => x !== id), primary === id ? null : primary)}
                        title={tr("folders.unmount")}
                        className="shrink-0 rounded p-0.5 text-faint hover:text-red-400"
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    </>
                  ) : (
                    <>
                      <span className="min-w-0 flex-1 truncate text-xs text-faint">
                        {tr("folders.missing", { id })}
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

              {/* 目录库快选：一键挂载已入库目录；「自动」= 新建会话预填标记 */}
              {quick.length > 0 && (
                <>
                  <div className="mt-1 border-t border-line px-2 pt-2 pb-1 text-[10.5px] font-medium uppercase tracking-wide text-faint">
                    {tr("folders.libraryTitle")}
                  </div>
                  {quick.map((f) => (
                    <div key={f.id} className="group flex items-center gap-2 rounded-lg px-2 py-1.5 hover:bg-card2">
                      <div className="min-w-0 flex-1">
                        <div className="truncate text-xs text-muted">{f.name}</div>
                        <div className="truncate font-mono text-[10px] text-faint" title={f.path}>
                          {f.path}
                        </div>
                      </div>
                      <button
                        onClick={() => toggleAuto(f)}
                        title={tr("folders.autoTitle")}
                        className="shrink-0 rounded border px-1 py-px font-mono text-[10px]"
                        style={{
                          color: f.auto_mount ? "#34d399" : "#52525b",
                          borderColor: f.auto_mount ? "#34d39955" : "var(--line2)",
                        }}
                      >
                        auto
                      </button>
                      <button
                        onClick={() => mountPath(f.path)}
                        disabled={busy}
                        title={tr("folders.quickAddTitle")}
                        className="shrink-0 rounded p-0.5 text-faint hover:text-txt disabled:opacity-50"
                      >
                        <Plus className="h-3.5 w-3.5" />
                      </button>
                    </div>
                  ))}
                </>
              )}
            </div>

            <div className="border-t border-line px-3 py-2">
              <div className="flex gap-1.5">
                <input
                  value={path}
                  onChange={(e) => {
                    setPath(e.target.value);
                    setErr("");
                  }}
                  onKeyDown={(e) => e.key === "Enter" && mountPath(path.trim())}
                  placeholder={tr("folders.inputPlaceholder")}
                  className="field min-w-0 flex-1 py-1 text-xs"
                />
                {isDesktop() && (
                  <button
                    onClick={browse}
                    disabled={busy}
                    title={tr("folders.browseTitle")}
                    className="flex shrink-0 items-center justify-center rounded-lg border border-line px-2 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
                  >
                    <HardDriveDownload className="h-3.5 w-3.5" />
                  </button>
                )}
                <button
                  onClick={() => mountPath(path.trim())}
                  disabled={busy || !path.trim()}
                  title={tr("folders.mountTitle")}
                  className="flex shrink-0 items-center gap-1 rounded-lg bg-violet px-2.5 py-1 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
                >
                  <Plus className="h-3 w-3" /> {tr("folders.mount")}
                </button>
              </div>
              {err && <div className="mt-1 text-[11px] text-red-400">{err}</div>}
              <button
                onClick={() => {
                  setOpen(false);
                  router.push("/settings/knowledge/folders");
                }}
                className="mt-1.5 flex items-center gap-1 text-[11px] text-faint hover:text-txt"
              >
                <Settings2 className="h-3 w-3" /> {tr("folders.manage")}
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
