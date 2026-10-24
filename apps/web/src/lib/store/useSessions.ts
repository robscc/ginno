"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import * as api from "../runtime";
import type { ScheduleRun, SessionMeta } from "../types";

// Last active session, restored on boot (open-experience redesign). Only real
// ids are stored; visiting home (null) keeps the previous id so a relaunch
// still resumes where the user left off.
export const LAST_SESSION_KEY = "ginno-last-session";

export function useSessions({
  sessionsRef,
  setActiveRunId,
  setScheduleRun,
  setSessionError,
  pruneTreeCollapsed,
}: {
  sessionsRef: { current: SessionMeta[] };
  setActiveRunId: (id: string | null) => void;
  setScheduleRun: (run: ScheduleRun | null) => void;
  setSessionError: (message: string | null) => void;
  pruneTreeCollapsed: (doomed: Set<string>) => void;
}) {
  const [sessions, setSessions] = useState<SessionMeta[]>([]);
  const [activeSessionId, setActiveSessionId] = useState<string | null>(null);

  // Cross-slice handles are stable setState dispatchers / useCallbacks / refs.
  // The refs keep the dependency arrays below identical to the pre-split store
  // (the original captured them as same-scope constants).
  const runViewHandlesRef = useRef({ setActiveRunId, setScheduleRun });
  const setSessionErrorRef = useRef(setSessionError);
  const handlesRef = useRef({ sessionsRef, pruneTreeCollapsed });

  // Persist the last real session so boot can resume it (AppShell reads
  // LAST_SESSION_KEY). Home (null) intentionally keeps the previous id.
  useEffect(() => {
    try {
      if (activeSessionId) localStorage.setItem(LAST_SESSION_KEY, activeSessionId);
    } catch {
      /* storage unavailable */
    }
    // Floating quick-chat "follow" mode (docs/floating-window-design.md §2.3):
    // announce the main window's active session so the pin window can mirror
    // it. The pin's OWN provider instance must never rebroadcast — it would
    // clobber the followed id with its (always null) activeSessionId.
    if (typeof window !== "undefined" && window.location.pathname !== "/pin") {
      try {
        const bc = new BroadcastChannel("ginno-active-session");
        bc.postMessage({ type: "active", sessionId: activeSessionId });
        bc.close();
      } catch {
        /* BroadcastChannel unavailable */
      }
    }
  }, [activeSessionId]);

  useEffect(() => {
    handlesRef.current.sessionsRef.current = sessions;
  }, [sessions]);

  const activeSessionRef = useRef<string | null>(null);
  useEffect(() => {
    activeSessionRef.current = activeSessionId;
  }, [activeSessionId]);

  // ---- 侧栏会话未读小圆点（服务端 session_unread 单一事实源）----
  // 内存 map 只是乐观覆盖层：message.end 瞬时先亮，reloadSessions 随后以
  // 服务端装饰的 unread 布尔回写权威态；跨重启的紫点由服务端
  // last_finished/seen 落盘保证（不再走 localStorage）。
  const [unreadSessions, setUnreadSessionsState] = useState<Record<string, number>>({});
  const unreadRef = useRef<Record<string, number>>({});
  // 写时读 ref + 按 sessions 镜像裁剪，均与 treeCollapsed 同模式。
  const setUnreadMap = useCallback((map: Record<string, number>) => {
    const sessions = handlesRef.current.sessionsRef.current;
    let next = map;
    if (sessions.length) {
      const alive = new Set(sessions.map((s) => s.id));
      const pruned: Record<string, number> = {};
      for (const [k, v] of Object.entries(map)) {
        if (alive.has(k)) pruned[k] = v;
      }
      next = pruned;
    }
    unreadRef.current = next;
    setUnreadSessionsState(next);
  }, []);
  // 「正在看」判定与 turn-done 通知一致（handle.ts message.end）：窗口可见
  // 且该会话是当前活跃会话 → turn 结束不算未读。窗口隐藏期间活跃会话跑完
  // 会标记，回到窗口即由下方 visibility 监听清除。
  const watchingSession = useCallback((id: string) => {
    return (
      activeSessionRef.current === id &&
      typeof document !== "undefined" &&
      document.visibilityState === "visible"
    );
  }, []);
  const markSessionUnread = useCallback(
    (id: string) => {
      if (!id || watchingSession(id)) return;
      if (unreadRef.current[id] !== undefined) return;
      setUnreadMap({ ...unreadRef.current, [id]: Date.now() / 1000 });
    },
    [setUnreadMap, watchingSession],
  );
  const clearSessionUnread = useCallback(
    (id: string) => {
      if (!id) return;
      // 已读上报服务端（fire-and-forget，幂等）：服务端在 turn 收口时已写
      // last_finished，「正在看」时也要上报才能压掉它。失败不重试——下一次
      // clear 路径或 reloadSessions 会再对齐。
      void api.markSessionSeen(id).catch(() => {});
      if (unreadRef.current[id] === undefined) return;
      const next = { ...unreadRef.current };
      delete next[id];
      setUnreadMap(next);
    },
    [setUnreadMap],
  );
  // 窗口重新可见/聚焦：正在盯着的活跃会话即视为已读（隐藏期间跑完的 turn
  // 已标记，回来直接看到结果就不留点）。
  useEffect(() => {
    const clearActiveIfVisible = () => {
      const sid = activeSessionRef.current;
      if (sid && document.visibilityState === "visible") clearSessionUnread(sid);
    };
    window.addEventListener("focus", clearActiveIfVisible);
    document.addEventListener("visibilitychange", clearActiveIfVisible);
    return () => {
      window.removeEventListener("focus", clearActiveIfVisible);
      document.removeEventListener("visibilitychange", clearActiveIfVisible);
    };
  }, [clearSessionUnread]);

  // Consumer-facing setter：切换/清空会话时同步关掉 run 视图（中央区二选一）。
  // 内部 boot 路径仍用裸 setActiveSessionId；newSession / removeSession 里也
  // 各自清一次。
  const setActiveSession = useCallback(
    (id: string | null) => {
      setActiveSessionId(id);
      runViewHandlesRef.current.setActiveRunId(null);
      runViewHandlesRef.current.setScheduleRun(null);
      // 打开会话 = 读过它 → 清未读小圆点
      if (id) clearSessionUnread(id);
    },
    [clearSessionUnread],
  );

  const reloadSessions = useCallback(async () => {
    try {
      const rows = await api.listSessions();
      setSessions(rows);
      // 服务端未读权威态回写（收件箱语义，session_unread）：服务端说未读而
      // 乐观层没标的补上（时间戳沿用旧值），服务端已读的撤掉本地残留。正在
      // 看的会话即便服务端还有未读（例：重连 reconcile 竞态）也不亮点——
      // 顺带上报已读自愈。
      const next: Record<string, number> = {};
      for (const s of rows) {
        if (!s.unread) continue;
        if (watchingSession(s.id)) {
          void api.markSessionSeen(s.id).catch(() => {});
          continue;
        }
        next[s.id] = unreadRef.current[s.id] ?? Date.now() / 1000;
      }
      setUnreadMap(next);
    } catch {
      /* ignore */
    }
  }, [setUnreadMap, watchingSession]);

  const creatingRef = useRef(false);
  const newSession = useCallback(
    async (
      agent_id?: string,
      opts?: {
        title?: string;
        provider?: string;
        model?: string;
        workflow_id?: string;
        /** 出生即挂载(home-mount-picker-design.md §3)——首页 composer 的 pending 目录 */
        context_folders?: string[];
        primary_folder?: string | null;
      },
    ) => {
      if (creatingRef.current) return null;
      creatingRef.current = true;
      setSessionErrorRef.current(null);
      try {
        const s = await api.createSession({
          workspace: process.env.NEXT_PUBLIC_WORKSPACE ?? "/tmp/gw",
          agent_id,
          title: opts?.title,
          provider: opts?.provider,
          model: opts?.model,
          workflow_id: opts?.workflow_id,
          context_folders: opts?.context_folders,
          primary_folder: opts?.primary_folder ?? undefined,
        });
        if (s && s.ok !== false && s.id) {
          setSessions((prev) => [s, ...prev.filter((x) => x.id !== s.id)]);
          setActiveSessionId(s.id);
          runViewHandlesRef.current.setActiveRunId(null); // 视图切到新会话，run 视图/定时回放一并关闭
          runViewHandlesRef.current.setScheduleRun(null);
          setSessionErrorRef.current(null);
          return s;
        }
        // server returned ok:false (e.g. no provider enabled / missing key)
        setSessionErrorRef.current(s?.error || "Failed to create session: enable a model provider in Settings → Model API");
      } catch {
        setSessionErrorRef.current("Failed to create session: cannot reach the runtime (is the sidecar running?)");
      } finally {
        creatingRef.current = false;
      }
      return null;
    },
    [],
  );

  const setSessionAgent = useCallback((id: string, agentId: string) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, agent_id: agentId } : s)));
    api
      .patchSession(id, { agent_id: agentId })
      .then((r) => {
        // reconcile with the server-computed title (auto titles follow the agent)
        if (r?.session) {
          setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...r.session } : s)));
        }
      })
      .catch(() => {
        /* ignore */
      });
  }, []);

  const removeSession = useCallback(async (id: string, opts?: { cascade?: boolean }) => {
    // optimistic remove; if it was active, land on home (lazy creation will
    // make the next send start a fresh session — no phantom auto-session).
    // 级联删除（subagent-design.md §5.8）时把全部后代一并从列表移除——后端沿
    // parent_session_id 反向索引删除，前端这里只做 UI 即时一致性。
    // 被删集合先在 sessions 镜像上算出（含嵌套后代）——折叠覆盖的清理复用它。
    const doomed = new Set<string>([id]);
    if (opts?.cascade) {
      let grew = true;
      while (grew) {
        grew = false;
        for (const s of handlesRef.current.sessionsRef.current) {
          if (s.parent_session_id && doomed.has(s.parent_session_id) && !doomed.has(s.id)) {
            doomed.add(s.id);
            grew = true;
          }
        }
      }
    }
    // 被删会话的折叠覆盖键一并剪掉并持久化（不走 persistTreeCollapsed：它按
    // sessions 镜像裁剪，而镜像此刻还没更新，会把待删键又加回来）。
    handlesRef.current.pruneTreeCollapsed(doomed);
    // 未读小圆点同理：被删会话（含级联后代）的标记一并剪掉（服务端的
    // last_finished/seen 随会话删除一并消失，无需上报）。
    const doomedUnread = Object.keys(unreadRef.current).filter((k) => doomed.has(k));
    if (doomedUnread.length) {
      const next = { ...unreadRef.current };
      for (const k of doomedUnread) delete next[k];
      unreadRef.current = next;
      setUnreadSessionsState(next);
    }
    // 正在查看的会话（或其祖先被级联删除）没了，run 视图也一并关闭。
    const cur = activeSessionRef.current;
    const activeDoomed = !!cur && (doomed.has(cur) || cur === id);
    setSessions((prev) => {
      const next = prev.filter((s) => !doomed.has(s.id));
      setActiveSessionId((current) => {
        if (current !== id && !(current && doomed.has(current))) return current;
        return null;
      });
      return next;
    });
    if (activeDoomed) runViewHandlesRef.current.setActiveRunId(null);
    if (activeDoomed) runViewHandlesRef.current.setScheduleRun(null);
    try {
      await api.deleteSession(id, opts?.cascade);
    } catch {
      /* ignore — reconcile on next reload */
    }
  }, []);

  const renameSession = useCallback(async (id: string, title: string) => {
    const trimmed = title.trim();
    if (!trimmed) return;
    setSessions((prev) =>
      prev.map((s) => (s.id === id ? { ...s, title: trimmed, title_auto: false } : s)),
    );
    try {
      const r = await api.patchSession(id, { title: trimmed });
      if (r?.session) {
        setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...r.session } : s)));
      }
    } catch {
      /* ignore */
    }
  }, []);

  const pinSession = useCallback(async (id: string, pinned: boolean) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, pinned } : s)));
    try {
      const r = await api.patchSession(id, { pinned });
      if (r?.session) {
        setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...r.session } : s)));
      }
    } catch {
      /* ignore — reconcile on next reload */
    }
  }, []);

  const applySessionPatch = useCallback((id: string, patch: Partial<SessionMeta>) => {
    setSessions((prev) => prev.map((s) => (s.id === id ? { ...s, ...patch } : s)));
  }, []);

  return {
    sessions,
    setSessions,
    activeSessionId,
    activeSessionRef,
    unreadSessions,
    markSessionUnread,
    clearSessionUnread,
    watchingSession,
    setActiveSession,
    reloadSessions,
    newSession,
    setSessionAgent,
    removeSession,
    renameSession,
    pinSession,
    applySessionPatch,
  };
}
