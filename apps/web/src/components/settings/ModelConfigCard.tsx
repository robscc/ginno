"use client";

import { useState, type ReactNode } from "react";
import { useTranslations } from "next-intl";
import { ChevronUp, Layers, Plug, Sparkles, Star, Trash2 } from "lucide-react";
import type { ModelConfig, ModelProtocol } from "@/lib/types";
import { hostOf, PROTOCOL_LABEL, protocolBadge, relTimeParts } from "./modelConfigShared";

// One saved model config in the list. 卡片化（2026-10-17，对齐 AgentsSettings
// 的卡片语言）：默认收起为摘要卡，点击展开为 col-span-full 编辑体（children =
// ModelConfigForm embedded）。展开/收起与删除确认条都走 grid-rows 0fr↔1fr
// 动画，与 agent 卡逐字同一手感。所有写操作仍走父组件回调（PUT 是全量替换，
// 列表归父级所有）。头行右侧按钮区整体 stopPropagation：点按钮不触发展开。
// 铅笔按钮已移除——点击卡片即编辑（agents 语义）。

// 协议 → 图标：色取协议徽标色（protocolBadge），与 agent 卡的 hex+"22" chip 同手法。
const PROTOCOL_ICON: Record<ModelProtocol, typeof Plug> = {
  anthropic: Sparkles,
  "openai-compatible": Plug,
  "openai-responses": Layers,
};

function Toggle({ on, onClick, label }: { on: boolean; onClick: () => void; label: string }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      onClick={onClick}
      className="relative h-5 w-9 shrink-0 rounded-full transition-colors"
      style={{ background: on ? "#8b5cf6" : "#34343f" }}
    >
      <span
        className="absolute top-0.5 h-4 w-4 rounded-full bg-white transition-all"
        style={{ left: on ? "18px" : "2px" }}
      />
    </button>
  );
}

// Persisted verify state: green with a relative time, yellow past 7 days,
// red when the last verify failed, gray when never verified.
function VerifyDot({ cfg }: { cfg: ModelConfig }) {
  const t = useTranslations("settings.model");
  if (cfg.last_error) {
    return (
      <span className="flex items-center gap-1.5 text-faint" title={cfg.last_error}>
        <span className="h-1.5 w-1.5 shrink-0 rounded-full bg-red" />
        {t("verifyFailed")}
      </span>
    );
  }
  if (cfg.verified_at) {
    const stale = Date.now() / 1000 - cfg.verified_at > 7 * 86400;
    const p = relTimeParts(cfg.verified_at);
    const when = p.key === "justNow" ? t("justNow") : t(p.key, { count: p.count });
    return (
      <span
        className={`flex items-center gap-1.5 ${stale ? "text-yellow" : "text-faint"}`}
        title={stale ? t("lastVerified", { time: when }) : undefined}
      >
        <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${stale ? "bg-yellow" : "bg-green"}`} />
        {stale ? t("reverify") : t("verified", { time: when })}
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 text-faint">
      <span className="h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: "#71717a" }} />
      {t("notVerified")}
    </span>
  );
}

export function ModelConfigCard({
  cfg,
  isDefault,
  busy,
  open,
  onOpen,
  onClose,
  onToggle,
  onSetDefault,
  onDelete,
  children,
}: {
  cfg: ModelConfig;
  isDefault: boolean;
  busy?: boolean;
  open: boolean;
  onOpen: () => void;
  onClose: () => void;
  onToggle: () => void;
  onSetDefault: () => void;
  onDelete: () => void;
  children: ReactNode;
}) {
  const t = useTranslations("settings.model");
  const [confirming, setConfirming] = useState(false);
  const badge = protocolBadge(cfg);
  const host = hostOf(cfg.base_url) || t("officialEndpoint");
  const models = cfg.models ?? [];
  const chips = models.slice(0, 4); // 收起卡更矮，预览前 4 个 + "+N"（原 6）
  const Icon = PROTOCOL_ICON[cfg.protocol] ?? Plug;

  return (
    <div
      onClick={() => !open && onOpen()}
      className={`rounded-xl border bg-card p-3 transition-[border-color,opacity] ${
        open ? "col-span-full border-violet/40" : "cursor-pointer border-line hover:border-violet/30"
      } ${cfg.enabled ? "" : "opacity-70"}`}
    >
      <div className="flex items-center gap-2">
        <span
          className="flex h-6 w-6 shrink-0 items-center justify-center rounded-md"
          style={{ background: `${badge.color}22`, color: badge.color }}
          title={t("protocolTooltip", { protocol: PROTOCOL_LABEL[cfg.protocol] })}
        >
          <Icon className="h-3.5 w-3.5" />
        </span>
        <span className="truncate font-medium text-txt" title={cfg.name}>
          {cfg.name || cfg.id}
        </span>
        {isDefault && (
          <span className="rounded-full border border-violet/40 bg-violet/10 px-1.5 py-px text-[10px] text-violet" title={t("defaultTooltip")}>
            {t("defaultLabel")}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2" onClick={(e) => e.stopPropagation()}>
          {!isDefault && cfg.enabled && (
            <button
              onClick={onSetDefault}
              disabled={busy}
              className="pill border border-line2 text-faint transition-colors hover:text-muted disabled:opacity-50"
              title={t("setDefaultTooltip")}
            >
              {t("setDefault")}
            </button>
          )}
          {open && (
            <button
              onClick={onClose}
              className="text-xs text-faint hover:text-violet"
              title={t("collapseCard")}
            >
              <ChevronUp className="h-3.5 w-3.5" />
            </button>
          )}
          <button
            onClick={() => setConfirming((v) => !v)}
            aria-label={t("deleteAria", { name: cfg.name })}
            className="rounded-md p-1.5 text-faint transition-colors hover:bg-card2 hover:text-red"
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
          <Toggle on={cfg.enabled} onClick={onToggle} label={t("enableAria", { name: cfg.name })} />
        </div>
      </div>

      {/* 收起态摘要：host · 模型数 · chips 预览 · 验证状态 */}
      {!open && (
        <>
          <div className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1 text-[11px] text-faint">
            <span className="rounded bg-card2 px-1.5 py-px">
              {badge.label === "OpenAI" ? t("officialApi") : host}
            </span>
            <span>
              {t("modelsCount", { count: models.length })}
              {cfg.default_model ? t("defaultModelSuffix", { model: cfg.default_model }) : ""}
            </span>
          </div>
          {models.length > 0 && (
            <div className="mt-2 flex flex-wrap gap-1.5">
              {chips.map((m) => (
                <span
                  key={m}
                  className={`pill border font-mono ${
                    m === cfg.default_model ? "border-violet/50 text-violet" : "border-line2 text-muted"
                  }`}
                  title={m === cfg.default_model ? t("defaultModelTooltip") : undefined}
                >
                  {m === cfg.default_model && <Star className="h-2.5 w-2.5 fill-violet" />}
                  {m}
                </span>
              ))}
              {models.length > chips.length && (
                <span className="pill border border-line2 text-faint">+{models.length - chips.length}</span>
              )}
            </div>
          )}
          <div className="mt-2">
            <VerifyDot cfg={cfg} />
          </div>
        </>
      )}

      {/* 展开编辑体：grid-rows 动画（与 agent 卡同一容器写法） */}
      <div
        className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out ${
          open ? "grid-rows-[1fr] opacity-100" : "pointer-events-none grid-rows-[0fr] opacity-0"
        }`}
      >
        <div className="overflow-hidden">
          <div className="mt-3 border-t border-line pt-3">{children}</div>
        </div>
      </div>

      {/* Q8: single yellow bar inline, no second dialog. If the backend still
          refuses (default / referenced), the parent shows the refs list.
          常驻 DOM + grid 动画（条件渲染会跳过入场动画）。 */}
      <div
        className={`grid transition-[grid-template-rows,opacity] duration-300 ease-out ${
          confirming ? "grid-rows-[1fr] opacity-100" : "pointer-events-none grid-rows-[0fr] opacity-0"
        }`}
      >
        <div className="overflow-hidden">
          <div className="mt-3 rounded-md border border-yellow/40 bg-yellow/10 px-3 py-2 text-xs text-yellow">
            {isDefault ? `${t("confirmDefaultPrefix")} ` : ""}
            {t("confirmDelete", { name: cfg.name || cfg.id })}
            <div className="mt-2 flex gap-2">
              <button
                onClick={() => {
                  setConfirming(false);
                  onDelete();
                }}
                className="rounded-md border border-yellow/50 px-2.5 py-1 font-medium transition-colors hover:bg-yellow/20"
              >
                {t("delete")}
              </button>
              <button
                onClick={() => setConfirming(false)}
                className="rounded-md px-2.5 py-1 text-muted transition-colors hover:text-txt"
              >
                {t("cancel")}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
