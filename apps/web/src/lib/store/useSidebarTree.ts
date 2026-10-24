"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import type { SessionMeta } from "../types";

// 侧栏子树折叠覆盖（subagent-design.md §6.1，从 AppShell 本地状态上收持久化）。
// shape: {session_id: boolean}——只存用户显式点过 chevron 的会话，其余走默认。
const TREE_COLLAPSED_KEY = "ginno-sidebar-tree";

export function useSidebarTree(sessionsRef: { current: SessionMeta[] }) {
  // The shared sessions mirror is a stable ref owned by the provider; wrapping
  // it locally keeps persistTreeCollapsed's dependency array identical to the
  // pre-split store (where it was a same-scope useRef).
  const handlesRef = useRef({ sessionsRef });
  // ---- 侧栏子树折叠覆盖（ginno-sidebar-tree，自 AppShell 本地状态上收）----
  const [treeCollapsed, setTreeCollapsedState] = useState<Record<string, boolean>>({});
  // Ref mirror for write-time reads（persist/prune 不能读过期 state——与
  // rightPanelWidthByTabRef 同理），外加一份 sessions 镜像供写时裁剪。
  const treeCollapsedRef = useRef<Record<string, boolean>>({});

  // Hydrate once on mount（与上方面板偏好同一模式：SSR 渲染默认值，挂载后修）。
  useEffect(() => {
    try {
      const raw = localStorage.getItem(TREE_COLLAPSED_KEY);
      if (!raw) return;
      const v = JSON.parse(raw) as Record<string, unknown>;
      if (!v || typeof v !== "object") return;
      const next: Record<string, boolean> = {};
      for (const [k, val] of Object.entries(v)) {
        if (typeof val === "boolean") next[k] = val;
      }
      treeCollapsedRef.current = next;
      setTreeCollapsedState(next);
    } catch {
      /* corrupted prefs — keep defaults */
    }
  }, []);

  // Persist a write，顺带裁掉已不存在的会话键（删除后不留陈旧残留）。boot 尚未
  // 拿到会话列表时跳过裁剪——空镜像会把整张表清光。
  const persistTreeCollapsed = useCallback((map: Record<string, boolean>) => {
    const sessions = handlesRef.current.sessionsRef.current;
    let next = map;
    if (sessions.length) {
      const alive = new Set(sessions.map((s) => s.id));
      const pruned: Record<string, boolean> = {};
      for (const [k, v] of Object.entries(map)) {
        if (alive.has(k)) pruned[k] = v;
      }
      next = pruned;
    }
    treeCollapsedRef.current = next;
    setTreeCollapsedState(next);
    try {
      localStorage.setItem(TREE_COLLAPSED_KEY, JSON.stringify(next));
    } catch {
      /* storage unavailable */
    }
  }, []);

  const setTreeCollapsed = useCallback(
    (id: string, collapsed: boolean) => {
      persistTreeCollapsed({ ...treeCollapsedRef.current, [id]: collapsed });
    },
    [persistTreeCollapsed],
  );

  // 被删会话的折叠覆盖键一并剪掉并持久化（不走 persistTreeCollapsed：它按
  // sessions 镜像裁剪，而镜像此刻还没更新，会把待删键又加回来）。
  const pruneTreeCollapsed = useCallback((doomed: Set<string>) => {
    const doomedKeys = Object.keys(treeCollapsedRef.current).filter((k) => doomed.has(k));
    if (doomedKeys.length) {
      const next = { ...treeCollapsedRef.current };
      for (const k of doomedKeys) delete next[k];
      treeCollapsedRef.current = next;
      setTreeCollapsedState(next);
      try {
        localStorage.setItem(TREE_COLLAPSED_KEY, JSON.stringify(next));
      } catch {
        /* storage unavailable */
      }
    }
  }, []);

  return {
    treeCollapsed,
    setTreeCollapsed,
    pruneTreeCollapsed,
  };
}
