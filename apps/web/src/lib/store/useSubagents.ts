"use client";

import { useCallback, useRef } from "react";
import type { SessionMeta, SubagentSpawnEvent, SubagentStatusEvent } from "../types";
import { uiText } from "../../i18n/uiText";

export function useSubagents({
  setSessions,
  markSessionUnread,
}: {
  setSessions: (updater: (prev: SessionMeta[]) => SessionMeta[]) => void;
  markSessionUnread: (id: string) => void;
}) {
  // Stable setter from the sessions slice; the ref keeps the dependency
  // arrays below identical to the pre-split store.
  const setSessionsRef = useRef(setSessions);
  // ---- subagent（subagent-design.md §4.1/§6.1）----
  const notifySubagentSpawned = useCallback((ev: SubagentSpawnEvent) => {
    if (!ev.session_id) return;
    const now = Date.now() / 1000;
    setSessionsRef.current((prev) => {
      const idx = prev.findIndex((s) => s.id === ev.session_id);
      const patch: Partial<SessionMeta> = {
        type: (ev.type as SessionMeta["type"]) || "subagent",
        parent_session_id: ev.parent_session_id,
        depth: typeof ev.depth === "number" ? ev.depth : 0,
        subagent: {
          goal: ev.goal ?? "",
          constraints: ev.constraints ?? "",
          acceptance: ev.acceptance ?? "",
          origin: ev.origin ?? "agent",
          status: "running",
          result_summary: "",
          agent_type: ev.agent_type ?? "",
        },
      };
      if (idx >= 0) {
        // 已在列表（boot reload 先到）：只叠加子代字段与标题。
        return prev.map((s, i) =>
          i === idx ? { ...s, ...patch, title: ev.title || s.title } : s,
        );
      }
      // 即时事件先于任何 reload：合成一行占位，服务端字段（provider/model 等）
      // 由下一次 reloadSessions 补齐。图标用 boxes（子代理组语义），渲染时子行
      // 以状态 emoji 为主，图标只是兜底。
      return [
        {
          id: ev.session_id,
          title: ev.title || ev.goal || uiText("subtask.fallbackTitle"),
          icon: ev.icon ?? "boxes",
          agent_id: null,
          provider: "",
          model: "",
          created: now,
          updated: now,
          ...patch,
        },
        ...prev,
      ];
    });
  }, []);

  const notifySubagentStatus = useCallback((ev: SubagentStatusEvent) => {
    if (!ev.session_id || !ev.status) return;
    // 终态且用户没在盯这个子会话 → 侧栏未读小圆点（结果再也不会自己消失，
    // 打开子会话看过才清）。watching 判定在 markSessionUnread 内部。
    if (ev.status === "done" || ev.status === "failed" || ev.status === "stopped") {
      markSessionUnread(ev.session_id);
    }
    setSessionsRef.current((prev) =>
      prev.map((s) =>
        s.id === ev.session_id
          ? {
              ...s,
              updated: Date.now() / 1000,
              // delegation 行的终态打在顶层 stop_reason 上(meta 无 subagent 对象);
              // 不回填则回放页的 3s 轮询永不停、侧栏状态点一直转 running
              stop_reason:
                s.type === "delegation"
                  ? ev.status === "done"
                    ? "success"
                    : ev.status === "failed"
                      ? "error"
                      : s.stop_reason
                  : s.stop_reason,
              subagent: s.subagent
                ? {
                    ...s.subagent,
                    status: ev.status,
                    result_summary: ev.result_summary ?? s.subagent.result_summary,
                  }
                : s.subagent,
            }
          : s,
      ),
    );
  }, [markSessionUnread]);

  return { notifySubagentSpawned, notifySubagentStatus };
}
