"use client";

import { useEffect, useRef } from "react";
import { useTranslations } from "next-intl";
import { ChevronDown, ChevronUp, X } from "lucide-react";
import type { SessionFindState } from "./useSessionFind";

/** Cmd+F 搜索浮层（find-bar-design §3.3）：右上角，Enter/⇧Enter 遍历，Esc 关闭。 */
export function FindBar({ state }: { state: SessionFindState }) {
  const t = useTranslations("chat");
  const inputRef = useRef<HTMLInputElement>(null);

  // 挂载聚焦全选；⌘F 再按 → focusRequest 变化重新聚焦全选（§2.1）。
  useEffect(() => {
    inputRef.current?.focus();
    inputRef.current?.select();
  }, [state.focusRequest]);

  if (!state.open) return null;
  const n = state.hits.length;

  return (
    <div className="absolute right-3 top-2 z-30 flex items-center gap-1 rounded-lg border border-line2 bg-card px-2 py-1.5 shadow-lg">
      <input
        ref={inputRef}
        value={state.query}
        onChange={(e) => state.setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.nativeEvent.isComposing) return; // IME 守卫（F12 先例）
          if (e.key === "Enter") {
            e.preventDefault();
            e.shiftKey ? state.prev() : state.next();
          } else if (e.key === "Escape") {
            e.preventDefault();
            state.close();
          }
        }}
        placeholder={t("find.placeholder")}
        className="w-52 rounded border-none bg-transparent px-1 text-[13px] outline-none placeholder:text-faint"
      />
      <span className={`min-w-[52px] text-center text-[11px] ${n === 0 && state.query.trim() ? "text-red" : "text-faint"}`}>
        {state.query.trim() ? (n === 0 ? t("find.none") : `${state.activeIdx + 1}/${n}`) : ""}
      </span>
      <button
        onClick={state.prev}
        disabled={n === 0}
        title={t("find.prev")}
        className="rounded p-1 text-faint hover:bg-card2 hover:text-muted disabled:opacity-40"
      >
        <ChevronUp className="h-3.5 w-3.5" aria-hidden />
      </button>
      <button
        onClick={state.next}
        disabled={n === 0}
        title={t("find.next")}
        className="rounded p-1 text-faint hover:bg-card2 hover:text-muted disabled:opacity-40"
      >
        <ChevronDown className="h-3.5 w-3.5" aria-hidden />
      </button>
      <button
        onClick={state.close}
        title={t("find.close")}
        className="rounded p-1 text-faint hover:bg-card2 hover:text-muted"
      >
        <X className="h-3.5 w-3.5" aria-hidden />
      </button>
    </div>
  );
}
