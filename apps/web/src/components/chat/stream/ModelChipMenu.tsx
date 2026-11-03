"use client";

// ModelChipMenu — composer 的模型 chip 与下拉菜单（自 ChatStream.tsx 机械拆出，
// 渲染逐字一致）：per-session provider/model 切换；home 态为「首条消息将生效」
// 的选择（含 agent 绑定与应用默认的解析结果）。thinking-effort-design §5：
// 菜单尾部带「Thinking」分组（5 档 radio），chip 上以角标显示非 auto 档。

import { Brain, ChevronDown, Globe } from "lucide-react";
import { useTranslations } from "next-intl";
import type { AgentConfig, ProviderConfig } from "@/lib/types";
import type { SessionMeta } from "@/lib/types";

const EFFORTS = ["auto", "off", "low", "medium", "high"] as const;
// chip 角标缩写（locale 无关）;auto 不占宽。
const EFFORT_BADGE: Record<string, string> = {
  off: "OFF",
  low: "LO",
  medium: "MED",
  high: "HI",
};

export function ModelChipMenu({
  session,
  label,
  homeModelSource,
  targetAgent,
  homeEffective,
  enabledProviders,
  thinkingEffort,
  effortDisabled,
  open,
  onToggle,
  onClose,
  onPick,
  onPickEffort,
}: {
  session: SessionMeta | null;
  label: string;
  homeModelSource: "picked" | "agent" | "default";
  targetAgent: AgentConfig | null;
  homeEffective: { provider?: string; model?: string };
  enabledProviders: Array<[string, ProviderConfig]>;
  /** 当前思考档位（"auto" = 跟随 provider 配置）。 */
  thinkingEffort: string;
  /** provider 声明 thinking_mode="none" 时置灰分组。 */
  effortDisabled: boolean;
  open: boolean;
  onToggle: () => void;
  onClose: () => void;
  onPick: (pid: string, model: string) => void;
  onPickEffort: (effort: string) => void;
}) {
  const tc = useTranslations("chat");
  const effortLabel: Record<string, string> = {
    auto: tc("model.thinkingAuto"),
    off: tc("model.thinkingOff"),
    low: tc("model.thinkingLow"),
    medium: tc("model.thinkingMedium"),
    high: tc("model.thinkingHigh"),
  };
  return (
    <div className="relative">
      <button
        type="button"
        onClick={onToggle}
        title={
          session
            ? tc("model.switchTitle")
            : homeModelSource === "agent"
              ? tc("model.fromAgentTitle", {
                  agent: targetAgent?.name ?? "",
                  model: label,
                })
              : homeModelSource === "default"
                ? tc("model.fromDefaultTitle", { model: label })
                : tc("model.pickTitle")
        }
        className="flex items-center gap-1.5 rounded-md border border-line2 bg-card px-2 py-1 text-xs text-muted hover:border-line hover:bg-card2 hover:text-txt disabled:opacity-50"
      >
        <Globe className="h-3.5 w-3.5 shrink-0" />
        <span className="max-w-[160px] truncate font-medium">{label}</span>
        {EFFORT_BADGE[thinkingEffort] && (
          <span
            className="shrink-0 rounded border border-violet/40 px-1 text-[9px] leading-4 font-medium text-violet"
            title={tc("model.thinkingTitle")}
          >
            {EFFORT_BADGE[thinkingEffort]}
          </span>
        )}
        <ChevronDown className="h-3 w-3 shrink-0 opacity-70" />
      </button>
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={onClose} />
          <div className="absolute bottom-full right-0 z-50 mb-1 w-72 max-w-[min(18rem,calc(100vw-2rem))] rounded-lg border border-line bg-card py-1 shadow-xl">
            {enabledProviders.length === 0 && (
              <div className="px-3 py-2 text-xs text-faint">
                {tc("model.noProviders")}
              </div>
            )}
            {enabledProviders.map(([pid, p]) => {
              // One row per MODEL, not per provider. The store keeps a
              // list (`models`) and a session can be pinned to any entry,
              // but showing only `default_model` left every extra model
              // unreachable from chat — which is exactly the "I cannot
              // pick GLM" report. Grouped by provider instead, and the
              // dangling `· ` disappears along with the single-row shape.
              const models = (
                Array.isArray(p.models) && p.models.length
                  ? p.models
                  : [p.default_model || p.model || ""]
              ).filter((m): m is string => !!m);
              // 菜单 ✓ 跟随「将要生效」的模型(含 agent 绑定/默认),与 chip 一致
              const curProvider = session ? session.provider : homeEffective?.provider;
              const curModel = session ? session.model : homeEffective?.model;
              return (
                <div key={pid}>
                  <div className="px-3 pb-0.5 pt-1.5 text-[10px] uppercase tracking-wider text-faint">
                    {p.name || pid}
                  </div>
                  {models.length === 0 ? (
                    <div className="px-3 py-1 text-[11px] text-faint">
                      {tc("model.noModels")}
                    </div>
                  ) : (
                    models.map((m) => {
                      const on = curProvider === pid && curModel === m;
                      return (
                        <button
                          key={`${pid}:${m}`}
                          onClick={() => onPick(pid, m)}
                          className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-card2 ${
                            on ? "text-txt" : "text-muted"
                          }`}
                        >
                          <span className={on ? "" : "opacity-0"}>✓</span>
                          <span className="min-w-0 flex-1 truncate">{m}</span>
                        </button>
                      );
                    })
                  )}
                </div>
              );
            })}
            {/* 思考档位分组(thinking-effort-design §5):5 档 radio,选定即
                热切;provider 声明不支持时整体置灰。 */}
            <div className="mt-1 border-t border-line pt-1">
              <div className="flex items-center gap-1.5 px-3 pb-0.5 pt-1 text-[10px] uppercase tracking-wider text-faint">
                <Brain className="h-3 w-3" />
                {tc("model.thinkingTitle")}
              </div>
              {effortDisabled ? (
                <div className="px-3 py-1 text-[11px] text-faint">
                  {tc("model.thinkingUnsupported")}
                </div>
              ) : (
                EFFORTS.map((e) => {
                  const on = thinkingEffort === e;
                  return (
                    <button
                      key={e}
                      onClick={() => onPickEffort(e)}
                      title={
                        e === "auto" ? tc("model.thinkingAutoHint") : undefined
                      }
                      className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-card2 ${
                        on ? "text-txt" : "text-muted"
                      }`}
                    >
                      <span className={on ? "" : "opacity-0"}>✓</span>
                      <span className="min-w-0 flex-1 truncate">{effortLabel[e]}</span>
                    </button>
                  );
                })
              )}
            </div>
            <div className="mt-1 border-t border-line px-3 pt-1 text-[10px] text-faint">
              {tc(session ? "model.footnote" : "model.footnoteHome")}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
