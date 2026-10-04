"use client";

/**
 * Workspace root switcher at the top of the code panel (design §3.4 "根区").
 *
 * A session can expose several roots (mounts + the session workspace); this
 * lists them with their rw/ro access and, for git roots, the current branch.
 * A root whose folder vanished is shown greyed with an explanation — never an
 * error (design §3.4). S1 renders git branch only; no git decorations.
 *
 * S4 adds the two entries the S1 brief deferred (design §8.4):
 *   - 「＋ 添加文件夹…」 — an INLINE path input (no `window.prompt`, brief §3.4)
 *     wired by the panel to the existing `POST /api/folders` +
 *     `PUT /api/sessions/{id}/context`. RootBar owns the input UI and the
 *     error display; the panel owns the request (it is the only place that
 *     knows the session's current mount set and primary folder).
 *   - a 「切为读写」 button on an `ro` root, wired to `PATCH /api/folders/{id}`.
 * No backend is added for either.
 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import { GitBranch, Loader2, Plus, RefreshCw } from "lucide-react";
import type { CodeRoot } from "@/lib/codeTypes";
import { cn } from "@/lib/utils";

export interface RootBarProps {
  roots: CodeRoot[];
  activeRootId: string | null;
  onSelectRoot: (id: string) => void;
  onRefresh: () => void;
  refreshing?: boolean;
  /** Register + mount a folder into this session (panel-implemented via the
   *  existing `/api/folders` and `/api/sessions/{id}/context` endpoints).
   *  Resolves to a Chinese error message, or `null` on success. */
  onAddFolder?: (path: string) => Promise<string | null> | string | null;
  /** Flip an `ro` root to `rw` (panel-implemented via `PATCH /api/folders/{id}`).
   *  Resolves to a Chinese error message, or `null` on success. */
  onMakeRootWritable?: (rootId: string) => Promise<string | null> | string | null;
  className?: string;
}

export function RootBar({
  roots,
  activeRootId,
  onSelectRoot,
  onRefresh,
  refreshing = false,
  onAddFolder,
  onMakeRootWritable,
  className,
}: RootBarProps) {
  // code 域 catalog：本组件文案收在 code.root（个别复用既有 key：panel/fs/delete/errors）
  const t = useTranslations("code");
  const [adding, setAdding] = useState(false);
  const [path, setPath] = useState("");
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // Root ids whose 「切为读写」 request is in flight.
  const [rwBusy, setRwBusy] = useState<Record<string, boolean>>({});

  const closeAdd = () => {
    setAdding(false);
    setPath("");
    setErr(null);
  };

  const submitAdd = async () => {
    if (!onAddFolder || busy) return;
    const p = path.trim();
    if (!p) {
      setErr(t("root.pathRequired"));
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      const message = await onAddFolder(p);
      if (message) setErr(message);
      else closeAdd();
    } catch (e) {
      setErr(e instanceof Error ? e.message : t("fs.mountFailed"));
    } finally {
      setBusy(false);
    }
  };

  const makeWritable = async (id: string) => {
    if (!onMakeRootWritable || rwBusy[id]) return;
    setRwBusy((prev) => ({ ...prev, [id]: true }));
    try {
      const message = await onMakeRootWritable(id);
      if (message) setErr(message);
    } catch (e) {
      setErr(e instanceof Error ? e.message : t("fs.switchFailed"));
    } finally {
      setRwBusy((prev) => ({ ...prev, [id]: false }));
    }
  };

  return (
    <div className={cn("flex flex-col", className)}>
      <div className="flex items-center justify-between px-2 py-1">
        <span className="text-[11px] font-medium tracking-wide text-faint">{t("panel.workspace")}</span>
        <button
          type="button"
          onClick={onRefresh}
          title={t("root.refresh")}
          aria-label={t("root.refresh")}
          className="rounded p-0.5 text-faint transition-colors hover:text-txt"
        >
          <RefreshCw className={cn("h-3.5 w-3.5", refreshing && "animate-spin")} />
        </button>
      </div>

      {!roots.length ? (
        <div className="px-2 py-1 text-[11px] text-faint">{t("panel.noWorkspace")}</div>
      ) : (
        <ul role="list" className="flex flex-col">
          {roots.map((r) => {
            const active = r.id === activeRootId;
            return (
              <li key={r.id}>
                <button
                  type="button"
                  onClick={() => onSelectRoot(r.id)}
                  aria-current={active ? "true" : undefined}
                  title={r.path}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-xs transition-colors",
                    active ? "bg-line2/50 text-txt" : "text-muted hover:bg-line2/40",
                    // Missing roots are still selectable (their files just fail
                    // with root-missing) but visually demoted.
                    r.missing && "opacity-50",
                  )}
                >
                  <span
                    className={cn(
                      "h-1.5 w-1.5 shrink-0 rounded-full",
                      active ? "bg-indigo" : "bg-faint/60",
                    )}
                  />
                  <span className="min-w-0 flex-1 truncate">{r.name}</span>
                  {r.is_repo && r.branch && (
                    <span className="flex shrink-0 items-center gap-0.5 text-[10px] text-faint">
                      <GitBranch className="h-3 w-3" />
                      <span className="max-w-[72px] truncate">{r.branch}</span>
                    </span>
                  )}
                  <span
                    className={cn(
                      "shrink-0 rounded px-1 text-[10px] font-medium",
                      r.access === "rw" ? "text-green" : "text-faint",
                    )}
                  >
                    {r.access}
                  </span>
                </button>
                {/* An `ro` mount is the one case where writes are unavailable
                    for a reason the user can fix — offer the fix right here
                    (design §3.4 验收 5). */}
                {r.access === "ro" && onMakeRootWritable ? (
                  <div className="pb-1 pl-5 pr-2">
                    <button
                      type="button"
                      onClick={() => void makeWritable(r.id)}
                      disabled={!!rwBusy[r.id]}
                      title={t("root.makeWritableTitle")}
                      className="flex items-center gap-1 text-[10px] text-indigo transition-colors hover:text-indigo2 disabled:opacity-50"
                    >
                      {rwBusy[r.id] ? (
                        <Loader2 className="h-3 w-3 animate-spin" />
                      ) : (
                        <RefreshCw className="h-3 w-3" />
                      )}
                      {t("root.makeWritable")}
                    </button>
                  </div>
                ) : null}
                {r.missing && (
                  <div className="pb-1 pl-5 pr-2 text-[10px] text-faint">
                    {t("errors.root-missing")}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {/* ＋ 添加文件夹… — inline input, never `window.prompt` (brief §3.4). */}
      {adding ? (
        <div className="px-2 pb-1 pt-0.5">
          <div className="flex items-center gap-1.5">
            <input
              autoFocus
              value={path}
              onChange={(e) => {
                setPath(e.target.value);
                setErr(null);
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  void submitAdd();
                } else if (e.key === "Escape") {
                  e.preventDefault();
                  closeAdd();
                }
              }}
              placeholder={t("root.pathPlaceholder")}
              className="min-w-0 flex-1 rounded-md border border-line2 bg-card px-2 py-1 text-[11px] text-txt outline-none placeholder:text-faint focus:border-indigo/60"
            />
            <button
              type="button"
              onClick={() => void submitAdd()}
              disabled={busy || !path.trim()}
              className="shrink-0 rounded-md bg-indigo px-2 py-1 text-[11px] font-medium text-white transition-opacity hover:opacity-90 disabled:opacity-50"
            >
              {busy ? t("root.mounting") : t("root.mount")}
            </button>
            <button
              type="button"
              onClick={closeAdd}
              className="shrink-0 rounded p-0.5 text-faint hover:text-txt"
              title={t("delete.cancel")}
            >
              ✕
            </button>
          </div>
          <div className="mt-0.5 text-[10px] text-faint">{t("root.mountHint")}</div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => {
            setAdding(true);
            setErr(null);
          }}
          disabled={!onAddFolder}
          title={onAddFolder ? t("root.addTitle") : t("root.addUnavailable")}
          className="flex items-center gap-1.5 rounded-md px-2 py-1 text-left text-[11px] text-faint transition-colors hover:bg-line2/40 hover:text-txt disabled:opacity-50"
        >
          <Plus className="h-3 w-3" /> {t("root.addFolder")}
        </button>
      )}

      {err ? <div className="px-2 pb-1 text-[10px] text-red">{err}</div> : null}
    </div>
  );
}