import { useCallback, useEffect, useMemo, useState } from "react";
import type { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import type { Team } from "@/lib/types";

export function useTeamList(tc: ReturnType<typeof useTranslations<"chat">>, setNotice: (message: string) => void) {
  const [teams, setTeams] = useState<Team[]>([]);
  const [teamId, setTeamId] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [offline, setOffline] = useState(false);

  const team = useMemo(
    () => teams.find((t) => t.id === teamId) ?? null,
    [teams, teamId],
  );

  // ---- 拉团队列表 ----
  const refresh = useCallback(async (): Promise<Team[]> => {
    try {
      const r = await api.listTeams();
      const list = r.teams ?? [];
      setTeams(list);
      setOffline(false);
      // 默认选第一个未归档团队（或保持当前选择）。
      setTeamId((cur) =>
        cur && list.some((t) => t.id === cur) ? cur : list.find((t) => !t.archived)?.id ?? list[0]?.id ?? null,
      );
      return list;
    } catch {
      // P2-18：sidecar 未起——保留上次数据，但不再静默：离线横幅 +
      // 错误提示告诉用户「这是缓存」，可手动重试。
      setOffline(true);
      setNotice(tc("team.refreshFailed"));
      return [];
    } finally {
      setLoaded(true);
    }
  }, [tc, setNotice]);

  return { teams, teamId, setTeamId, team, loaded, offline, refresh, setTeams, setOffline };
}

// Scope synchronization to the list and keep its effects after the team view preferences.
export function useTeamListSync({ team, refresh, setTeams, setOffline }: ReturnType<typeof useTeamList>) {
  useEffect(() => {
    void refresh();
  }, [refresh]);

  // ---- 订阅主会话 WS 的 team.changed（状态回流，§5.3）----
  // 只订阅当前团队主会话；事件里带完整 team payload，直接替换本地那份。
  // P1-11：断线自动重连（指数退避 1s→30s 封顶，照 useChatStreamEngine 的
  // onclose→setTimeout 重连范式）；重连成功后全量重拉团队数据——断线期间
  // 错过的 team.changed 无法补帧，只有重拉能对账。sidecar 重启不再永久 stale。
  const mainSid = team?.main_session_id;
  useEffect(() => {
    if (!mainSid) return;
    // 守卫后固化成 string：TS 不把外层 narrowing 带进下面嵌套的 connect()
    // 闭包，直接用 mainSid 会报 string|undefined（真实 tsc 抓到）。
    const sid: string = mainSid;
    let ws: WebSocket | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let disposed = false;
    let attempt = 0;
    let dropped = false; // 是否断过线（重连成功才需要全量重拉）

    function scheduleReconnect() {
      if (disposed) return;
      const delay = Math.min(30_000, 1000 * 2 ** attempt);
      attempt += 1;
      timer = setTimeout(connect, delay);
    }

    function connect() {
      if (disposed) return;
      try {
        ws = api.openSessionSocket(sid);
      } catch {
        scheduleReconnect(); // 预渲染等无 WebSocket 环境
        return;
      }
      ws.onopen = () => {
        attempt = 0;
        setOffline(false);
        if (dropped) void refresh();
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as {
            event?: string;
            team_id?: string;
            team?: Team;
          };
          if (msg.event === "team.changed" && msg.team && msg.team_id) {
            setTeams((prev) =>
              prev.map((t) => (t.id === msg.team_id ? (msg.team as Team) : t)),
            );
          }
        } catch {
          /* 非 JSON 帧 */
        }
      };
      ws.onerror = () => {
        try {
          ws?.close();
        } catch {
          /* noop */
        }
      };
      ws.onclose = () => {
        if (disposed) return;
        dropped = true;
        setOffline(true);
        scheduleReconnect();
      };
    }

    connect();
    return () => {
      disposed = true;
      if (timer) clearTimeout(timer);
      try {
        ws?.close();
      } catch {
        /* noop */
      }
    };
  }, [mainSid, refresh, setTeams, setOffline]);
}
