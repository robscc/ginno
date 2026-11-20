// engine/notifyOut — OS 通知出口辅助（isWatching / ask_user 去重 / reply 合并窗），
// 自 handle.ts 机械拆出（批次 6-2），逻辑逐字一致。模块级单例
// （notifiedQuestionIds / replyMerge）与拆分前同生命周期：模块加载即建，
// 全应用一份。
// 偏好闸门（总开关 ∧ 分类型 ∧ 勿扰 ∧ 隐私 ∧ 提示音）统一装在 desktop.ts 的
// notifyNative 出口里；本文件只负责两件事：判「用户是否正盯着」（isWatching）
// 与「同类通知怎么节流」（reply 走 30s 合并窗 + 60s 全局冷却，askUser/failure
// 即时弹不合并——等人/出事绝不节流）。

import { notifyNative } from "@/lib/desktop";
import { applyPrivacy, osGateOpen } from "@/lib/notifyPrefs";
import { ReplyMergeWindow, isSessionAttentive, replyRateGate, shouldNotify, type MergeItem } from "@/lib/notifyHub";
import type { EngineCtx } from "./ctx";

/** watching 三条件（应用可见 ∧ 工作区路由 ∧ 正盯该会话）——socket 回调闭包会
 *  过期，只读 ref/即时值。判据本体在 notifyHub.isSessionAttentive，与其余 6 个
 *  发通知的站点共用同一口径（P1-4）。 */
export function isWatching(ctx: EngineCtx, sid: string): boolean {
  return isSessionAttentive(ctx.activeSidRef.current, sid);
}

/** 已通知过的 ask_user 问题 id（tool_call id，全局唯一）——重连重放不二次弹。 */
// 已通知过的 ask_user 问题 id（去重防「每次重连/重载都弹一遍等你回答」）。
// 2026-11-10：落 sessionStorage——问题 id 存在 checkpointer 里跨重启稳定，
// 但应用重启/webview 重载会清掉内存 Set，挂起未答的问题就会再弹一次
// （用户实录：ec0c5453 的挂起问题反复打扰）。sessionStorage 生命周期
// = 应用本次运行，退出即清，不越界持久。
const _NQI_KEY = "ginno:notified-question-ids";
function _loadNotifiedQuestionIds(): Set<string> {
  try {
    const raw = sessionStorage.getItem(_NQI_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}
const notifiedQuestionIds = _loadNotifiedQuestionIds();
function _saveNotifiedQuestionIds() {
  try {
    sessionStorage.setItem(_NQI_KEY, JSON.stringify([...notifiedQuestionIds]));
  } catch {
    /* 私密模式等写失败静默 */
  }
}

/** 去重集上限：超出按插入序淘汰最旧（JS Set 保序）。
 *  2026-11-14 改：旧实现是 `size > 1000 → clear()` 整表清空——delta 洪水把集
 * 灌满时会连**真实挂起问题**的去重记录一起抹掉，让 b088cc0 修的「重连重复弹」
 * 复发。逐条淘汰只丢最旧的，正在挂起的问题（最新）永远留着。 */
const _NQI_MAX = 1000;
function _pruneNotifiedQuestionIds() {
  while (notifiedQuestionIds.size > _NQI_MAX) {
    const oldest = notifiedQuestionIds.values().next().value;
    if (oldest === undefined) break;
    notifiedQuestionIds.delete(oldest);
  }
}

/** ask_user 的去重键。
 *
 *  正常路径是 tool_call id：ask_tools.py:359 把 `InjectedToolCallId` 放进中断
 *  载荷（`"id": tool_call_id or ""`），ws.py:286 的重放路径也带着它——id 在
 *  checkpoint / live 事件 / replay 三处稳定，正是前端合并幂等的基础。
 *
 *  id 为空是退化情形（工具被非 tool-call 路径调用等）。此时**不能**直接静默
 *  （那会漏掉真挂起的问题），也**不能**退回旧实现的 `${sid}:${Date.now()}`
 *  （每帧一把新钥匙 = 通知风暴根因）。改用「会话 + 问题文本」派生的稳定键：
 *  同一问题重放时文本不变 → 仍然去重；不同问题 → 各自一条。
 *
 *  注意本函数只可能被 `user.question` handler 调到（见 handlers/01-stream.ts
 *  的共享体警告），delta/tool 帧已在结构上进不来，所以这里的兜底不会再被
 *  洪水灌满。 */
function _askDedupeKey(sid: string, ev: { [k: string]: unknown }): string {
  const rawId = typeof ev.id === "string" ? ev.id.trim() : "";
  if (rawId) return rawId;
  const subs = Array.isArray(ev.questions) ? (ev.questions as Array<{ question?: unknown }>) : [];
  const q = String(ev.question ?? "").trim() || String(subs[0]?.question ?? "").trim();
  return `q:${sid}:${q.slice(0, 120)}:${subs.length}`;
}

/** ask_user 挂起的 OS 通知（V2：最急的通知类，即时弹不合并——不看就永远卡着）。
 *
 *  **只允许 `user.question` handler 调用**：见 handlers/01-stream.ts 里 tool.*
 *  共享体的警告注释，与 docs/notifications-audit-2026-11-14.md P0-1。
 *
 *  去重（2026-11-05 真机回归）：WS 重连时服务端会把 parked 的挂起问题重发一遍
 *  （ws.py:244，给前端重建问题卡用，id 不变）——不按 id 去重的话每次重连/切会话
 *  都弹一遍「等你回答」（用户报：没有 ask_user 也弹）。
 *
 *  2026-11-14 加固（通知风暴根因）：去重键改走 `_askDedupeKey`——旧兜底
 *  `${sid}:${Date.now()}` 等于给每个 delta 帧发一把新钥匙，去重形同虚设。
 *  记 id 在闸门判定**之前**（与原实现同语义）：通知关着时也不会在开启后补弹。 */
export function maybeNotifyAskUser(
  ctx: EngineCtx,
  sid: string,
  ev: { event: string; [k: string]: unknown },
) {
  const qid = _askDedupeKey(sid, ev);
  if (notifiedQuestionIds.has(qid)) return;
  notifiedQuestionIds.add(qid);
  _pruneNotifiedQuestionIds();
  _saveNotifiedQuestionIds();
  if (!shouldNotify("askUser", isWatching(ctx, sid))) return;
  const sTitle = ctx.g.sessions.find((s) => s.id === sid)?.title?.trim() || "Ginno";
  void notifyNative({
    kind: "session",
    notifyKind: "askUser",
    id: sid,
    title: `⏸ ${sTitle}`,
    body: ctx.i18nRef.current.tr("notify.askPending"),
  });
}

/** reply 合并条目：MergeItem + 多条时的计数文案（push 侧闭包捕获，
 *  i18nRef 延迟求值——ctx 长存所以安全）。 */
interface ReplyItem extends MergeItem {
  trN: (n: number) => string;
  /** flush 时**重新**判定「用户是否已经在看」。push 到 flush 隔着 30s，期间
   *  用户完全可能已经切进这个会话——那时再弹就是纯打扰（P1-3）。
   *  闭包捕获 ctx，与 trN 同理。 */
  attentive: () => boolean;
}

/** reply 类通知的**统一**节流出口（P1-3）。
 *
 *  修复前是两套互不相干的机制：`message.end` 走 30s 按会话合并窗、`linked.done`
 *  走 60s 全局冷却——而多会话并发完成时真正需要的是全局上限，偏偏主路径
 *  （message.end）没有。2026-11-10 为治刷屏加的 KindRateGate 只接到了
 *  linked.done 一路。现在两路都 push 进这同一个窗口，flush 时统一过全局冷却。
 *
 *  flush 时才判注意力与冷却（而非 push 时）：30s 窗口内用户的注意力会变，
 *  用过期判定决定现在要不要打扰是错的。 */
export const replyMerge = new ReplyMergeWindow<ReplyItem>({
  onFlush: (sid, items) => {
    if (items.length === 0) return;
    // 用户已经切进这个会话了 → 不弹（他正在看）
    if (items[0].attentive()) return;
    // 60s 全局冷却：窗口内被吞掉的条数由下一次放行时带出（+N）
    const gate = replyRateGate.hit("reply");
    if (!gate.ok) return;
    const single = items.length === 1;
    const title = gate.suppressed > 0 ? `${items[0].title} (+${gate.suppressed})` : items[0].title;
    const body = single ? items[0].body : items[0].trN(items.length);
    const focusSession = () => {
      window.focus();
      // stable setter 语义见 desktop fallback —— 通过 CustomEvent 让
      // store 跳转（onFlush 里拿不到 ctx，走全局事件总线）。
      window.dispatchEvent(new CustomEvent("ginno:focus-latest", { detail: sid }));
    };
    void notifyNative({
      kind: "session",
      notifyKind: "reply",
      id: sid,
      title,
      body,
    }).then((sent) => {
      if (sent) return;
      // sent=false 现在也包含「被偏好闸门拦下」（notifyNative 是唯一出口）。
      // 浏览器兜底必须自己再判一次，否则用户关掉了桌面提醒、dev 浏览器里
      // 反而照样弹 HTML5 通知。
      if (!osGateOpen("reply")) return;
      if (typeof Notification === "undefined") return;
      if (Notification.permission === "default") {
        try {
          void Notification.requestPermission();
        } catch {
          /* unsupported */
        }
        return;
      }
      if (Notification.permission !== "granted") return;
      try {
        const n = new Notification(title, { body: applyPrivacy(body) });
        n.onclick = () => {
          focusSession();
          n.close();
        };
      } catch {
        /* blocked/unsupported */
      }
    });
  },
});

/** 丢弃某个会话待发的 reply 合并通知（用户已经切进去了，不必再弹）。
 *
 *  `ReplyMergeWindow.cancel` 的注释从 V2 起就写着 "e.g. user switched to the
 *  session"，但直到本次修复**全仓库零调用点**——API 造好了没接线。由
 *  useChatStreamEngine 在 activeSessionId 变更的 effect 里调用。 */
export function cancelPendingReplyNotice(sid: string | null | undefined): void {
  if (sid) replyMerge.cancel(sid);
}
