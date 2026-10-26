"use client";

import { useEffect, useRef, useState } from "react";
import { Minus, Plus, RotateCcw, X } from "lucide-react";

/**
 * 全屏看图：缩放 + 拖拽平移。
 *
 * 只负责「把一段已渲染好的 svg 放大给人看」，不碰 mermaid —— 图在进入这里之前
 * 已经渲染完成（主题、安全配置都由 MermaidBlock 决定），所以这个组件可以完全
 * 不知道 mermaid 的存在。
 *
 * 滚轮用原生监听器（passive:false）而不是 React onWheel：React 17+ 在根节点
 * 上以 passive 方式挂 wheel，`preventDefault` 会被忽略，页面会跟着一起滚。
 */
const MIN_SCALE = 0.3;
const MAX_SCALE = 6;

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

export function MermaidPanZoom({
  svg,
  labels,
  onClose,
}: {
  svg: string;
  labels: { dialog: string; zoomIn: string; zoomOut: string; resetZoom: string; close: string };
  onClose: () => void;
}) {
  const [scale, setScale] = useState(1);
  const [offset, setOffset] = useState({ x: 0, y: 0 });
  const viewRef = useRef<HTMLDivElement | null>(null);
  const dragRef = useRef<{ id: number; x: number; y: number } | null>(null);

  // Esc 关闭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // 滚轮缩放（非 passive）
  useEffect(() => {
    const el = viewRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
      setScale((s) => clamp(s * factor, MIN_SCALE, MAX_SCALE));
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, []);

  const reset = () => {
    setScale(1);
    setOffset({ x: 0, y: 0 });
  };

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-label={labels.dialog}
      className="fixed inset-0 z-50 flex flex-col bg-black/70 backdrop-blur-sm"
      onClick={onClose}
    >
      <div
        className="flex items-center justify-between px-4 py-3 text-xs text-white/85"
        onClick={(e) => e.stopPropagation()}
      >
        <span className="truncate font-medium">{labels.dialog}</span>
        <div className="flex items-center gap-1">
          <button
            type="button"
            aria-label={labels.zoomOut}
            onClick={() => setScale((s) => clamp(s / 1.2, MIN_SCALE, MAX_SCALE))}
            className="rounded p-1.5 text-white/70 transition-colors hover:bg-white/10 hover:text-white"
          >
            <Minus className="h-4 w-4" aria-hidden />
          </button>
          <span className="w-12 text-center tabular-nums text-white/60">
            {Math.round(scale * 100)}%
          </span>
          <button
            type="button"
            aria-label={labels.zoomIn}
            onClick={() => setScale((s) => clamp(s * 1.2, MIN_SCALE, MAX_SCALE))}
            className="rounded p-1.5 text-white/70 transition-colors hover:bg-white/10 hover:text-white"
          >
            <Plus className="h-4 w-4" aria-hidden />
          </button>
          <button
            type="button"
            aria-label={labels.resetZoom}
            onClick={reset}
            className="rounded p-1.5 text-white/70 transition-colors hover:bg-white/10 hover:text-white"
          >
            <RotateCcw className="h-4 w-4" aria-hidden />
          </button>
          <button
            type="button"
            aria-label={labels.close}
            onClick={onClose}
            className="rounded p-1.5 text-white/70 transition-colors hover:bg-white/10 hover:text-white"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
      </div>

      <div
        ref={viewRef}
        className="relative flex-1 cursor-grab touch-none select-none overflow-hidden active:cursor-grabbing"
        onClick={(e) => e.stopPropagation()}
        onPointerDown={(e) => {
          if (e.button !== 0) return;
          e.currentTarget.setPointerCapture?.(e.pointerId);
          dragRef.current = { id: e.pointerId, x: e.clientX, y: e.clientY };
        }}
        onPointerMove={(e) => {
          const d = dragRef.current;
          if (!d || d.id !== e.pointerId) return;
          setOffset((o) => ({ x: o.x + (e.clientX - d.x), y: o.y + (e.clientY - d.y) }));
          dragRef.current = { ...d, x: e.clientX, y: e.clientY };
        }}
        onPointerUp={() => {
          dragRef.current = null;
        }}
        onPointerCancel={() => {
          dragRef.current = null;
        }}
      >
        <div
          className="absolute left-1/2 top-1/2 origin-center"
          style={{
            transform: `translate(-50%, -50%) translate(${offset.x}px, ${offset.y}px) scale(${scale})`,
          }}
        >
          <div
            className="rounded-xl bg-[rgb(var(--card))] p-5 shadow-2xl [&_svg]:h-auto [&_svg]:max-w-none"
            dangerouslySetInnerHTML={{ __html: svg }}
          />
        </div>
      </div>
    </div>
  );
}
