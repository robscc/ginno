import { useCallback, useEffect, useRef, type Dispatch, type SetStateAction } from "react";
import * as api from "@/lib/runtime";
import type { Team, TeamMemberRow } from "@/lib/types";
import { consumeOpenMemberIntent } from "./openMemberIntent";

export type TeamView =
  | { kind: "main" }
  | { kind: "overview" }
  | { kind: "member"; instanceId: string; sessionId: string };

export function useTeamPinFollow(activeSessionId: string | null, view: TeamView, mainSessionId?: string) {
  // ---- P2-24：pin 浮窗 follow 联动 ----
  // 核实结论：pin 的 follow 模式经 BroadcastChannel "ginno-active-session"
  // 镜像主窗口 store 的 activeSessionId（useSessions.ts 广播 / PinApp.tsx
  // 订阅）。但 /team 导航不改 store.activeSessionId，TeamShell 的视图状态
  // 自包含 → pin 停留在最后一个普通聊天会话，团队活动完全不可见。
  // 最小修复：把团队空间当前展示的会话（广场主会话 / 成员工作台会话）广播
  // 到同一频道——团队会话是真实 session，runtime 把 turn 广播给该会话的
  // 所有 socket，pin 的 PinStream 直接跟流（含权限代答）；team.reply /
  // team.changed 由 PinStream 新增的 case 消费（状态徽标与主窗口同语义）。
  // 离开 /team（unmount）时恢复广播 store 的 activeSessionId，follow 语义
  // 交还主窗口导航。
  const storeActiveRef = useRef<string | null>(activeSessionId);
  useEffect(() => {
    storeActiveRef.current = activeSessionId;
  });
  const pinFollowSid =
    view.kind === "member" ? view.sessionId : mainSessionId ?? null;
  useEffect(() => {
    if (!pinFollowSid) return;
    let bc: BroadcastChannel | null = null;
    try {
      bc = new BroadcastChannel("ginno-active-session");
      // 晚到的 pin（主窗口已在 /team 时才打开浮窗）：PinApp 挂载发 hello，
      // 这里应答当前会话——否则 pin 只能拿到 localStorage 里的旧 seed。
      bc.onmessage = (e) => {
        const d = e.data as { type?: string } | null;
        if (d?.type === "hello") {
          try {
            bc?.postMessage({ type: "active", sessionId: pinFollowSid });
          } catch {
            /* noop */
          }
        }
      };
      bc.postMessage({ type: "active", sessionId: pinFollowSid });
    } catch {
      /* BroadcastChannel 不可用——pin 回退 localStorage seed */
      bc = null;
    }
    return () => {
      try {
        bc?.close();
      } catch {
        /* noop */
      }
    };
  }, [pinFollowSid]);
  useEffect(
    () => () => {
      try {
        const bc = new BroadcastChannel("ginno-active-session");
        bc.postMessage({ type: "active", sessionId: storeActiveRef.current });
        bc.close();
      } catch {
        /* noop */
      }
    },
    [],
  );
}

export function useTeamMemberNavigation({
  teamId, setTeamId, setView, refresh,
}: {
  teamId: string | null;
  setTeamId: Dispatch<SetStateAction<string | null>>;
  setView: Dispatch<SetStateAction<TeamView>>;
  refresh: () => Promise<Team[]>;
}) {
  // ---- 成员工作台跳转（TeamReplyCard 的「进入工作台」也走这个事件）----
  const openMemberSession = useCallback(
    (instanceId: string, sessionId: string, teamIdOverride?: string) => {
      // P2-25：跨团队跳转（回帖卡带 team_id）先选中团队再进工作台。
      const tid = teamIdOverride ?? teamId;
      if (teamIdOverride && teamIdOverride !== teamId) setTeamId(teamIdOverride);
      setView({ kind: "member", instanceId, sessionId });
      // 打开即标记 seen：done → idle（§3.3 收件箱语义）。P2-18：seen 失败
      // 不阻塞跳转（下次刷新会对账），不再裸 .then。
      if (tid) {
        void api
          .markTeamMemberSeen(tid, instanceId)
          .then(() => refresh())
          .catch(() => {});
      }
    },
    [teamId, refresh, setTeamId, setView],
  );

  const openMember = useCallback(
    (row: TeamMemberRow) => openMemberSession(row.instance_id, row.session_id),
    [openMemberSession],
  );

  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as {
        sessionId?: string;
        instanceId?: string;
        teamId?: string;
      };
      if (detail?.sessionId && detail?.instanceId) {
        openMemberSession(detail.instanceId, detail.sessionId, detail.teamId);
      }
    };
    window.addEventListener("ginno:open-team-member", handler);
    return () => window.removeEventListener("ginno:open-team-member", handler);
  }, [openMemberSession]);

  // P2-25：在 /team 之外点「进入工作台」——TeamReplyCard 先存一次性意图
  // 再导航过来；挂载时消费（sessionStorage，读后即清，不会误重放）。
  useEffect(() => {
    const intent = consumeOpenMemberIntent();
    if (!intent) return;
    if (intent.teamId) setTeamId(intent.teamId);
    setView({ kind: "member", instanceId: intent.instanceId, sessionId: intent.sessionId });
    if (intent.teamId) {
      void api
        .markTeamMemberSeen(intent.teamId, intent.instanceId)
        .then(() => refresh())
        .catch(() => {});
    }
    // 只在挂载时消费一次；refresh 变化不得导致意图重放。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { openMemberSession, openMember };
}
