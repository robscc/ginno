import { Hand } from "lucide-react";
import { useTranslations } from "next-intl";

export type SessionActivity = "waiting" | "compacting" | "running" | "idle";

export function SessionActivityIndicator({ activity }: { activity: SessionActivity }) {
  const t = useTranslations("chat.status");
  const shell = useTranslations("shell.topbar");
  const label = activity === "waiting" || activity === "compacting" ? t(activity) : shell(activity);

  return (
    <span
      className="relative inline-flex size-4 flex-none items-center justify-center"
      data-session-activity={activity}
      role="img"
      aria-label={label}
      title={label}
    >
      {activity === "waiting" && (
        <span aria-hidden className="session-activity-hand flex size-4 items-center justify-center rounded-full bg-amber-500 text-white shadow-sm ring-1 ring-background">
          <Hand className="size-2.5" strokeWidth={2.5} />
        </span>
      )}
      {activity === "compacting" && (
        <span aria-hidden className="session-compaction-roller">
          <span className="track" />
          <span className="paper" />
          <span className="wheel" />
        </span>
      )}
      {activity === "running" && (
        <span aria-hidden className="running-indicator">
          <span className="pulse" />
          <span className="core" />
        </span>
      )}
      {activity === "idle" && (
        <span aria-hidden className="size-2 rounded-full" style={{ background: "var(--st-skipped)" }} />
      )}
    </span>
  );
}
