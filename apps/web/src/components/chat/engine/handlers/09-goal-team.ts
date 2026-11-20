// 09-goal-team — goal/plan/team 聚合状态与回放会话元数据。等价迁移自
// handle() switch（批次 6-2），case 体逐字一致（goal.archived 原 fall
// through 到 goal.cleared，还原为两键注册同一 handler）。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { notifyNative } from "@/lib/desktop";
import { pushNotice } from "@/lib/notice";
import { shouldNotify } from "@/lib/notifyHub";
import type { Goal, Plan } from "@/lib/types";
import { isWatching } from "../notifyOut";

// AgentTeam blocked 跃迁去重（P1-18）：记录当前处于 blocked 的成员实例，
// 只在「非 blocked → blocked」时通知一次；离开 blocked 即移除，下次再卡可再弹。
// （拆分前是 handle.ts 模块级单例，这里同生命周期。）
const _teamBlockedSeen = new Set<string>();

export function onGoalUpdated(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // Live goal snapshot (created/status/accounting) → TopBar chip.
  ctx.g.notifyGoal(sid, (ev.goal as Goal) ?? null);
}

export function onTeamChanged(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // AgentTeam 聚合状态回流：@ 投递菜单可见范围/总览卡都吃 store.teams。
  if (ev.team) ctx.g.notifyTeamUpdated(ev.team as never);
  // P1-18：成员**转入** blocked → 通知（设计里最有价值的一态：它卡住了，
  // 需要用户去代答）。只在状态跃迁时弹，不是每次 team.changed 都弹。
  {
    const t = ev.team as { id?: string; member_rows?: Array<{ instance_id?: string; display_name?: string; status?: string }> } | undefined;
    const rows = t?.member_rows ?? [];
    for (const r of rows) {
      const iid = String(r.instance_id || "");
      if (!iid) continue;
      if (r.status === "blocked") {
        if (!_teamBlockedSeen.has(iid)) {
          _teamBlockedSeen.add(iid);
          const body = ctx.i18nRef.current.tr("team.notifyBlocked", {
            name: r.display_name || iid,
          });
          pushNotice("warn", body, { dedupeKey: `team-blocked-${iid}` });
          if (shouldNotify("team", isWatching(ctx, sid))) {
            const title = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
            // 新契约（main 7c8fe8a 闸门收口）：notifyNative 是唯一出口，
            // 内部施加 osGateOpen(notifyKind) → applyPrivacy → soundFor。
            // 调用点只给原文 body + notifyKind，不再自己裁隐私/推提示音。
            // 外层 shouldNotify 保留——main 注释明写这是有意的双保险。
            void notifyNative({
              kind: "session",
              notifyKind: "team",
              id: sid,
              title: `⛔ ${title}`,
              body,
            });
          }
        }
      } else {
        _teamBlockedSeen.delete(iid);
      }
    }
  }
}

export function onPlanUpdated(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 任务清单实时刷（plan-orchestration §2）：watcher 每次落盘推整张
  // 清单快照；store 按 plan_id upsert，PlanBoard 重渲染。
  if (ev.plan) ctx.g.notifyPlanUpdated(ev.plan as Plan);
}

/** goal.archived 与 goal.cleared 共用（原 goal.archived case 无本体直接
 *  fall through 到 goal.cleared）。 */
export function onGoalCleared(ctx: EngineCtx, sid: string) {
  // Lifecycle finish (2026-11-16): complete goal auto-archived at turn
  // end — record moved to goals-history.jsonl, chip clears like cleared.
  ctx.g.notifyGoal(sid, null);
}

export function onSessionTitle(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // Auto-title from the first user message (runtime _touch_session_title):
  // sidebar + TopBar rename live without a sessions reload.
  ctx.g.applySessionPatch(sid, { title: (ev.title as string) ?? "", title_auto: false });
}

export function onSessionContext(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // Mount set changed server-side (context-folders-design.md): /mount
  // command or another client ran PUT /sessions/{id}/context. Patch the
  // store so the TopBar chip re-renders without a full reload.
  ctx.g.applySessionPatch(sid, {
    context_folders: ((ev.context_folders as string[]) ?? []),
    primary_folder: (ev.primary_folder as string | null) ?? null,
  });
}
