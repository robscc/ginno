"use client";

/**
 * ChangesPanel — CodePanel 底部可折叠「变更文件」子窗
 * (docs/code-panel-diff-design.md §A).
 *
 * 数据源：session socket 推送的 codeSessionChanges（绝对路径 → 基线 + op +
 * turn 标签，最新写覆盖旧值）。列表每行：相对路径、op 徽标（A=新建/M=修改）、
 * turn 标签、+N -M 行数统计。统计不依赖 runtime numstat（/api/code/git 只回
 * status 字母）：直接拉该文件的 session 基线 diff（GET /api/code/diff），在
 * 前端按「公共前后缀裁剪」近似计数——对典型的 Agent 编辑（局部改动）足够
 * 准确；超大文件 / degraded 基线显示「—」。视图切换 Current / Per-Turn /
 * All；点击一行联动主编辑器：打开该文件并进入 inline diff（复用 §B 调度器，
 * 由父组件通过 onOpenDiff 注入）。
 *
 * 空态：没有任何改动时不渲染列表，只保留一行「暂无改动文件」的窄条提示。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, FilePlus2, FilePen, FileQuestion } from "lucide-react";
import { useTranslations } from "next-intl";
import { codeDiff } from "@/lib/runtime";
import type { CodeRoot } from "@/lib/codeTypes";
import type { SessionChange } from "@/lib/store/useCodePanel";
import { lineDiffCounts } from "./changesStats";
import { usePanelWidth } from "@/components/workflow/studio/PanelResizer";
import { cn } from "@/lib/utils";

const PROJECT_SLUG = "default";
const STATS_MAX_CHARS = 200_000;

export type ChangesView = "current" | "turn" | "all";

/** One flat row of the changes list. */
export interface ChangeRow {
  /** Absolute path — the session map's key and the stats-cache key. */
  abs: string;
  rootId: string;
  /** Root-relative path shown in the UI. */
  rel: string;
  /** "A" = created by the agent / git-added, "M" = modified, git letters pass
   *  through for git-only rows (D/R/!…). */
  op: string;
  turnId?: string;
  /** session = agent write this session; git = only known from git status. */
  source: "session" | "git";
}


export function ChangesPanel({
  sessionId,
  roots,
  sessionChanges,
  gitStatus,
  gitRootId,
  onOpenDiff,
  className,
}: {
  sessionId: string;
  /** All workspace roots — abs paths are resolved to (rootId, rel) here. */
  roots: CodeRoot[];
  /** Abs path → session change (newest-wins), pushed over the session socket. */
  sessionChanges: Record<string, SessionChange>;
  /** Git status letters for `gitRootId`, or null (not a repo / fetch failed). */
  gitStatus: Record<string, string> | null;
  gitRootId: string | null;
  /** Open the file in the main editor AND enter inline diff (§A ↔ §B link). */
  onOpenDiff: (rootId: string, rel: string) => void;
  className?: string;
}) {
  const t = useTranslations("code.changes");
  const [view, setView] = useState<ChangesView>("current");
  const [collapsed, setCollapsed] = useState(false);
  const [stats, setStats] = useState<Record<string, { add: number; del: number } | "na">>({});
  const statsSeqRef = useRef(0);

  // Persisted collapse toggle. Read after mount so SSR and the first client
  // render agree (same posture as PanelResizer's persisted width).
  useEffect(() => {
    try {
      setCollapsed(window.localStorage.getItem("ginno:code-changes-collapsed") === "1");
    } catch {
      // storage unavailable — stay expanded
    }
  }, []);
  const toggleCollapsed = useCallback(() => {
    setCollapsed((prev) => {
      const next = !prev;
      try {
        window.localStorage.setItem("ginno:code-changes-collapsed", next ? "1" : "0");
      } catch {
        // ignore
      }
      return next;
    });
  }, []);

  // ---- rows ------------------------------------------------------------------
  /** Abs path → its best-matching root (longest prefix wins), mirroring the
   *  panel's own splitRootPath. */
  const splitRootPath = useCallback(
    (abs: string): { rootId: string; rel: string } | null => {
      let best: { rootId: string; rel: string; len: number } | null = null;
      for (const r of roots) {
        const base = r.path.endsWith("/") ? r.path.slice(0, -1) : r.path;
        if (!base) continue;
        if (abs === base || abs.startsWith(base + "/")) {
          if (!best || base.length > best.len) {
            best = { rootId: r.id, rel: abs.slice(base.length + 1), len: base.length };
          }
        }
      }
      return best ? { rootId: best.rootId, rel: best.rel } : null;
    },
    [roots],
  );

  const rows = useMemo<ChangeRow[]>(() => {
    const out: ChangeRow[] = [];
    const seen = new Set<string>();
    for (const [abs, ch] of Object.entries(sessionChanges)) {
      const m = splitRootPath(abs);
      if (!m || !m.rel) continue;
      seen.add(abs);
      out.push({
        abs,
        rootId: m.rootId,
        rel: m.rel,
        op: ch.op === "write" ? "A" : "M",
        turnId: ch.turnId,
        source: "session",
      });
    }
    // Git-only entries (changed outside the agent's file tools, or the fetch
    // raced ahead of the socket events) fill the "all" view when a repo exists.
    if (gitStatus && gitRootId) {
      for (const [rel, letter] of Object.entries(gitStatus)) {
        if (!rel) continue;
        const base = roots.find((r) => r.id === gitRootId)?.path ?? "";
        const abs = rel ? `${base}/${rel}` : base;
        if (seen.has(abs)) continue;
        out.push({ abs, rootId: gitRootId, rel, op: letter || "M", source: "git" });
      }
    }
    // Newest writes first so the list reads top-down like a feed.
    out.sort((x, y) => (sessionChanges[y.abs]?.at ?? 0) - (sessionChanges[x.abs]?.at ?? 0));
    return out;
  }, [sessionChanges, gitStatus, gitRootId, roots, splitRootPath]);

  const sessionRows = useMemo(() => rows.filter((r) => r.source === "session"), [rows]);

  const turnGroups = useMemo(() => {
    const groups = new Map<string, ChangeRow[]>();
    for (const r of sessionRows) {
      const key = r.turnId ?? "";
      const arr = groups.get(key);
      if (arr) arr.push(r);
      else groups.set(key, [r]);
    }
    return [...groups.entries()];
  }, [sessionRows]);

  // ---- lazy per-file line stats ---------------------------------------------
  // One sequential fetch pass per rows-snapshot; a newer snapshot cancels the
  // older one via the sequence guard (no ConcurrentMode dances needed).
  useEffect(() => {
    const seq = ++statsSeqRef.current;
    void (async () => {
      for (const r of sessionRows) {
        if (seq !== statsSeqRef.current) return;
        if (stats[r.abs] !== undefined) continue;
        if (!sessionId) return;
        try {
          const d = await codeDiff(PROJECT_SLUG, sessionId, r.rootId, r.rel);
          if (seq !== statsSeqRef.current) return;
          const base = d.baseContent;
          const current = d.currentContent;
          if (
            d.degraded ||
            base == null ||
            current == null ||
            base.length > STATS_MAX_CHARS ||
            current.length > STATS_MAX_CHARS
          ) {
            setStats((prev) => ({ ...prev, [r.abs]: "na" }));
            continue;
          }
          setStats((prev) => ({
            ...prev,
            [r.abs]: lineDiffCounts(base, current),
          }));
        } catch {
          if (seq !== statsSeqRef.current) return;
          setStats((prev) => ({ ...prev, [r.abs]: "na" }));
        }
      }
    })();
    // `stats` intentionally excluded: the loop checks the mirror via closure on
    // each iteration and a mid-pass write must not restart the pass.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionRows, sessionId]);

  // ---- height (reuses PanelResizer's persisted-width hook, vertical) --------
  const [height, setHeight] = usePanelWidth("ginno:code-changes-h", 180, 96, 420);
  const heightRef = useRef(height);
  heightRef.current = height;

  const onResizeStart = useCallback(
    (e: React.PointerEvent) => {
      e.preventDefault();
      const startY = e.clientY;
      const startH = heightRef.current;
      const onMove = (ev: PointerEvent) => {
        setHeight(startH - (ev.clientY - startY));
      };
      const onUp = () => {
        window.removeEventListener("pointermove", onMove);
        window.removeEventListener("pointerup", onUp);
      };
      window.addEventListener("pointermove", onMove);
      window.addEventListener("pointerup", onUp);
    },
    [setHeight],
  );

  const renderRow = (r: ChangeRow) => {
    const st = stats[r.abs];
    return (
      <button
        key={r.abs}
        type="button"
        onClick={() => onOpenDiff(r.rootId, r.rel)}
        className="flex w-full items-center gap-1.5 rounded px-2 py-1 text-left text-[11px] text-muted hover:bg-card2 hover:text-txt"
        title={r.rel}
      >
        {r.source === "git" ? (
          <FileQuestion size={12} className="shrink-0 text-faint" />
        ) : r.op === "A" ? (
          <FilePlus2 size={12} className="shrink-0 text-green" />
        ) : (
          <FilePen size={12} className="shrink-0 text-amber" />
        )}
        <span className="min-w-0 flex-1 truncate">{r.rel}</span>
        {r.turnId ? (
          <span className="shrink-0 rounded bg-card2 px-1 py-px text-[10px] text-faint">
            {r.turnId}
          </span>
        ) : null}
        <span className="w-4 shrink-0 text-center font-medium text-faint" aria-label={r.op}>
          {r.op}
        </span>
        <span className="w-16 shrink-0 text-right font-mono text-[10px]">
          {r.source === "git" ? (
            <span className="text-faint">—</span>
          ) : st === undefined ? (
            <span className="text-faint">…</span>
          ) : st === "na" ? (
            <span className="text-faint">—</span>
          ) : (
            <>
              <span className="text-green">+{st.add}</span>{" "}
              <span className="text-red">-{st.del}</span>
            </>
          )}
        </span>
      </button>
    );
  };

  const viewTabs: { key: ChangesView; label: string }[] = [
    { key: "current", label: t("view.current") },
    { key: "turn", label: t("view.turn") },
    { key: "all", label: t("view.all") },
  ];

  // ---- empty state: a slim one-line hint, no resizable body -----------------
  if (!rows.length) {
    return (
      <div className={cn("shrink-0 border-t border-line bg-panel", className)}>
        <div className="flex items-center gap-1.5 px-3 py-1 text-[11px] text-faint">
          <ChevronUp size={12} />
          <span>{t("empty")}</span>
        </div>
      </div>
    );
  }

  return (
    <div className={cn("flex shrink-0 flex-col border-t border-line bg-panel", className)}>
      {/* Title bar: click toggles collapse; the top edge resizes when open. */}
      {!collapsed ? (
        <div
          onPointerDown={onResizeStart}
          className="h-1 shrink-0 cursor-row-resize"
          role="separator"
          aria-orientation="horizontal"
        />
      ) : null}
      <div className="flex shrink-0 items-center gap-1 px-2 py-1">
        <button
          type="button"
          onClick={toggleCollapsed}
          className="flex items-center gap-1.5 rounded px-1 py-0.5 text-[11px] text-txt hover:bg-card2"
          aria-expanded={!collapsed}
        >
          {collapsed ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
          <span>{t("title")}</span>
          <span className="text-faint">{sessionRows.length}</span>
        </button>
        {!collapsed ? (
          <div className="ml-auto flex shrink-0 items-center gap-0.5">
            {viewTabs.map((v) => (
              <button
                key={v.key}
                type="button"
                onClick={() => setView(v.key)}
                aria-pressed={view === v.key}
                className={cn(
                  "rounded px-1.5 py-0.5 text-[11px] transition-colors",
                  view === v.key ? "bg-card2 text-txt" : "text-faint hover:text-txt",
                )}
              >
                {v.label}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      {!collapsed ? (
        <div className="min-h-0 overflow-y-auto pb-1" style={{ height }}>
          {view === "turn" ? (
            turnGroups.map(([turnId, group]) => (
              <div key={turnId || "ungrouped"}>
                <div className="px-2 pb-0.5 pt-1.5 text-[10px] font-medium text-faint">
                  {turnId || t("turnUngrouped")}
                </div>
                {group.map(renderRow)}
              </div>
            ))
          ) : (
            (view === "current" ? sessionRows : rows).map(renderRow)
          )}
        </div>
      ) : null}
    </div>
  );
}
