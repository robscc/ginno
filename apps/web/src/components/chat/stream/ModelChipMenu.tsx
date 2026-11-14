"use client";

// ModelChipMenu — composer 的模型 chip 与下拉菜单（自 ChatStream.tsx 机械拆出，
// 渲染逐字一致）：per-session provider/model 切换；home 态为「首条消息将生效」
// 的选择（含 agent 绑定与应用默认的解析结果）。thinking-effort-design §5：
// 菜单尾部带「Thinking」分组（5 档 radio），chip 上以角标显示非 auto 档。

import { useState } from "react";
import { ChevronDown, ChevronRight, Globe } from "lucide-react";
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
  // 手风琴内联（2026-11-14 用户反馈「选模型还要点两次+难度要优化」）：
  // 点模型行 = 选择 + 关闭（一次完成）；行尾箭头 = 内联展开该模型的思考
  // 强度档（不跳页、无返回键），点强度档 = 选模型+设档+关闭。取代旧二级
  // 导航（旧版点模型强制跳二级，选模型还得点返回，改强度又要进二级）。
  const [expanded, setExpanded] = useState<string | null>(null);
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
            <div>
            {enabledProviders.length === 0 && (
              <div className="px-3 py-2 text-xs text-faint">
                {tc("model.noProviders")}
              </div>
            )}
            {enabledProviders.map(([pid, p]) => {
              const models = (
                Array.isArray(p.models) && p.models.length
                  ? p.models
                  : [p.default_model || p.model || ""]
              ).filter((m): m is string => !!m);
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
                      const key = `${pid}:${m}`;
                      const isOpen = expanded === key;
                      return (
                        <div key={key}>
                          <button
                            onClick={() => {
                              onPick(pid, m);
                              onClose();
                            }}
                            className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-card2 ${
                              on ? "text-txt" : "text-muted"
                            }`}
                          >
                            <span className={on ? "" : "opacity-0"}>✓</span>
                            <span className="min-w-0 flex-1 truncate">{m}</span>
                            {/* 行尾箭头：内联展开该模型思考强度（不跳页）。
                                选中行额外显示当前档角标。 */}
                            <span
                              role="button"
                              tabIndex={-1}
                              title={tc("model.thinkingTitle")}
                              onClick={(e) => {
                                e.stopPropagation();
                                setExpanded(isOpen ? null : key);
                              }}
                              className="-m-1 flex shrink-0 items-center gap-1 rounded p-1 text-faint hover:bg-line2/60 hover:text-txt"
                            >
                              {on && EFFORT_BADGE[thinkingEffort] && (
                                <span className="text-[9px] font-medium text-violet">
                                  {EFFORT_BADGE[thinkingEffort]}
                                </span>
                              )}
                              <ChevronRight
                                className={`h-3 w-3 transition-transform ${isOpen ? "rotate-90" : ""}`}
                              />
                            </span>
                          </button>
                          {isOpen && !effortDisabled && (
                            <div className="mb-1 ml-6 flex flex-wrap gap-1 px-3">
                              {EFFORTS.map((e) => {
                                const eOn = thinkingEffort === e && on;
                                return (
                                  <button
                                    key={e}
                                    onClick={() => {
                                      onPick(pid, m);
                                      onPickEffort(e);
                                      onClose();
                                    }}
                                    title={e === "auto" ? tc("model.thinkingAutoHint") : undefined}
                                    className={`rounded-full border px-2 py-0.5 text-[10px] transition-colors ${
                                      eOn
                                        ? "border-violet bg-violet/15 text-violet"
                                        : "border-line text-faint hover:border-line2 hover:text-muted"
                                    }`}
                                  >
                                    {effortLabel[e]}
                                  </button>
                                );
                              })}
                            </div>
                          )}
                          {isOpen && effortDisabled && (
                            <div className="mb-1 ml-6 px-3 text-[10px] text-faint">
                              {tc("model.thinkingUnsupported")}
                            </div>
                          )}
                        </div>
                      );
                    })
                  )}
                </div>
              );
            })}
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
