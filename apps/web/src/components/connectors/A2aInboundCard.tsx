"use client";

/* A2A 枢纽入站控制卡（a2a-connector-design.md §5/§6.2/§12.A/§12.C）：
 * 开关（开启需二次确认+安全提示）、绑定范围、接入地址明文（大号等宽 +
 * 复制 + 重置二次确认）、证书指纹、实际监听地址；出站「接入外部 Agent」
 * 入口也挂在这张卡上（枢纽概览页）。
 *
 * props 数据源：connector.config（settings.connectors.a2a）与
 * connector.extra（后端 report_meta 上报的只读派生值，契约）：
 *   inboundAddress     能力地址明文（https://host:port/a2a/<token>#fp=…）
 *   inboundFingerprint 证书指纹（26 字符 base32）
 *   listenAddress      实际监听地址（host:port）
 * 后端骨架阶段未上报时降级为「尚未上报」提示，不造假数据。
 *
 * 重置走 connector action "a2a_reset_inbound_address"（后端集成轮落地）；
 * 开关走既有 PATCH /api/connectors/a2a/config。
 *
 * 地址明文是刻意设计（§12.C-2：要复制分发，打码妨碍使用），配
 * 「此地址等同密码，请勿公开分享」提示；重置/开启均二次确认（§12.C-4）。 */

import { useState } from "react";
import { useTranslations } from "next-intl";
import {
  Check,
  Copy,
  Loader2,
  Network,
  Plus,
  RotateCcw,
  ShieldAlert,
} from "lucide-react";
import {
  connectorAction,
  patchConnectorConfig,
  type ConnectorInfo,
} from "@/lib/api/connectors";
import { copyText } from "@/lib/errorReport";
import { A2A_HUB_ID, A2aPeerAddModal } from "./A2aPeerAddModal";

// ---- 纯逻辑层（node 环境 vitest 直接测） ------------------------------------

export type BindMode = "loopback" | "lan" | "custom";

export interface InboundView {
  enabled: boolean;
  /** A2A 总开关（config.a2a_enabled）；关着时入站开关无意义。 */
  masterOn: boolean;
  bindMode: BindMode;
  /** i18n key：bind-loopback / bind-lan / bind-custom（字面量联合，
   * useTranslations 静态检查）。 */
  bindLabelKey: "bind-loopback" | "bind-lan" | "bind-custom";
  bindInterface: string;
  port: number;
  /** 能力地址明文——不掩码（§12.C-2）。空串 = 未生成/未上报。 */
  address: string;
  fingerprint: string;
  listenAddress: string;
  hasAddress: boolean;
}

function str(v: unknown): string {
  return typeof v === "string" ? v : "";
}

/** 从 config + extra 派生展示模型（纯函数）。 */
export function inboundView(
  config: Record<string, unknown> | undefined,
  extra: Record<string, unknown> | undefined,
): InboundView {
  const c = config ?? {};
  const x = extra ?? {};
  const rawMode = str(c.bind_mode);
  const bindMode: BindMode =
    rawMode === "lan" || rawMode === "custom" ? rawMode : "loopback";
  const bindInterface = str(c.bind_interface);
  const port =
    typeof c.port === "number" && Number.isFinite(c.port) && c.port > 0
      ? Math.floor(c.port)
      : 8790;
  const address = str(x.inboundAddress);
  // 实际监听地址：后端上报优先；缺失时按绑定配置推导（lan 无法前端枚举，留空）。
  const listenAddress =
    str(x.listenAddress) ||
    (bindMode === "loopback"
      ? `127.0.0.1:${port}`
      : bindMode === "custom" && bindInterface
        ? `${bindInterface}:${port}`
        : "");
  return {
    enabled: c.inbound_enabled === true,
    masterOn: c.a2a_enabled === true,
    bindMode,
    bindLabelKey: `bind-${bindMode}`,
    bindInterface,
    port,
    address,
    fingerprint: str(x.inboundFingerprint),
    listenAddress,
    hasAddress: address !== "",
  };
}

/** 开启入站需要二次确认；关闭不需要（§12.C-4 危险操作显式化）。 */
export function needsEnableConfirm(currentEnabled: boolean, next: boolean): boolean {
  return next && !currentEnabled;
}

/** 二次确认小状态机（开启/重置共用）：request → confirm；accept/cancel → idle。
 *  accept 后的异步动作由组件执行，状态机只管确认门。 */
export type ConfirmState = "idle" | "confirm";
export type ConfirmEvent = "request" | "cancel" | "accept";

export function confirmReducer(state: ConfirmState, event: ConfirmEvent): ConfirmState {
  if (event === "request") return "confirm";
  return "idle";
}

/** 开关 PATCH payload（只动 inbound_enabled，其余键由后端合并保留）。 */
export function inboundTogglePatch(next: boolean): Record<string, unknown> {
  return { inbound_enabled: next };
}

// ---- 组件 ------------------------------------------------------------------

export function A2aInboundCard({
  connector,
  onReload,
}: {
  /** 枢纽连接器（id="a2a"）的详情数据，config/extra 为数据源。 */
  connector: ConnectorInfo;
  /** 配置变更/重置后刷新详情与列表。 */
  onReload: () => Promise<void> | void;
}) {
  const t = useTranslations("a2a");
  const view = inboundView(connector.config, connector.extra);

  const [enableConfirm, setEnableConfirm] = useState<ConfirmState>("idle");
  const [resetConfirm, setResetConfirm] = useState<ConfirmState>("idle");
  const [busy, setBusy] = useState<"toggle" | "reset" | null>(null);
  const [msg, setMsg] = useState<{ kind: "ok" | "error"; text: string } | null>(null);
  const [copied, setCopied] = useState(false);
  const [wizardOpen, setWizardOpen] = useState(false);

  const applyToggle = async (next: boolean) => {
    setBusy("toggle");
    setMsg(null);
    try {
      const r = await patchConnectorConfig(A2A_HUB_ID, inboundTogglePatch(next));
      if (r.ok === false || r.error) {
        setMsg({ kind: "error", text: r.error || t("inbound-save-failed") });
        return;
      }
      await onReload();
    } catch {
      setMsg({ kind: "error", text: t("inbound-save-failed") });
    } finally {
      setBusy(null);
    }
  };

  const onToggleClick = () => {
    const next = !view.enabled;
    if (needsEnableConfirm(view.enabled, next)) {
      setEnableConfirm((s) => confirmReducer(s, "request"));
    } else {
      void applyToggle(next);
    }
  };

  const doCopy = async () => {
    const ok = await copyText(view.address);
    if (ok) {
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } else {
      setMsg({ kind: "error", text: t("inbound-copy-failed") });
    }
  };

  const doReset = async () => {
    setBusy("reset");
    setMsg(null);
    try {
      const r = await connectorAction(A2A_HUB_ID, "a2a_reset_inbound_address");
      if (r && r.ok === false) {
        setMsg({ kind: "error", text: String(r.error || t("inbound-reset-failed")) });
        return;
      }
      setMsg({ kind: "ok", text: t("inbound-reset-done") });
      await onReload();
    } catch {
      setMsg({ kind: "error", text: t("inbound-reset-failed") });
    } finally {
      setBusy(null);
    }
  };

  return (
    <section className="space-y-4 rounded-xl border border-line bg-card2/40 p-4">
      {/* 入站 */}
      <div className="space-y-3">
        <div className="flex items-center gap-2">
          <Network className="h-4 w-4 shrink-0 text-violet" />
          <h3 className="flex-1 text-[12.5px] font-semibold text-txt">{t("inbound-title")}</h3>
          <button
            type="button"
            role="switch"
            aria-checked={view.enabled}
            aria-label={t("inbound-toggle")}
            data-testid="a2a-inbound-toggle"
            onClick={onToggleClick}
            disabled={busy === "toggle"}
            className={`relative h-5 w-9 shrink-0 rounded-full transition-colors disabled:opacity-50 ${
              view.enabled ? "bg-violet-600" : "bg-line2"
            }`}
          >
            <span
              className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow transition-all ${
                view.enabled ? "left-[18px]" : "left-0.5"
              }`}
            />
          </button>
        </div>
        <p className="text-[11.5px] leading-relaxed text-faint">{t("inbound-subtitle")}</p>

        {!view.masterOn && (
          <p className="rounded-lg border border-line bg-base px-3 py-2 text-[11px] text-faint">
            {t("inbound-master-off")}
          </p>
        )}

        {/* 开启二次确认 + 安全提示（§12.C-4） */}
        {enableConfirm === "confirm" && (
          <div className="space-y-2 rounded-lg border border-amber/40 bg-amber/10 px-3 py-2.5">
            <p className="flex items-center gap-1.5 text-[12px] font-semibold text-amber">
              <ShieldAlert className="h-3.5 w-3.5" /> {t("inbound-enable-confirm-title")}
            </p>
            <p className="text-[11.5px] leading-relaxed text-muted">
              {t("inbound-enable-confirm-body")}
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                data-testid="a2a-inbound-enable-confirm"
                onClick={() => {
                  setEnableConfirm((s) => confirmReducer(s, "accept"));
                  void applyToggle(true);
                }}
                className="rounded-lg bg-amber px-2.5 py-1 text-[11px] font-medium text-white hover:opacity-90"
              >
                {t("inbound-enable-confirm-ok")}
              </button>
              <button
                type="button"
                onClick={() => setEnableConfirm((s) => confirmReducer(s, "cancel"))}
                className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-muted hover:bg-hover"
              >
                {t("inbound-confirm-cancel")}
              </button>
            </div>
          </div>
        )}

        {/* 绑定范围 */}
        <div className="flex gap-2 text-[11.5px]">
          <span className="w-24 shrink-0 text-faint">{t("inbound-bind-mode")}</span>
          <span className="text-muted">
            {t(view.bindLabelKey)}
            {view.bindMode === "custom" && view.bindInterface ? ` · ${view.bindInterface}` : ""}
            {` · :${view.port}`}
          </span>
        </div>

        {/* 接入地址：明文（§12.C-2），大号等宽 + 复制 + 重置 */}
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <span className="text-[11px] font-medium text-muted">{t("inbound-address")}</span>
            <span className="flex items-center gap-1 text-[10.5px] text-amber">
              <ShieldAlert className="h-3 w-3" /> {t("inbound-address-secret-hint")}
            </span>
          </div>
          {view.hasAddress ? (
            <>
              <div
                data-testid="a2a-inbound-address"
                className="selectable break-all rounded-lg border border-line bg-base px-3 py-2.5 font-mono text-[13px] leading-relaxed text-txt"
              >
                {view.address}
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  data-testid="a2a-inbound-copy"
                  onClick={() => void doCopy()}
                  className="flex items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-[11px] text-txt hover:bg-hover"
                >
                  {copied ? (
                    <Check className="h-3 w-3 text-green-600 dark:text-green-400" />
                  ) : (
                    <Copy className="h-3 w-3" />
                  )}
                  {copied ? t("inbound-copied") : t("inbound-copy")}
                </button>
                <button
                  type="button"
                  data-testid="a2a-inbound-reset"
                  onClick={() => setResetConfirm((s) => confirmReducer(s, "request"))}
                  disabled={busy === "reset"}
                  className="flex items-center gap-1 rounded-lg border border-line px-2.5 py-1 text-[11px] text-muted hover:bg-hover hover:text-red-500 disabled:opacity-50"
                >
                  {busy === "reset" ? (
                    <Loader2 className="h-3 w-3 animate-spin" />
                  ) : (
                    <RotateCcw className="h-3 w-3" />
                  )}
                  {t("inbound-reset")}
                </button>
              </div>
            </>
          ) : (
            <p className="rounded-lg border border-dashed border-line2 px-3 py-2 text-[11.5px] text-faint">
              {view.enabled ? t("inbound-address-not-ready") : t("inbound-address-empty")}
            </p>
          )}
        </div>

        {/* 重置二次确认：后果说明（§12.C-4） */}
        {resetConfirm === "confirm" && (
          <div className="space-y-2 rounded-lg border border-red/40 bg-red/10 px-3 py-2.5">
            <p className="text-[12px] font-semibold text-red-600 dark:text-red-400">
              {t("inbound-reset-confirm-title")}
            </p>
            <p className="text-[11.5px] leading-relaxed text-muted">
              {t("inbound-reset-confirm-body")}
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                data-testid="a2a-inbound-reset-confirm"
                onClick={() => {
                  setResetConfirm((s) => confirmReducer(s, "accept"));
                  void doReset();
                }}
                className="rounded-lg bg-red-600 px-2.5 py-1 text-[11px] font-medium text-white hover:bg-red-500"
              >
                {t("inbound-reset-ok")}
              </button>
              <button
                type="button"
                onClick={() => setResetConfirm((s) => confirmReducer(s, "cancel"))}
                className="rounded-lg border border-line px-2.5 py-1 text-[11px] text-muted hover:bg-hover"
              >
                {t("inbound-confirm-cancel")}
              </button>
            </div>
          </div>
        )}

        {/* 证书指纹 + 实际监听地址（只读派生值） */}
        {view.fingerprint && (
          <div className="flex gap-2 text-[11.5px]">
            <span className="w-24 shrink-0 text-faint">{t("inbound-fingerprint")}</span>
            <span className="selectable break-all font-mono text-[11px] text-muted">
              {view.fingerprint}
            </span>
          </div>
        )}
        {view.listenAddress && (
          <div className="flex gap-2 text-[11.5px]">
            <span className="w-24 shrink-0 text-faint">{t("inbound-listen")}</span>
            <span className="selectable break-all font-mono text-[11px] text-muted">
              {view.listenAddress}
            </span>
          </div>
        )}

        {msg && (
          <p
            className={`text-[11px] ${
              msg.kind === "error"
                ? "text-red-600 dark:text-red-400"
                : "text-green-700 dark:text-green-400"
            }`}
          >
            {msg.text}
          </p>
        )}
      </div>

      {/* 出站入口 */}
      <div className="space-y-2 border-t border-line pt-3">
        <h3 className="text-[12.5px] font-semibold text-txt">{t("outbound-title")}</h3>
        <p className="text-[11.5px] leading-relaxed text-faint">{t("outbound-subtitle")}</p>
        <button
          type="button"
          data-testid="a2a-peer-add"
          onClick={() => setWizardOpen(true)}
          className="flex items-center gap-1.5 rounded-lg border border-violet/40 bg-violet/10 px-3 py-1.5 text-[11.5px] font-medium text-violet hover:bg-violet/20"
        >
          <Plus className="h-3.5 w-3.5" /> {t("peer-add")}
        </button>
      </div>

      {wizardOpen && (
        <A2aPeerAddModal
          onClose={() => setWizardOpen(false)}
          onAdded={(name) => {
            setMsg({ kind: "ok", text: t("wizard-added", { name }) });
            void onReload();
          }}
        />
      )}
    </section>
  );
}
