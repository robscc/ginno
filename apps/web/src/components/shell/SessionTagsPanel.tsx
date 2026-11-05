"use client";

// SessionTagsPanel — 会话「标签与图标」打标面板（session-label-icon-design.md
// §3.2）：8 宫格 icon 单选（含「无」）+ ≤3 chip 输入（回车加 / ✕ 删）+
// 清除/保存。由 RowFanMenu 的「标签与图标」扇叶唤起，portal 到 body 的
// fixed 弹层（对齐 design-mocks/session-label-icon-preview.html 的暗色语言）。

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import { SESSION_TAG_ICONS } from "./sessionTagIcons";

const MAX_LABELS = 3;
const MAX_LEN = 20;
// 8 宫格文案 key（字面量映射——模板串会被 next-intl 的 key 类型拒绝）
const ICON_LABEL_KEYS = {
  wrench: "icon.wrench",
  bug: "icon.bug",
  doc: "icon.doc",
  release: "icon.release",
  research: "icon.research",
  chat: "icon.chat",
  warn: "icon.warn",
  star: "icon.star",
} as const;

export function SessionTagsPanel({
  anchor,
  title,
  initialIcon,
  initialLabels,
  onClose,
  onSave,
  saving,
}: {
  /** 触发按钮中心的视口坐标（fixed 弹层锚点，同 RowFanMenu 扇叶模式）。 */
  anchor: { x: number; y: number };
  title: string;
  initialIcon: string | null;
  initialLabels: string[];
  onClose: () => void;
  onSave: (icon: string | null, labels: string[]) => void;
  saving?: boolean;
}) {
  const t = useTranslations("shell.sessionTags");
  const [icon, setIcon] = useState<string | null>(initialIcon);
  const [labels, setLabels] = useState<string[]>(initialLabels);
  const [draft, setDraft] = useState("");
  const hostRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onDown = (e: PointerEvent) => {
      if (hostRef.current && !hostRef.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    inputRef.current?.focus();
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  const addDraft = () => {
    const v = draft.trim();
    if (!v) return;
    if (labels.length >= MAX_LABELS) return; // 上限提示由占位文案表达
    if (v.length > MAX_LEN) return;
    if (!labels.includes(v)) setLabels((ls) => [...ls, v]);
    setDraft("");
  };

  const save = () => {
    onSave(icon, labels);
    onClose();
  };

  const atLimit = labels.length >= MAX_LABELS;

  return createPortal(
    <div
      ref={hostRef}
      className="fixed z-50 w-64 rounded-lg border border-line2 bg-card p-3 shadow-xl"
      style={{
        left: Math.min(anchor.x, window.innerWidth - 268),
        top: Math.min(anchor.y, window.innerHeight - 340),
      }}
      onClick={(e) => e.stopPropagation()}
    >
      <div className="truncate text-xs font-medium text-txt">{title}</div>
      <div className="mt-0.5 text-[10px] text-faint">{t("subtitle")}</div>

      <div className="mt-2.5 text-[10px] text-faint">{t("iconSection")}</div>
      <div className="mt-1 grid grid-cols-3 gap-1.5">
        {/* 「无」= 清除 icon */}
        <button
          onClick={() => setIcon(null)}
          title={t("none")}
          className={`flex flex-col items-center gap-1 rounded-md border px-1 py-2 text-[10px] transition-colors ${
            icon === null
              ? "border-violet bg-violet/10 text-txt"
              : "border-line text-muted hover:border-line2 hover:bg-white/[0.03]"
          }`}
        >
          <span className="h-3.5 w-3.5 rounded-full border border-dashed border-line2" />
          {t("none")}
        </button>
        {SESSION_TAG_ICONS.map(({ id, Icon, color }) => (
          <button
            key={id}
            onClick={() => setIcon(id)}
            title={t(ICON_LABEL_KEYS[id as keyof typeof ICON_LABEL_KEYS])}
            className={`flex flex-col items-center gap-1 rounded-md border px-1 py-2 text-[10px] transition-colors ${
              icon === id
                ? "border-violet bg-violet/10 text-txt"
                : "border-line text-muted hover:border-line2 hover:bg-white/[0.03]"
            }`}
          >
            <Icon className="h-3.5 w-3.5" style={{ color }} />
            {t(ICON_LABEL_KEYS[id as keyof typeof ICON_LABEL_KEYS])}
          </button>
        ))}
      </div>

      <div className="mt-3 text-[10px] text-faint">{t("labelSection")}</div>
      <div className="mt-1 flex flex-wrap items-center gap-1 rounded-md border border-line bg-base/60 px-1.5 py-1.5">
        {labels.map((l) => (
          <span
            key={l}
            className="flex items-center gap-1 rounded-full border border-line2 bg-white/[0.03] px-1.5 py-0.5 text-[11px] text-muted"
          >
            {l}
            <button
              onClick={() => setLabels((ls) => ls.filter((x) => x !== l))}
              aria-label={t("removeLabel")}
              className="text-faint hover:text-txt"
            >
              ✕
            </button>
          </span>
        ))}
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              e.preventDefault();
              addDraft();
            } else if (e.key === "Backspace" && !draft && labels.length) {
              setLabels((ls) => ls.slice(0, -1));
            }
          }}
          disabled={atLimit}
          placeholder={atLimit ? t("limitHint", { max: MAX_LABELS }) : t("labelPlaceholder")}
          maxLength={MAX_LEN}
          className="min-w-[80px] flex-1 bg-transparent text-xs text-txt outline-none placeholder:text-faint"
        />
      </div>

      <div className="mt-3 flex items-center justify-between">
        <button
          onClick={() => {
            setIcon(null);
            setLabels([]);
          }}
          className="rounded-md border border-line2 px-2.5 py-1 text-[11px] text-muted transition-colors hover:text-txt"
        >
          {t("clear")}
        </button>
        <button
          onClick={save}
          disabled={saving}
          className="rounded-md bg-violet px-3 py-1 text-[11px] font-medium text-white transition-opacity disabled:opacity-50"
        >
          {t("save")}
        </button>
      </div>
    </div>,
    document.body,
  );
}
