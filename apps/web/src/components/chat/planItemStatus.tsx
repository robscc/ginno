import { useTranslations } from "next-intl";
import type { PlanItem, PlanItemStatus } from "@/lib/types";

export const PLAN_ITEM_STATUS = {
  pending: { label: "status.pending", color: "bg-faint" },
  blocked: { label: "planBoard.itemBlocked", color: "bg-yellow" },
  running: { label: "status.running", color: "bg-blue" },
  done: { label: "status.done", color: "bg-green" },
  failed: { label: "status.failed", color: "bg-red" },
  stopped: { label: "status.stopped", color: "bg-faint" },
} as const satisfies Record<PlanItemStatus, { label: string; color: string }>;

/** The server stores pending; both views derive whether its dependencies are ready. */
export function resolvePlanItemStatuses(items: PlanItem[]): PlanItem[] {
  const byId = new Map(items.map((item) => [item.item_id, item.status]));
  return items.map((item) =>
    item.status === "pending" && item.depends_on?.some((id) => byId.get(id) !== "done")
      ? { ...item, status: "blocked" }
      : item,
  );
}

export function PlanItemStatusLabel({ status }: { status: PlanItemStatus }) {
  const t = useTranslations("chat");
  const meta = PLAN_ITEM_STATUS[status] ?? PLAN_ITEM_STATUS.pending;
  return (
    <span className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap text-[11px] font-medium leading-4 text-txt">
      <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${meta.color}`} aria-hidden />
      {t(meta.label)}
    </span>
  );
}
