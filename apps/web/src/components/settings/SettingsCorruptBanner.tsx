"use client";

import { useEffect, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";

/**
 * settings.json 损坏横幅（契约 §5 安全恢复流程的前端侧）。
 *
 * 自包含：挂载时 GET /api/settings/health，status==="corrupt" 才渲染；
 * 网络失败/未知形状静默不渲染（绝不误报损坏）。两个变体：
 *
 * - variant="app"：主页/工作区顶部紧凑条——告知「保存已锁定」+ 跳转
 *   设置页的恢复入口。可 dismiss（下次挂载重新探测）。
 * - variant="panel"：设置页顶部完整恢复流程——① 非破坏备份（POST
 *   /api/settings/backup，原文件不动、可重复调用）②a 手动修复说明
 *   ②b UI 修复（PUT /api/settings/repair，服务端落盘前自动 pre-repair
 *   备份）③ restart_required 指引。
 *
 * 纪律：损坏的原文件逐字节保留，本组件绝不自动重置/覆盖/「修复」——
 * 一切写动作（仅 repair）都由用户显式发起且先备份。
 */
export function SettingsCorruptBanner({ variant }: { variant: "app" | "panel" }) {
  const t = useTranslations("settings.corrupt");
  const [health, setHealth] = useState<api.SettingsHealth | null>(null);
  const [dismissed, setDismissed] = useState(false);
  const [backup, setBackup] = useState<api.BackupResult | null>(null);
  const [backingUp, setBackingUp] = useState(false);
  const [draft, setDraft] = useState("");
  const [repairing, setRepairing] = useState(false);
  const [repair, setRepair] = useState<api.RepairResult | null>(null);
  const [draftInvalid, setDraftInvalid] = useState(false);

  useEffect(() => {
    let alive = true;
    api
      .getSettingsHealth()
      .then((h) => {
        if (alive) setHealth(h);
      })
      .catch(() => {
        /* 网络失败：静默，下次挂载再探测 */
      });
    return () => {
      alive = false;
    };
  }, []);

  if (!health || health.status !== "corrupt" || dismissed) return null;

  if (variant === "app") {
    return (
      <div
        data-testid="settings-corrupt-app"
        role="alert"
        className="flex items-center gap-2 border-b border-red/40 bg-red/[0.04] px-4 py-2 text-xs"
      >
        <span className="selectable font-medium text-red">{t("title")}</span>
        <span className="selectable text-muted">{t("appLine")}</span>
        <Link
          href="/settings/general"
          className="rounded border border-red/40 px-2 py-0.5 font-medium text-red hover:bg-red/10"
        >
          {t("appAction")}
        </Link>
        <button
          onClick={() => setDismissed(true)}
          className="ml-auto shrink-0 rounded border border-line px-2 py-0.5 text-faint hover:text-txt"
        >
          {t("dismiss")}
        </button>
      </div>
    );
  }

  async function doBackup() {
    if (backingUp) return;
    setBackingUp(true);
    try {
      setBackup(await api.backupSettings());
    } finally {
      setBackingUp(false);
    }
  }

  async function doRepair() {
    if (repairing) return;
    setDraftInvalid(false);
    let doc: Record<string, unknown>;
    try {
      const parsed = JSON.parse(draft) as unknown;
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("not an object");
      doc = parsed as Record<string, unknown>;
    } catch {
      setDraftInvalid(true);
      return;
    }
    setRepairing(true);
    try {
      setRepair(await api.repairSettings(doc));
    } finally {
      setRepairing(false);
    }
  }

  return (
    <div
      data-testid="settings-corrupt-banner"
      role="alert"
      className="border-b border-red/40 bg-red/[0.04] px-8 py-4 text-sm"
    >
      <h3 className="font-semibold text-red">{t("title")}</h3>
      <p className="mt-1 select-text font-mono text-xs text-muted" data-testid="settings-corrupt-error">
        {t("detail", { error: health.error ?? "" })}
      </p>
      <p className="mt-0.5 select-text font-mono text-xs text-faint" data-testid="settings-corrupt-path">
        {t("pathLabel", { path: health.path })}
      </p>
      <p className="mt-2 text-xs text-muted">{t("kept")}</p>

      {!repair?.ok && (
        <div className="mt-3 space-y-3">
          <div>
            <p className="text-xs font-medium text-txt">{t("backupTitle")}</p>
            <button
              data-testid="settings-corrupt-backup"
              onClick={() => void doBackup()}
              disabled={backingUp}
              className="mt-1 rounded border border-line2 px-2.5 py-1 text-xs text-muted hover:text-txt disabled:opacity-50"
            >
              {backingUp ? "…" : t("backup")}
            </button>
            {backup && (
              <p
                data-testid="settings-corrupt-backup-result"
                className={`mt-1 select-text break-all font-mono text-xs ${backup.ok ? "text-muted" : "text-red"}`}
              >
                {backup.ok ? t("backupDone", { path: backup.path ?? "" }) : t("backupFailed", { error: backup.error ?? "" })}
              </p>
            )}
          </div>
          <div>
            <p className="text-xs font-medium text-txt">{t("manualTitle")}</p>
            <p className="mt-1 max-w-2xl text-xs text-muted">{t("manualBody")}</p>
          </div>
          <div>
            <p className="text-xs font-medium text-txt">{t("repairTitle")}</p>
            <p className="mt-1 max-w-2xl text-xs text-muted">{t("repairHint")}</p>
            <textarea
              data-testid="settings-corrupt-repair-input"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
                setDraftInvalid(false);
              }}
              placeholder={t("repairPlaceholder")}
              rows={4}
              aria-label={t("repairPlaceholder")}
              spellCheck={false}
              className="mt-1 w-full max-w-2xl rounded border border-line2 bg-card2 p-2 font-mono text-xs text-txt"
            />
            {draftInvalid && <p className="text-xs text-red">{t("repairInvalid")}</p>}
            <button
              data-testid="settings-corrupt-repair"
              onClick={() => void doRepair()}
              disabled={repairing || draft.trim() === ""}
              className="mt-1 rounded bg-red/90 px-3 py-1 text-xs text-white hover:bg-red disabled:opacity-50"
            >
              {t("repair")}
            </button>
            {repair && !repair.ok && (
              <p className="mt-1 text-xs text-red">
                {t("repairFailed", { error: repair.detail || repair.error || "" })}
              </p>
            )}
          </div>
        </div>
      )}

      {repair?.ok && (
        <div className="mt-3 space-y-1" data-testid="settings-corrupt-repair-done">
          <p className="text-xs font-medium text-txt">{t("repairDone", { path: repair.backup ?? "" })}</p>
          <p className="text-xs text-muted">{t("repairRestart")}</p>
        </div>
      )}
    </div>
  );
}
