// 07-runtime-events — 会话状态机与 sidecar 运行时事件（dev-swap / 遗言）。
// 等价迁移自 handle() switch（批次 6-2），case 体逐字一致。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { dismissLastwords, resumeLastwords } from "@/lib/runtime";
import { notifyNative } from "@/lib/desktop";
import { pushNotice } from "@/lib/notice";
import { pushDevSwapRun } from "@/lib/devswap/bus";
import { normalizeDevSwapRun } from "@/lib/devswap/types";
import { markDelivered } from "../liveBubble";

export function onSessionStateChanged(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  const stSid = String(ev.session_id ?? "");
  if (!stSid) return;
  if (ev.state === "compacting") {
    ctx.compactStageRef.current[stSid] ??= "summarizing";
  } else if (ev.state === "idle") {
    ctx.compactStageRef.current[stSid] = null;
  }
  ctx.syncDisplay(stSid);
}

export function onDevswapProgress(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // dev-swap 热切换进度（docs/dev-swap-progress-design.md §2.2）：sidecar
  // 广播的阶段事件推入模块级总线，DevSwapCard / session 状态行订阅呈现。
  // 不 markDelivered——这不是本会话 turn 的帧，只是路过本 socket。
  const run = normalizeDevSwapRun(ev);
  if (run) pushDevSwapRun(run);
}

export function onLastwordsPending(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 遗言恢复确认（2026-11-16 交互补齐）：旧实现只投一条通知、点击仅跳
  // 会话，靠注入带里的「请从中断处继续」让 agent 自己动——用户没有按钮
  // 可点（「遗言好像没触发过」的直接原因之一）。现在通知卡片即决策面：
  // 「恢复」真调 /api/lastwords/resume，「忽略」调同端点 dismiss:true；
  // 常驻（duration:0）等用户决策，绝不 6s 自动消失。点卡片本体仍跳会话
  // （actionMeta 保留）。后端契约：人工会话 + 任意 reason + notify/auto
  // 都发本事件；payload 带 head（请求摘要）与 bg_interrupted（被重启杀掉
  // 的后台任务数）——文案必须呈现，用户才知道要恢复的是什么。
  const lpSid = String(ev.session_id ?? "");
  if (!lpSid) return;
  {
    const { tr } = ctx.i18nRef.current;
    const tools = Number(ev.tool_calls ?? 0);
    const secs = Math.max(0, Math.round(Number(ev.elapsed_s ?? 0)));
    const mm = `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, "0")}`;
    // head / user_request_head 双键兼容（后端两个都发）；摘要缺失回落
    // 占位文案，不让卡片出现空引号。用 || 而非 ??：head 为空串时也取备用键。
    const head =
      String(ev.head || ev.user_request_head || "").trim() ||
      tr("notice.lastwords-no-head");
    const bgInterrupted = Number(ev.bg_interrupted ?? 0);
    const text = tr("notice.lastwords-pending", { head, tools, elapsed: mm, bgInterrupted });
    const pendingKey = `lastwords-pending:${lpSid}`;
    const jumpMeta = { kind: "session" as const, id: lpSid };
    // 反馈复用 pending dedupeKey：收件箱条目原位刷新成结果文案，不残留
    // 过期的「要恢复吗」；失败卡带重试按钮（错误不吞、可再点）。
    const doResume = () => {
      void (async () => {
        try {
          const r = await resumeLastwords(lpSid);
          if (r?.ok) {
            pushNotice("success", tr("notice.lastwords-resume-ok"), {
              source: "general",
              kind: "lastwords",
              dedupeKey: pendingKey,
              actionMeta: jumpMeta,
            });
          } else if (r?.error === "no_pending") {
            // 条目已过期/已处置（TTL 或别处已确认）——明确告知，不重试。
            pushNotice("warn", tr("notice.lastwords-resume-expired"), {
              source: "general",
              kind: "lastwords",
              dedupeKey: pendingKey,
              actionMeta: jumpMeta,
            });
          } else {
            pushNotice(
              "error",
              tr("notice.lastwords-resume-failed", { error: String(r?.error ?? "unknown") }),
              {
                source: "general",
                kind: "lastwords",
                duration: 0,
                dedupeKey: pendingKey,
                actionMeta: jumpMeta,
                action: { label: tr("notice.lastwords-resume-btn"), onClick: doResume },
              },
            );
          }
        } catch (e) {
          pushNotice(
            "error",
            tr("notice.lastwords-resume-failed", {
              error: e instanceof Error ? e.message : String(e),
            }),
            {
              source: "general",
              kind: "lastwords",
              duration: 0,
              dedupeKey: pendingKey,
              actionMeta: jumpMeta,
              action: { label: tr("notice.lastwords-resume-btn"), onClick: doResume },
            },
          );
        }
      })();
    };
    const doDismiss = () => {
      void (async () => {
        try {
          await dismissLastwords(lpSid);
          pushNotice("info", tr("notice.lastwords-dismissed"), {
            source: "general",
            kind: "lastwords",
            dedupeKey: pendingKey,
            actionMeta: jumpMeta,
          });
        } catch (e) {
          pushNotice(
            "error",
            tr("notice.lastwords-dismiss-failed", {
              error: e instanceof Error ? e.message : String(e),
            }),
            {
              source: "general",
              kind: "lastwords",
              duration: 0,
              dedupeKey: pendingKey,
              actionMeta: jumpMeta,
              action: { label: tr("notice.lastwords-dismiss-btn"), onClick: doDismiss },
            },
          );
        }
      })();
    };
    pushNotice("info", text, {
      source: "general",
      kind: "lastwords",
      countInBadge: true,
      dedupeKey: pendingKey,
      duration: 0,
      actionMeta: jumpMeta,
      action: { label: tr("notice.lastwords-resume-btn"), onClick: doResume },
      secondaryAction: { label: tr("notice.lastwords-dismiss-btn"), onClick: doDismiss },
    });
    // 同步发 OS 通知。待人确认的恢复属于高优先级：不走 reply 的合并窗与
    // 60s 冷却门（那是给「完成」类刷屏用的），直接即时弹。
    // P1-5：但仍必须过偏好闸门——修复前这一路连总开关都不看，用户关掉
    // 「启用桌面提醒」它照样弹。归类 askUser（等人确认），闸门由
    // notifyNative 出口统一施加。
    void notifyNative({
      kind: "session",
      notifyKind: "askUser",
      id: lpSid,
      title: `⚡ ${tr("notice.lastwords-pending-title")}`,
      body: text,
    });
  }
}

export function onLastwordsRestored(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 遗言恢复回执（docs/lastwords-design.md P2 通知中心待恢复列表）：
  // 新 sidecar 启动扫描后广播——投一条收件箱条目（kind:"lastwords"，
  // countInBadge:true，dedupeKey 按会话去重），action 跳回原会话。
  // 不 markDelivered——非本会话 turn 的帧。
  const lwSid = String(ev.session_id ?? "");
  if (!lwSid) return;
  {
    const { tr } = ctx.i18nRef.current;
    pushNotice("info", tr("notice.lastwordsRestored"), {
      source: "general",
      kind: "lastwords",
      countInBadge: true,
      dedupeKey: `lastwords:${lwSid}`,
      actionMeta: { kind: "session", id: lwSid },
    });
  }
}

export function onDevSwapPending(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 开发热切换（docs/runtime-hot-swap-design.md §3.4）：CLI 已把 symlink
  // 切到新 bundle。2026-11-05 用户需求：切换需用户确认——不再自动 invoke
  // finish_dev_swap，改为点亮确认条（DevSwapCard 确认区，TopBar 常驻可见）。
  // 「暂不」= 不重启，老 sidecar 继续跑，下次重启自然吃到新 bundle。
  // G1（2026-11-11）：auto=true = 企微侧已 y 确认——跳过弹框直接 invoke
  // （L2 低延迟路径；壳 watcher L3/L4 兜 webview 不在的情况）。
  markDelivered(ctx, sid);
  void (async () => {
    const { setDevSwapPendingConfirm } = await import("@/lib/devswap/bus");
    if (ev.auto === true) {
      setDevSwapPendingConfirm(null);
      try {
        const { invoke } = await import("@tauri-apps/api/core");
        await invoke("finish_dev_swap");
      } catch {
        /* invoke 失败（非桌面/webview 不可用）→ 壳 watcher 兜底 */
      }
      return;
    }
    setDevSwapPendingConfirm("pending");
  })();
}
