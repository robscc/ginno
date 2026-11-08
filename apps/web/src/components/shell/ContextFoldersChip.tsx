"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { FolderOpen, Star, X, Plus, Settings2, HardDriveDownload, ShieldAlert } from "lucide-react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { isDesktop } from "@/lib/desktop";
import { useGinno } from "@/lib/store";
import type { FolderEntry, SessionMeta } from "@/lib/types";
import { TopBarFuncButton } from "./TopBarFuncButton";

/** Mount chip (context-folders-design.md §3.4). Answers the user's constant
 * question "what can the agent see right now?" and is the mount/unmount/
 * primary entry point. Two modes share one panel (home-mount-picker-design.md):
 *  - live (TopBar): session-bound, mutations PUT /api/sessions/{id}/context
 *  - pending (home composer): controlled by the caller, no session exists yet —
 *    the choice rides the createSession request on first send. */

type Pending = { ids: string[]; primary: string | null };
type ChangeFn = (ids: string[], primary: string | null) => void;

/** 特权挂载预设：Ginno 运行时数据 home（runtime-data-access-design.md §5）。
 * 真实路径以 GET /api/folders 的 runtime_home 为准（$GINNO_HOME 可被重定向），
 * "~/.ginno" 仅作 sidecar 不可达时的展示回退。 */
const RUNTIME_HOME_FALLBACK = "~/.ginno";

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
  const [runtimeHome, setRuntimeHome] = useState("");
  const [path, setPath] = useState("");
  const [err, setErr] = useState("");
  const [busy, setBusy] = useState(false);
  /** 特权挂载待确认路径（非空 = 确认弹层开着）。 */
  const [privPath, setPrivPath] = useState<string | null>(null);

  const live = session !== undefined;
  const ids = live ? session?.context_folders ?? [] : pendingIds ?? [];
  const primary = live ? session?.primary_folder ?? null : pendingPrimary ?? null;

  const refreshLibrary = useCallback(async () => {
    try {
      const r = await api.listFolders();
      setLibrary(r.folders || []);
      if (r.runtime_home) setRuntimeHome(r.runtime_home);
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

  /** 挂载一个路径（快选/手输/浏览/特权预设共用）：先 probe 判特权——
   * 路径落入运行时数据 home 必须先过用户确认弹层（design §5，D12：
   * 闸挂在 path 判定上，预设按钮只是快捷方式）；普通路径直接入库挂载。 */
  async function mountPath(p: string, auto = false) {
    if (!p || busy) return;
    setBusy(true);
    setErr("");
    try {
      let runtimeHome = false;
      try {
        const pr = await api.probeFolder(p);
        runtimeHome = !!pr.runtime_home;
      } catch {
        /* probe 不可达 → 交给 createFolder 报错（同一 sidecar） */
      }
      if (runtimeHome) {
        setPrivPath(p);
        return;
      }
      await doMount(p, auto);
    } finally {
      setBusy(false);
    }
  }

  async function doMount(p: string, auto = false) {
    if (!p) return;
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
      {live ? (
        // TopBar 统一按钮(topbar-unification.md §2.3):挂载数徽标,tooltip 带
        // 名称列表 + ⭐primary + ro/rw(数据 hover 展示)。
        <TopBarFuncButton
          onClick={() => setOpen((o) => !o)}
          open={open}
          title={
            library.length && rows.length
              ? rows
                  .map(({ id, folder }) => {
                    const star = primary === id ? "⭐" : "";
                    const acc = folder?.access === "ro" ? " (ro)" : "";
                    return `${star}${folder?.name ?? id}${acc}`;
                  })
                  .join(" · ")
              : tr("folders.chipTitle")
          }
          count={ids.length}
          active={ids.length ? "var(--st-planned)" : undefined}
        >
          <FolderOpen className="h-4 w-4" />
        </TopBarFuncButton>
      ) : (
        <button
          onClick={() => setOpen((o) => !o)}
          title={tr("folders.chipTitle")}
          className="pill border border-line2 bg-card text-muted transition-colors hover:text-txt"
          style={ids.length ? { color: "var(--st-planned)" } : undefined}
        >
          <FolderOpen className="h-3 w-3" />
          {ids.length > 0 ? ids.length : tr("folders.mount")}
        </button>
      )}

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute left-0 z-50 mt-1.5 w-96 max-w-[min(24rem,calc(100vw-2rem))] overflow-hidden rounded-xl border border-line bg-card shadow-xl">
            <div className="border-b border-line px-3 py-2 text-xs font-medium text-muted">
              {tr("folders.title")}
            </div>

            <div className="max-h-72 overflow-y-auto px-2 py-1.5">
              {/* 特权预设行（runtime-data-access-design.md §5）：一键发起运行时
                  数据只读挂载——mountPath 的 probe 闸会先弹确认层。 */}
              <div className="mb-1 flex items-center gap-2 rounded-lg border border-amber-500/30 bg-amber-500/5 px-2 py-1.5">
                <ShieldAlert className="h-3.5 w-3.5 shrink-0 text-amber-400" />
                <div className="min-w-0 flex-1">
                  <div className="truncate text-xs text-muted">{tr("folders.privPreset")}</div>
                  <div
                    className="selectable truncate font-mono text-[10px] text-faint"
                    title={runtimeHome || RUNTIME_HOME_FALLBACK}
                  >
                    {runtimeHome || RUNTIME_HOME_FALLBACK}
                  </div>
                </div>
                <button
                  onClick={() => mountPath(runtimeHome || RUNTIME_HOME_FALLBACK)}
                  disabled={busy || !runtimeHome}
                  title={tr("folders.privPresetTitle")}
                  className="shrink-0 rounded border border-amber-500/40 px-1.5 py-px font-mono text-[10px] text-amber-300 hover:bg-amber-500/10 disabled:opacity-50"
                >
                  ro
                </button>
              </div>
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
                        <div className="truncate text-xs text-txt">
                          {folder.forced_ro && (
                            <span title={tr("folders.privBadge")}>🛡 </span>
                          )}
                          {folder.name}
                        </div>
                        <div className="selectable truncate font-mono text-[10px] text-faint" title={folder.path}>
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
                        <div className="truncate text-xs text-muted">
                          {f.forced_ro && <span title={tr("folders.privBadge")}>🛡 </span>}
                          {f.name}
                        </div>
                        <div className="selectable truncate font-mono text-[10px] text-faint" title={f.path}>
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
              {err && <div className="selectable mt-1 text-[11px] text-red-400">{err}</div>}
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

      {/* 特权挂载确认层（design §5）：probe 判定路径落入运行时数据 home 时
          必经；双确认 = auto_mount 语义（仅本会话 / 默认用于新会话）。 */}
      {privPath && (
        <div
          className="fixed inset-0 z-[60] flex items-center justify-center bg-black/60 p-4"
          onMouseDown={() => setPrivPath(null)}
          role="dialog"
          aria-modal="true"
        >
          <div
            className="w-full max-w-md rounded-xl border border-amber-500/40 bg-card p-4 shadow-2xl"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="flex items-center gap-2 text-sm font-semibold text-txt">
              <ShieldAlert className="h-4 w-4 text-amber-400" /> {tr("folders.privTitle")}
            </div>
            <div className="mt-2 text-xs leading-relaxed text-muted">
              {tr("folders.privMessage")}
              <div className="selectable mt-1 font-mono text-[10px] text-faint">{privPath}</div>
            </div>
            <div className="mt-4 flex flex-wrap justify-end gap-2">
              <button
                onClick={() => setPrivPath(null)}
                className="rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt"
              >
                {tr("folders.privCancel")}
              </button>
              <button
                onClick={() => {
                  const p = privPath;
                  setPrivPath(null);
                  void doMount(p, true);
                }}
                className="rounded-lg border border-amber-500/40 px-3 py-1.5 text-xs text-amber-300 hover:bg-amber-500/10"
              >
                {tr("folders.privAuto")}
              </button>
              <button
                onClick={() => {
                  const p = privPath;
                  setPrivPath(null);
                  void doMount(p, false);
                }}
                className="rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90"
              >
                {tr("folders.privThisSession")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
