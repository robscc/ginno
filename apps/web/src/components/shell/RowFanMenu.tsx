"use client";

// RowFanMenu — 会话行的行尾动作扇形菜单（用户需求 2026-10-28）：置顶/改名/
// 删除原先三个按钮常驻行尾、挤占标题宽度，现在收进一个 hover 触发的 ⋯ 里，
// 点击后沿左上弧扇形展开（带 stagger 弹出动画）。置顶态本身由侧栏「置顶」
// 分组表达，不需要常驻 pin 图标。
//
// 2026-11-16：动作列表与右键菜单（SessionContextMenu）共享同一构造源
// （useRowActions）——两处菜单完全同步，新增动作只改一处。

import { MoreHorizontal } from "lucide-react";
import { useTranslations } from "next-intl";
import { SessionTagsPanel } from "./SessionTagsPanel";
import { useRowActions } from "./rowActions";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

/** 扇形几何：极坐标 → CSS 位移（y 向下为正）。角度从正右方逆时针量，
 * 90°=正上、180°=正左——展开成左上弧，不遮行下方内容。
 * 扇叶通过 portal 渲染到 body（position:fixed），不受侧栏滚动容器
 * overflow 裁剪——小角度扇叶可向右伸进对话区（2026-10-31 之前曾因
 * 被裁被迫压在 96-180 窄弧上，5 叶挤成一坨），所以各叶数都可用
 * ~37° 间距的宽弧，chord ≈ 2·52·sin(18.5°) ≈ 33px > 28px 叶宽不叠。
 * 7 叶（含「标为未读」）间距 28°，chord ≈ 2·52·sin(14°) ≈ 25px 会叠，
 * 半径提到 64：chord ≈ 31px ✓。 */
const RADIUS = 52;
const RADIUS_7 = 64;
const ANGLES_3 = [100, 140, 180];
const ANGLES_4 = [45, 90, 135, 180];
const ANGLES_5 = [30, 67, 104, 142, 180];
const ANGLES_6 = [12, 45, 78, 111, 145, 178];
const ANGLES_7 = [8, 36, 64, 92, 120, 149, 178];
const fanOffset = (deg: number, radius: number) => ({
  dx: Math.round(radius * Math.cos((deg * Math.PI) / 180)),
  dy: -Math.round(radius * Math.sin((deg * Math.PI) / 180)),
});

export function RowFanMenu({
  pinned,
  onPin,
  onRename,
  onDelete,
  worker,
  onWorker,
  forkSid,
  onFork,
  title,
  tagsIcon,
  tagsLabels,
  onTags,
  unread,
  onUnread,
}: {
  pinned: boolean;
  onPin: (pinned: boolean) => void;
  onRename: () => void;
  onDelete: () => void;
  /** Worker 协议「接活」开关（仅 main 会话行传入；缺省不渲染该扇叶）。 */
  worker?: boolean;
  onWorker?: (worker: boolean) => void;
  /** Fork：复制本会话为新会话（缺省不渲染该扇叶）。tooltip 带
   *  「fork from <sid 前 8 位>」——扇叶伸在行外，明示复制源是哪一行。 */
  forkSid?: string;
  onFork?: () => void;
  /** Session label/icon（session-label-icon-design.md §3.2）：传 onTags 才
   *  渲染「标签与图标」扇叶，点击打开 SessionTagsPanel（portal 弹层）。 */
  tagsIcon?: string | null;
  tagsLabels?: string[];
  onTags?: (icon: string | null, labels: string[]) => void;
  /** 手动未读标注（2026-11-16）：缺省不渲染该扇叶。 */
  unread?: boolean;
  onUnread?: (unread: boolean) => void;
  /** 面板头展示的会话标题（由行组件传入）。 */
  title: string;
}) {
  const t = useTranslations("shell");
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLSpanElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const leafHostRef = useRef<HTMLDivElement>(null);
  // 扇叶锚点：触发钮中心的视口坐标（扇叶 portal 到 body 用 fixed 定位，
  // 展开期间侧栏滚动/窗口缩放时重算，扇叶跟着行走）。
  const [anchor, setAnchor] = useState<{ x: number; y: number } | null>(null);
  const measure = () => {
    const r = triggerRef.current?.getBoundingClientRect();
    if (r) setAnchor({ x: r.left + r.width / 2, y: r.top + r.height / 2 });
  };

  // 标签与图标面板（SessionTagsPanel）：独立于扇叶展开态，锚点复用触发钮中心。
  const [tagsOpen, setTagsOpen] = useState(false);

  const items = useRowActions({
    pinned,
    onPin,
    onRename,
    onDelete,
    worker,
    onWorker,
    forkSid,
    onFork,
    onTags: onTags ? () => setTagsOpen(true) : undefined,
    unread,
    onUnread,
  });

  // 展开期间：点外部 / Esc 收起。行 mouseleave 不收——扇叶伸在行外，
  // 移向扇叶必然先离开行。扇叶在 portal 里，外部判定要算上 leafHost。
  useEffect(() => {
    if (!open) return;
    measure();
    const onDown = (e: PointerEvent) => {
      const target = e.target as Node;
      if (
        rootRef.current &&
        !rootRef.current.contains(target) &&
        !leafHostRef.current?.contains(target)
      )
        setOpen(false);
    };
    // capture=true：侧栏滚动容器（overflow-y-auto 会裁掉行内绝对定位的
    // 扇叶，这正是 portal 的原因）冒泡前拿到滚动事件重算锚点。
    window.addEventListener("scroll", measure, true);
    window.addEventListener("resize", measure);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("scroll", measure, true);
      window.removeEventListener("resize", measure);
    };
  }, [open]);

  const radius = items.length >= 7 ? RADIUS_7 : RADIUS;
  const angles =
    items.length >= 7
      ? ANGLES_7
      : items.length === 6
        ? ANGLES_6
        : items.length === 5
          ? ANGLES_5
          : items.length === 4
            ? ANGLES_4
            : ANGLES_3;

  return (
    <span ref={rootRef} className="relative inline-flex">
      <button
        ref={triggerRef}
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-label={t("session.rowActions")}
        aria-haspopup="menu"
        aria-expanded={open}
        className={`rounded p-1 transition-opacity hover:bg-card2 ${
          pinned ? "text-violet" : "text-muted hover:text-txt"
        } ${open ? "opacity-100" : "opacity-0 group-hover:opacity-100"}`}
      >
        <MoreHorizontal className="h-3.5 w-3.5" />
      </button>
      {createPortal(
        <div ref={leafHostRef}>
          {items.map((it, i) => {
            const { dx, dy } = fanOffset(angles[i], radius);
            return (
              <button
                key={it.key}
                role="menuitem"
                aria-label={it.label}
                title={it.hint}
                onClick={(e) => {
                  e.stopPropagation();
                  setOpen(false);
                  it.run();
                }}
                className={`fixed z-50 flex h-7 w-7 items-center justify-center rounded-full border border-line2 bg-card shadow-md transition-[transform,opacity,visibility] duration-200 [transition-timing-function:cubic-bezier(0.34,1.56,0.64,1)] ${it.cls} ${
                  open && anchor ? "visible" : "invisible pointer-events-none"
                }`}
                style={{
                  left: anchor?.x ?? 0,
                  top: anchor?.y ?? 0,
                  // 收起时所有扇叶叠在触发钮中心（scale 0.3）；展开沿弧位弹出，
                  // 逐片 30ms stagger；收拢不延迟。
                  transform: open
                    ? `translate(calc(-50% + ${dx}px), calc(-50% + ${dy}px)) scale(1)`
                    : "translate(-50%, -50%) scale(0.3)",
                  opacity: open ? 1 : 0,
                  transitionDelay: open ? `${i * 30}ms` : "0ms",
                }}
              >
                {it.icon}
              </button>
            );
          })}
        </div>,
        document.body,
      )}
      {onTags && tagsOpen && anchor && (
        <SessionTagsPanel
          anchor={anchor}
          title={title}
          initialIcon={tagsIcon ?? null}
          initialLabels={tagsLabels ?? []}
          onClose={() => setTagsOpen(false)}
          onSave={onTags}
        />
      )}
    </span>
  );
}
