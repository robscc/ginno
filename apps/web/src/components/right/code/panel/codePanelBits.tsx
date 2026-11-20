"use client";

/**
 * Small presentational building blocks shared across the code panel family
 * (top bar, conflict bar, empty states). Extracted verbatim from the tail of
 * CodePanel.tsx in the hooks split (批次 3-3) — no behaviour change.
 */

import type { ReactNode } from "react";
import { ChevronRight } from "lucide-react";
import { useTranslations } from "next-intl";
import { cn } from "@/lib/utils";

export function ToolButton({
  title,
  active = false,
  onClick,
  children,
}: {
  title: string;
  active?: boolean;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      title={title}
      aria-label={title}
      aria-pressed={active}
      onClick={onClick}
      className={cn(
        "rounded p-1 transition-colors",
        active ? "bg-card2 text-txt" : "text-faint hover:bg-card hover:text-txt",
      )}
    >
      {children}
    </button>
  );
}

/** Small text button, used by the conflict bar. */
export function BarButton({
  onClick,
  icon,
  title,
  children,
}: {
  onClick: () => void;
  icon?: ReactNode;
  title?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      className="flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] text-muted transition-colors hover:bg-card2 hover:text-txt"
    >
      {icon}
      {children}
    </button>
  );
}

export function Breadcrumb({
  crumbs,
  onJump,
}: {
  crumbs: { label: string; path: string }[];
  onJump: (path: string) => void;
}) {
  const t = useTranslations("code.panel");
  if (!crumbs.length)
    return <span className="min-w-0 flex-1 truncate text-[11px] text-faint">{t("noFileOpen")}</span>;
  return (
    <div className="flex min-w-0 flex-1 items-center overflow-hidden text-[11px]">
      {crumbs.map((c, i) => (
        <span key={`${c.path}-${i}`} className="flex min-w-0 items-center">
          {i > 0 && <ChevronRight className="mx-0.5 h-3 w-3 shrink-0 text-faint" />}
          <button
            type="button"
            onClick={() => onJump(c.path)}
            title={c.path || t("breadcrumbRoot")}
            className={cn(
              "max-w-[160px] truncate rounded px-1 py-0.5 hover:bg-card2 hover:text-txt",
              i === crumbs.length - 1 ? "text-txt" : "text-muted",
            )}
          >
            {c.label}
          </button>
        </span>
      ))}
    </div>
  );
}

export function EmptyHint({
  icon,
  text,
  hint,
}: {
  icon?: ReactNode;
  text: string;
  hint?: string;
}) {
  return (
    <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-xs text-faint">
      {icon}
      <span>{text}</span>
      {hint ? <span className="text-[11px] text-faint/80">{hint}</span> : null}
    </div>
  );
}
