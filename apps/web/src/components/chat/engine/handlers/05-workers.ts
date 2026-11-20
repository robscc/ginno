// 05-workers — worker_bus 等待可见化三态与订阅会话完成。等价迁移自 handle()
// switch（批次 6-2），case 体逐字一致（worker 三态共享体还原为单函数）。

import type { EngineCtx } from "../ctx";
import type { ServerEvent } from "./types";
import { pushNotice } from "@/lib/notice";
import { shouldNotify } from "@/lib/notifyHub";
import { isWatching, replyMerge } from "../notifyOut";

/** worker.waiting / worker.progress / worker.resumed 共享体（原三个相邻 case
 *  fall through 到同一体，靠 ev.event 判别文案）。 */
export function onWorkerNotice(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 等待可见化三态（2026-11-06「assign 后 5 分钟像卡死」；2026-11-08
  // 语义化）：waiting = 订阅建立（等待起点）；progress = digest 进度
  // 节选到达（**不是完成**）；resumed = 终态结果注入。文案全部带目标
  // 标题，用户一眼知道是哪个会话、发生了什么。
  {
    const title =
      (typeof ev.target_title === "string" && ev.target_title.trim()) ||
      ctx.g.sessions.find((x) => x.id === ev.target)?.title?.trim() ||
      "…";
    if (ev.event === "worker.waiting") {
      pushNotice(
        "info",
        ctx.i18nRef.current.tr("worker.waitingNotice", { title }),
        { dedupeKey: `${ev.event}-${ev.target}` },
      );
    } else if (ev.event === "worker.progress") {
      pushNotice(
        "info",
        ctx.i18nRef.current.tr("worker.progressNotice", { title }),
        { dedupeKey: `${ev.event}-${ev.target}` },
      );
    } else {
      pushNotice(
        "info",
        ctx.i18nRef.current.tr("worker.resumedNotice", { title }),
        { dedupeKey: `${ev.event}-${ev.target}` },
      );
    }
  }
}

export function onLinkedDone(ctx: EngineCtx, sid: string, ev: ServerEvent) {
  // 订阅的会话 turn 完成（2026-11-06 闭环）：📡 前缀 OS 通知 + 点击直达
  // 目标会话。origin=schedule/subagent 的完成已有专属通道，服务端已过滤
  // （engine.py:1303 只放行 interactive/deliver），此处不再判。
  //
  // P1-3：改走与 message.end **同一个** replyMerge 窗口。修复前这一路单独
  // 用 60s 全局冷却、message.end 单独用 30s 按会话合并，两套机制互不相干
  // ——多会话并发完成时真正需要的全局上限偏偏不在主路径上。现在冷却统一
  // 在 flush 时施加，两路共享同一个计数（+N）。
  const targetSid = typeof ev.session_id === "string" ? ev.session_id : "";
  if (!targetSid) return;
  if (!shouldNotify("reply", isWatching(ctx, sid))) return;
  const title = ctx.g.sessions.find((x) => x.id === targetSid)?.title?.trim() || "…";
  const raw = typeof ev.text === "string" ? ev.text.trim() : "";
  // worker 语义修正：worker_status=working/waiting 说明它只是完成了一段
  // （还在干活/等下一段），文案不再说「回复完成」。
  const wst = typeof ev.worker_status === "string" ? ev.worker_status : "";
  const body =
    wst === "working"
      ? ctx.i18nRef.current.tr("notify.linkedTurnOngoing")
      : wst === "waiting"
        ? ctx.i18nRef.current.tr("notify.linkedTurnWaiting")
        : raw || ctx.i18nRef.current.tr("notify.replyCompleted");
  replyMerge.push(targetSid, {
    title: `📡 ${title}`,
    body,
    trN: (n) => ctx.i18nRef.current.tr("notify.nCompleted", { n }),
    // 保持既有语义：正盯着**自己**的会话时不弹（📡 chip 徽标已可见）。
    // 判的是本会话 sid，不是 targetSid——flush 时复检，30s 内切走/切回
    // 都以当下为准。
    attentive: () => isWatching(ctx, sid),
  });
}
