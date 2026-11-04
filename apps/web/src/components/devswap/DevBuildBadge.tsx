"use client";

// DevBuildBadge — dev·{指纹} 琥珀 pill（docs/runtime-hot-swap-design.md P1）：
// /api/health 的 build.channel 非 app（壳从 dev-override 拉起 sidecar 时注入
// GINNO_RUNTIME_CHANNEL=dev，2026-11-04 起为可靠信号）或 fingerprint 带
// -dirty 时显示，明示「你正跑在开发 bundle 上」。挂在侧栏品牌行 GinnoWork
// 右侧；单次 fetch——dev-swap 收尾会整页 reload，无需轮询。

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";

type HealthBuild = { fingerprint?: string; channel?: string; built_at?: string };

/** built_at（ISO-8601 Z）→ 本地 HH:mm；解析失败返回 null（不显示时间）。 */
function builtClock(builtAt?: string): string | null {
  if (!builtAt) return null;
  const d = new Date(builtAt);
  if (Number.isNaN(d.getTime())) return null;
  return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

export function DevBuildBadge() {
  const tr = useTranslations("devswap");
  const [build, setBuild] = useState<HealthBuild | null>(null);
  useEffect(() => {
    let alive = true;
    fetch(`${api.BASE}/health`)
      .then((r) => r.json())
      .then((j: { build?: HealthBuild }) => {
        const b = j.build;
        if (alive && b && (b.channel === "dev" || (b.fingerprint || "").includes("-dirty")))
          setBuild(b);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);
  if (!build) return null;
  const fp = build.fingerprint || "dev";
  const clock = builtClock(build.built_at);
  return (
    <span
      className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 font-mono text-[10px] text-amber-600 dark:text-amber-400"
      title={tr("badge.hint", { fp, time: clock ?? "—" })}
    >
      dev·{fp.slice(0, 13)}
      {clock && <span className="ml-1 opacity-70">{clock}</span>}
    </span>
  );
}
