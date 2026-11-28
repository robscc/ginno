import { useEffect, useRef, useState } from "react";
import type { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import type { useGinno } from "@/lib/store";
import { notifyScheduleRunFinished } from "@/lib/store";
import { pushNotice } from "@/lib/notice";
import { SESSION_ACTIVITY_CHANGED } from "@/lib/sessionActivity";
import { openSocket, wsConnectorsUrl, wsScheduleUrl } from "@/lib/runtime";
import * as api from "@/lib/runtime";
import { setKeepAwake } from "@/lib/desktop";
import type { ScheduleRun } from "@/lib/types";

type ShellActivityStore = Pick<ReturnType<typeof useGinno>,
  "setActiveSession" | "setRightTab" | "setRightPanelOpen" | "openScheduleRun" | "closeScheduleRun" | "ready"
>;

export function useShellActivity(
  g: ShellActivityStore,
  router: ReturnType<typeof useRouter>,
  pathname: string,
) {
  const t = useTranslations("shell");

  // Tauri shell bridges: clicking a native notification fires one of these via
  // webview.eval (apps/desktop/src/lib.rs) — same convention as ChatStream's
  // __ginnoFileDrop. AppShell stays mounted for the app's lifetime, including
  // while the window is hidden, so the globals are always registered.
  // Stable setters destructured out of `g` so the effect doesn't re-arm on
  // every provider render; the route is read live inside the handlers.
  const setActiveSession = g.setActiveSession;
  const setRightTab = g.setRightTab;
  const setRightPanelOpenForBridge = g.setRightPanelOpen;
  // 定时任务完成通知的点击回跳数据：run_finished 事件携带的行与用户点击通知
  // 之间可能相隔数分钟，按 run_id 缓存最近 20 条供回跳查；webview 重载后缓存
  // 为空 → 退回 /scheduled 执行记录页（Notification Center 里的旧通知仍可用）。
  const scheduleNotifyRowsRef = useRef<Map<string, ScheduleRun>>(new Map());
  const openScheduleRunFromNotifyRef = useRef<(runId: string) => void>(() => {});
  openScheduleRunFromNotifyRef.current = (runId: string) => {
    const row = scheduleNotifyRowsRef.current.get(runId);
    if (row && row.target_type === "prompt" && row.session_id) {
      // 与 RunsPanel.openRun 同语义：点开 = 已读（后端广播 runs_seen，状态点
      // 即时消除），并挂 ScheduleRunView 影子会话回放（不离开当前路由）。
      void api.markScheduleRunsSeen({ run_id: runId }).catch(() => {});
      g.openScheduleRun(row);
      return;
    }
    // 找不到行（重载后旧通知）或 workflow 目标：落到执行记录列表
    if (window.location.pathname !== "/scheduled") router.push("/scheduled");
  };
  useEffect(() => {
    const openSession = (sid: string) => {
      if (!sid) return;
      setActiveSession(sid);
      if (window.location.pathname !== "/") router.push("/");
      // ChatStream arms stick-to-bottom and scrolls on this event (the
      // session's history may still be loading — its switch effect and the
      // [messages] auto-scroll finish the job).
      window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: sid }));
    };
    const openWorkflowRun = () => {
      // The right panel only renders on the workspace route.
      if (window.location.pathname !== "/") router.push("/");
      setRightTab("workflow"); // manual open also clears the panel badge
      setRightPanelOpenForBridge(true);
    };
    // 「运行中任务」面板（tasks-overview §2.2 入口直达）：menubar ▶N 下拉的
    // 「查看全部」项（Rust tray 菜单）与侧栏品牌行 ▶N 徽标都落到这里——
    // 与 openWorkflowRun 同款三步：回工作区 + 切 tab + 展开面板。
    const openTasksOverview = () => {
      if (window.location.pathname !== "/") router.push("/");
      setRightTab("running");
      setRightPanelOpenForBridge(true);
    };
    // 后台任务完成通知的点击落点（background-tasks-design §6.3）：任务中心
    // 路由页——任务没有 per-task 路由，id 本身不可定位。
    const openTasks = () => {
      if (window.location.pathname !== "/tasks") router.push("/tasks");
    };
    (window as unknown as { __ginnoOpenSession?: (sid: string) => void }).__ginnoOpenSession =
      openSession;
    (window as unknown as { __ginnoOpenWorkflowRun?: () => void }).__ginnoOpenWorkflowRun =
      openWorkflowRun;
    (window as unknown as { __ginnoOpenTasksOverview?: () => void }).__ginnoOpenTasksOverview =
      openTasksOverview;
    (window as unknown as { __ginnoOpenScheduleRun?: (runId: string) => void })
      .__ginnoOpenScheduleRun = (runId: string) => openScheduleRunFromNotifyRef.current(runId);
    (window as unknown as { __ginnoOpenTasks?: () => void }).__ginnoOpenTasks = openTasks;
    return () => {
      delete (window as unknown as { __ginnoOpenSession?: unknown }).__ginnoOpenSession;
      delete (window as unknown as { __ginnoOpenWorkflowRun?: unknown }).__ginnoOpenWorkflowRun;
      delete (window as unknown as { __ginnoOpenTasksOverview?: unknown }).__ginnoOpenTasksOverview;
      delete (window as unknown as { __ginnoOpenScheduleRun?: unknown }).__ginnoOpenScheduleRun;
      delete (window as unknown as { __ginnoOpenTasks?: unknown }).__ginnoOpenTasks;
    };
  }, [setActiveSession, setRightTab, setRightPanelOpenForBridge, router]);

  // Connector 聚合状态点(connector-module §2.1):有断连→黄,有错误→红,
  // 全部正常/未启用→不显示。10s 轮询即可——dot 只是个入口提示。
  const [connectorDot, setConnectorDot] = useState("");
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const r = await import("@/lib/runtime").then((m) => m.listConnectors());
        if (alive) setConnectorDot(r.aggregateDot || "");
      } catch { /* sidecar 未起时静默 */ }
    };
    tick();
    const iv = setInterval(tick, 10000);
    return () => {
      alive = false;
      clearInterval(iv);
    };
  }, []);

  // Read all session activities together. WebSocket changes refresh this snapshot;
  // polling also picks up work in sessions without an open socket.
  const [runningIds, setRunningIds] = useState<ReadonlySet<string>>(new Set());
  const [compactingIds, setCompactingIds] = useState<ReadonlySet<string>>(new Set());
  const [waitingMap, setWaitingMap] = useState<Record<string, string>>({});
  useEffect(() => {
    let alive = true;
    let inFlight = false;
    let refreshQueued = false;
    const updateSet = (prev: ReadonlySet<string>, ids: string[]) => {
      const next = new Set(ids);
      return prev.size === next.size && ids.every(id => prev.has(id)) ? prev : next;
    };
    const refresh = async () => {
      inFlight = true;
      try {
        const r = await api.listRunning();
        // A frame that arrived during the read invalidates this response.
        if (!alive || refreshQueued) return;
        setRunningIds(prev => updateSet(prev, (r.sessions ?? []).map(x => x.id)));
        setCompactingIds(prev => updateSet(prev, r.compacting ?? []));
        const waiting = Object.fromEntries((r.waiting ?? []).map(w => [w.id, w.kind ?? "unknown"]));
        setWaitingMap(prev => JSON.stringify(prev) === JSON.stringify(waiting) ? prev : waiting);
      } catch { /* sidecar 未起时静默 */ }
      finally {
        inFlight = false;
        if (alive && refreshQueued) {
          refreshQueued = false;
          void refresh();
        }
      }
    };
    const invalidate = () => {
      if (inFlight) refreshQueued = true;
      else void refresh();
    };
    window.addEventListener(SESSION_ACTIVITY_CHANGED, invalidate);
    void refresh();
    const iv = setInterval(() => { if (!inFlight) void refresh(); }, 5000);
    return () => {
      alive = false;
      window.removeEventListener(SESSION_ACTIVITY_CHANGED, invalidate);
      clearInterval(iv);
    };
  }, []);

  // 定时回放不跟随路由(ScheduleRunView 挂在 main 顶层)——切到 Connectors/KB
  // 等其他页面时自动收起,否则回放挡住新页面、看起来像「跳转失灵」。打开回放
  // 本身不换路由,此 effect 不会误伤;workflow 分流的 push("/") 在 openRunView
  // 里已互斥清掉回放。ref 防止 store 重建导致的意外收起。
  const closeScheduleRunRef = useRef(g.closeScheduleRun);
  closeScheduleRunRef.current = g.closeScheduleRun;
  useEffect(() => {
    closeScheduleRunRef.current();
  }, [pathname]);

  // 连接器事件通道(#6「提示一次」):B 轨 fallback 首次发生时推一条 notice,
  // 用户知情后可在 连接器 设置里改 off/auto。WS 断了静默——提示不是关键路径。
  // (notice-unification §4:私有 state + fixed 层已收编进 pushNotice 右下容器)
  useEffect(() => {
    let ws: WebSocket | null = null;
    try {
      ws = openSocket(wsConnectorsUrl());
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { type: string; hint?: string };
          if (msg.type === "browser_fallback_used" && msg.hint) {
            // 8s 自动消失;dedupeKey 防多帧重弹;一次性提示(sessionStorage 防重复打扰)
            pushNotice("info", `${t("fallbackToast.title")}\n${msg.hint}`, {
              duration: 8000,
              dedupeKey: "browser-fallback",
              source: "connector",
            });
            if (!sessionStorage.getItem("ginno-fb-toast")) {
              sessionStorage.setItem("ginno-fb-toast", "1");
            }
          }
        } catch { /* 非 JSON 帧 */ }
      };
      ws.onclose = () => { /* 提示非关键路径,不重连 */ };
    } catch { /* WebSocket 不可用(静态导出预渲染) */ }
    return () => {
      try { ws?.close(); } catch { /* already closed */ }
    };
    // Keep a single connector event channel for the shell lifetime.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 定时任务状态点（scheduled-tasks-design §3.1/§6 + schedule-unseen）：红 =
  // 存在未查看的执行结果（实心红圆 + 白色数字徽章，点开记录/全部已读后
  // 消除）；灰 = 全局关闭；无点 = 无未读。冷数据 GET 兜底（60s），运行中由
  // /api/ws/schedule 的 run_finished/missed/task_updated/runs_seen 事件即时
  // 刷新；挂这一条轻量 WS 只为状态点，页面级订阅归 /scheduled 自己（§6）。
  const [scheduleDot, setScheduleDot] = useState<"" | "off" | "error">("");
  // 定时任务执行中数（2026-11-19 侧栏彩虹点）：与 unseen 同一份 list_schedule
  // 载荷顺带（runtime unseen_summary），60s 冷轮询 + ws 事件 tick 刷新。
  const [scheduleRunning, setScheduleRunning] = useState(0);
  const [scheduleUnseen, setScheduleUnseen] = useState<{ total: number; error: number; missed: number }>({
    total: 0,
    error: 0,
    missed: 0,
  });
  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const cfg = await api.listSchedule();
        if (!alive) return;
        // 空闲治理（2026-11-15）：60s 轮询数值不变保引用 bailout。
        const u = cfg.unseen ?? { total: 0, error: 0, missed: 0 };
        setScheduleUnseen((prev) =>
          prev.total === u.total && prev.error === u.error && prev.missed === u.missed
            ? prev
            : u,
        );
        if (!cfg?.enabled) {
          setScheduleDot("off");
          return;
        }
        setScheduleDot((cfg.unseen?.total ?? 0) > 0 ? "error" : "");
        setScheduleRunning(cfg.unseen?.running ?? 0);
      } catch {
        /* sidecar 未起时静默——保持上次状态 */
      }
    };
    tick();
    const iv = setInterval(tick, 60000);
    let ws: WebSocket | null = null;
    try {
      ws = openSocket(wsScheduleUrl());
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data) as { type?: string; run?: ScheduleRun };
          if (msg.type === "run_finished" && msg.run) {
            // 点击回跳数据先落缓存（通知点击可能晚于事件数分钟），再发通知
            // ——闸门/排除项（workflow 目标、skipped_overlap、窗口可见）在
            // notifyScheduleRunFinished 内统一判定。
            scheduleNotifyRowsRef.current.set(msg.run.run_id, msg.run);
            if (scheduleNotifyRowsRef.current.size > 20) {
              const oldest = scheduleNotifyRowsRef.current.keys().next().value;
              if (oldest !== undefined) scheduleNotifyRowsRef.current.delete(oldest);
            }
            notifyScheduleRunFinished(msg.run, (runId) =>
              openScheduleRunFromNotifyRef.current(runId),
            );
          }
          if (
            msg.type === "run_finished" ||
            msg.type === "missed" ||
            msg.type === "task_updated" ||
            msg.type === "runs_seen"
          ) {
            void tick();
          }
        } catch {
          /* 非 JSON 帧 */
        }
      };
      ws.onclose = () => {
        /* 状态点非关键路径，不重连（60s 轮询兜底） */
      };
    } catch {
      /* WebSocket 不可用（静态导出预渲染） */
    }
    return () => {
      alive = false;
      clearInterval(iv);
      try {
        ws?.close();
      } catch {
        /* already closed */
      }
    };
  }, []);

  // keep_awake 状态同步（scheduled-tasks-design §3.3）：runtime 的
  // schedules.json 是唯一真值；壳重启而 runtime 未动时由 web 启动加载后向壳
  // 同步一次。纯 web/dev 无 Tauri 时 setKeepAwake 直接返回 false，优雅降级。
  useEffect(() => {
    if (!g.ready) return;
    api
      .listSchedule()
      .then((cfg) => {
        // 无条件断言到真值（command 幂等）：false 时顺带清掉壳内可能残留的
        // caffeinate 子进程。
        if (cfg) void setKeepAwake(!!cfg.keep_awake);
      })
      .catch(() => {
        /* sidecar 未起 */
      });
  }, [g.ready]);

  return { connectorDot, runningIds, compactingIds, waitingMap, scheduleDot, scheduleRunning, scheduleUnseen };
}
