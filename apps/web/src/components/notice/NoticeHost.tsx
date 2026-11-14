"use client";

import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { CircleCheck, Info, Octagon, TriangleAlert, X } from "lucide-react";
import {
  dismissNotice,
  markNoticeRead,
  pauseNotice,
  resumeNotice,
  subscribeNotices,
  type Notice,
  type NoticeLevel,
} from "@/lib/notice";
import { notifyNative } from "@/lib/desktop";
import { isAppAttentive } from "@/lib/notifyHub";

/**
 * 全局通知中心宿主（notice-center-design.md §3/§4；右下角统一收编见
 * notice-unification-design.md §2.2/§4 P0）。
 *
 * 挂载点：AppShell 顶层的右下角统一容器（`fixed bottom-6 right-6 z-50`，
 * 与 DevSwapCard 持久卡同容器堆叠避让）——定位类由容器持有，本组件只是
 * 容器内的流式子元素，因此 /settings、/kb、/workflows 等全路由可见。
 *
 * 旧 mods 子系统的右下 toast（chat/mod/ModToastHost）已退役：`mod.toast`
 * 帧由 engine 直接 `pushNotice(source:"mod")` 进本总线（§4 收编表）。
 */

const LEVEL_META: Record<
  NoticeLevel,
  { icon: typeof Info; bar: string; fg: string; iconBg: string }
> = {
  info: { icon: Info, bar: "bg-blue", fg: "text-blue-400", iconBg: "bg-blue/15" },
  success: { icon: CircleCheck, bar: "bg-green", fg: "text-green-400", iconBg: "bg-green/15" },
  warn: { icon: TriangleAlert, bar: "bg-yellow", fg: "text-yellow-400", iconBg: "bg-yellow/15" },
  error: { icon: Octagon, bar: "bg-red", fg: "text-red-400", iconBg: "bg-red/15" },
};
export { LEVEL_META };

function NoticeCard({ n }: { n: Notice }) {
  const t = useTranslations("chat");
  const meta = LEVEL_META[n.level] ?? LEVEL_META.info;
  const Icon = meta.icon;
  return (
    <div
      role={n.level === "error" ? "alert" : "status"}
      aria-live={n.level === "error" ? "assertive" : "polite"}
      onMouseEnter={() => pauseNotice(n.id)}
      onMouseLeave={() => resumeNotice(n.id)}
      className={`pointer-events-auto flex w-[340px] gap-2.5 overflow-hidden rounded-xl border border-line bg-card2 py-2.5 pl-0 pr-2.5 text-xs leading-relaxed text-txt shadow-lg transition-all duration-[180ms] ease-out ${
        n.entered ? "translate-y-0 opacity-100" : "translate-y-1 opacity-0"
      }`}
    >
      {/* 左侧 2px 语义色条 */}
      <span className={`w-0.5 shrink-0 self-stretch rounded-r ${meta.bar}`} />
      <span className={`mt-px flex h-4 w-4 shrink-0 items-center justify-center rounded-full ${meta.iconBg}`}>
        <Icon className={`h-3 w-3 ${meta.fg}`} />
      </span>
      <div className="min-w-0 flex-1">
        <div className="selectable break-words whitespace-pre-wrap">{n.text}</div>
        {n.action && (
          <button
            onClick={() => {
              // 已读触发①（§3.2）：点 action = 已读 + 执行原 onClick。
              markNoticeRead(n.id, n.dedupeKey);
              n.action!.onClick();
            }}
            className="mt-1.5 rounded-md border border-line2 px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-violet/60 hover:text-txt"
          >
            {n.action.label}
          </button>
        )}
      </div>
      <button
        // 已读触发②（§3.2）：点 × 关闭 = 已读（与超时自动消失不同）。
        onClick={() => markNoticeRead(n.id, n.dedupeKey)}
        aria-label={t("notice.dismiss")}
        title={t("notice.dismiss")}
        className="mt-px shrink-0 rounded p-0.5 text-faint transition-colors hover:text-txt"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}

export function NoticeHost() {
  const [items, setItems] = useState<Notice[]>([]);

  useEffect(() => subscribeNotices(setItems), []);

  // Esc 关闭最上一条；输入框内不拦截（不抢用户的 Esc）。
  useEffect(() => {
    if (items.length === 0) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      const el = e.target as HTMLElement | null;
      if (el && (el.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName))) return;
      dismissNotice(items[items.length - 1].id);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items]);

  if (items.length === 0) return null;
  return (
    // pointer-events-none：不挡住底下的内容，每张卡片自己恢复
    // pointer-events-auto；定位（fixed 右下角）由 AppShell 的统一容器持有。
    <div className="pointer-events-none flex flex-col items-end gap-2">
      {items.map((n) => (
        <NoticeCard key={n.id} n={n} />
      ))}
    </div>
  );
}
/**
 * 失焦 OS 通知兜底（notice-unification-design.md §1 ③/§3 P1 行）：
 * 主窗隐藏/失焦期间到达的 toast 级通知转发 OS 原生通知（notifyNative，
 * 非桌面环境自动返回 false 跳过）。
 *
 * 降级范围（P1 决策）：notifyNative 的**点击目标** kind 枚举（session/
 * workflow-run/schedule-run/bg-task/test）不含泛化 notice 文案，强行映射会带
 * 错误跳转；因此只对 **error 级**兜底（点击目标 "test" = 仅 refocus 主窗）——
 * 出事类通知最不容错过且无合理跳转目标。其余级别 TODO(P2)：Rust 侧增加
 * "notice" 点击目标后放开（见设计文档 §2.3）。
 *
 * 2026-11-14（审计文档 P1-4 / P1-5）：
 *  · 点击目标与**通知分类**是两个正交轴。此前只有点击目标一个 `kind` 字段，
 *    这里被迫借用 `kind:"test"`，而 test 类刻意绕过偏好闸门（设置页测试按钮
 *    必须在任何偏好下都有反馈）——结果用户关掉「启用桌面提醒」后，error 级
 *    toast 照样弹系统通知。现在分类走 `notifyKind:"failure"`，正常受闸门约束。
 *  · 注意力判据统一到 isAppAttentive。此前只有这一处额外看了
 *    `document.hasFocus()`，其余 6 个站点只看 visibilityState——同一个「用户在
 *    看吗」在不同站点有不同答案。hasFocus 是否该纳入全局判据，待实机核实
 *    WKWebView 的遮挡语义后再单独收口（审计文档 §5 待核实 1）。
 */
export function useNoticeNativeFallback() {
  const lastIdRef = useRef(0);
  const primedRef = useRef(false);

  useEffect(
    () =>
      subscribeNotices((list) => {
        // 订阅即回放全量（含 AppShell 重挂时的在途卡片）：首轮只对齐水位
        // 不触发，避免把存量当「新到」重发系统通知。
        const fresh = list.filter((n) => n.id > lastIdRef.current);
        const maxId = list.reduce((m, n) => Math.max(m, n.id), lastIdRef.current);
        lastIdRef.current = maxId;
        if (!primedRef.current) {
          primedRef.current = true;
          return;
        }
        if (fresh.length === 0) return;
        if (isAppAttentive()) return;
        const err = fresh.find((n) => n.level === "error");
        if (!err) return; // TODO(P2): 非 error 级失焦兜底待 notifyNative 泛化
        void notifyNative({
          // 点击只能 refocus（无合理跳转目标），但分类是 failure——受总开关 /
          // kinds.failure / 勿扰 / 隐私约束，并按 failure 走 Basso 故障音。
          kind: "test",
          notifyKind: "failure",
          id: "",
          title: "Ginno",
          body: err.text,
        });
      }),
    [],
  );
}
