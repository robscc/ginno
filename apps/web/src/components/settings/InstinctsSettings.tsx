"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import { Loader2, Sparkles } from "lucide-react";
import * as api from "@/lib/runtime";
import { useGinno } from "@/lib/store";

// Settings → 智能体 → 本能 (docs/instincts-design.md §10.1)。
// 2026-11-15 P0：此前 instincts 配置段没有任何 UI 入口，observer_model
// 只能手改 settings.json——「默认空串=禁用 LLM」叠加无入口，管理页长期
// 空转。本页是「可操作」的入口；空串语义已恢复为「跟随主模型」。

type InstinctsForm = {
  enabled: boolean;
  capture: boolean;
  inject_enabled: boolean;
  inject_min_confidence: number;
  observer_model: string;
  observer_model_id: string;
  min_observations: number;
  correction_min_observations: number;
  analyze_gap_min: number;
};

const DEFAULTS: InstinctsForm = {
  enabled: true,
  capture: true,
  inject_enabled: true,
  inject_min_confidence: 0.7,
  observer_model: "",
  observer_model_id: "",
  min_observations: 20,
  correction_min_observations: 3,
  analyze_gap_min: 5,
};

const BOOL_FIELDS = [
  ["enabled", "enabled", "enabledDesc"],
  ["capture", "capture", "captureDesc"],
  ["inject_enabled", "injectEnabled", "injectEnabledDesc"],
] as const;

const NUM_FIELDS = [
  ["min_observations", "minObs", "minObsDesc"],
  ["correction_min_observations", "corrMinObs", "corrMinObsDesc"],
  ["analyze_gap_min", "gapMin", "gapMinDesc"],
] as const;

export function InstinctsSettings() {
  const t = useTranslations("settings.instincts");
  const g = useGinno();
  const [form, setForm] = useState<InstinctsForm>(DEFAULTS);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        const raw = ((s as Record<string, unknown>).instincts ||
          {}) as Partial<InstinctsForm>;
        setForm({ ...DEFAULTS, ...raw });
      })
      .catch(() => {})
      .finally(() => setLoaded(true));
  }, []);

  const set = <K extends keyof InstinctsForm>(key: K, value: InstinctsForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  // 供应商的可选模型（照 MemorySettings 口径）：models[] 优先，老配置单值退化
  const provModels = (pid: string): string[] => {
    const p = g.providers[pid];
    if (!p) return [];
    if (p.models?.length) return p.models;
    return [p.default_model || p.model || ""].filter(Boolean);
  };
  const judgeProvider = form.observer_model || g.defaultProvider;

  const save = async () => {
    setBusy(true);
    setMsg("");
    try {
      // PUT /settings 是整文件替换：先读当前值再合并，读失败绝不写
      //（同 WebSearchSettings/MemorySettings 的防误清空守卫）。
      const cur = (await api.getSettings()) as Record<string, unknown>;
      if (!cur || typeof cur !== "object" || Object.keys(cur).length === 0) {
        setMsg(t("connError"));
        return;
      }
      await api.putSettings({
        ...cur,
        instincts: {
          ...((cur.instincts as Record<string, unknown>) || {}),
          ...form,
          inject_min_confidence: Number(form.inject_min_confidence),
          min_observations: Math.max(
            1,
            Math.round(Number(form.min_observations) || DEFAULTS.min_observations),
          ),
          correction_min_observations: Math.max(
            1,
            Math.round(
              Number(form.correction_min_observations) ||
                DEFAULTS.correction_min_observations,
            ),
          ),
          analyze_gap_min: Math.max(
            1,
            Math.round(Number(form.analyze_gap_min) || DEFAULTS.analyze_gap_min),
          ),
        },
      });
      setMsg(t("saved"));
    } catch {
      setMsg(t("connError"));
    } finally {
      setBusy(false);
    }
  };

  if (!loaded) {
    return (
      <div className="flex items-center gap-2 px-8 py-10 text-sm text-faint">
        <Loader2 className="h-4 w-4 animate-spin" />
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-2xl px-8 py-8">
      <div className="mb-2 flex items-center gap-2">
        <Sparkles className="h-5 w-5 text-violet" />
        <h2 className="text-[1rem] font-semibold">{t("title")}</h2>
      </div>
      <p className="mb-6 text-xs leading-relaxed text-faint">{t("description")}</p>

      <div className="flex flex-col gap-1">
        {BOOL_FIELDS.map(([key, label, desc]) => (
          <label
            key={key}
            className="flex cursor-pointer items-start gap-2 border-b border-white/5 py-2.5"
          >
            <input
              type="checkbox"
              className="mt-0.5"
              checked={!!form[key]}
              onChange={(e) => set(key, e.target.checked)}
            />
            <span className="min-w-0">
              <span className="block text-[12.5px] text-txt">{t(label)}</span>
              <span className="block text-[11px] text-faint">{t(desc)}</span>
            </span>
          </label>
        ))}

        <div className="flex items-center gap-2 border-b border-white/5 py-2">
          <div className="min-w-0 flex-1">
            <div className="text-[12.5px] text-txt">{t("injectMinConf")}</div>
            <div className="text-[11px] text-faint">{t("injectMinConfDesc")}</div>
          </div>
          <select
            className="field w-24 py-1 text-xs"
            value={form.inject_min_confidence}
            onChange={(e) => set("inject_min_confidence", Number(e.target.value))}
          >
            {[0.3, 0.5, 0.7, 0.9].map((v) => (
              <option key={v} value={v}>
                {v.toFixed(1)}
              </option>
            ))}
          </select>
        </div>

        {NUM_FIELDS.map(([key, label, desc]) => (
          <div key={key} className="flex items-center gap-2 border-b border-white/5 py-2">
            <div className="min-w-0 flex-1">
              <div className="text-[12.5px] text-txt">{t(label)}</div>
              <div className="text-[11px] text-faint">{t(desc)}</div>
            </div>
            <input
              type="number"
              min={1}
              className="field w-20 py-1 text-right text-xs"
              value={form[key]}
              onChange={(e) => set(key, Number(e.target.value) || 0)}
            />
          </div>
        ))}

        <div className="mt-2">
          <label className="field-label" htmlFor="instincts-observer-provider">
            {t("observerProvider")}
          </label>
          <select
            id="instincts-observer-provider"
            className="field text-xs"
            value={form.observer_model}
            onChange={(e) => {
              const pid = e.target.value;
              // 换供应商后原模型 id 大概率不在新供应商的 models 里，回落默认
              if (
                form.observer_model_id &&
                !provModels(pid).includes(form.observer_model_id)
              ) {
                setForm((f) => ({ ...f, observer_model: pid, observer_model_id: "" }));
              } else {
                set("observer_model", pid);
              }
            }}
          >
            <option value="">{t("modelDefault")}</option>
            {Object.keys(g.providers).map((p) => (
              <option key={p} value={p} disabled={!g.providers[p].enabled}>
                {p}
                {g.providers[p].enabled ? "" : t("modelDisabled")}
              </option>
            ))}
            {form.observer_model && !g.providers[form.observer_model] && (
              // 已删除/改名的历史 provider：保留原值选项，避免下拉悄悄改存量配置
              <option value={form.observer_model}>{form.observer_model}</option>
            )}
          </select>
          <p className="mt-1 text-[11px] text-faint">{t("observerProviderDesc")}</p>
        </div>

        <div className="mt-2">
          <label className="field-label" htmlFor="instincts-observer-model">
            {t("model")}
          </label>
          <select
            id="instincts-observer-model"
            className="field text-xs"
            value={form.observer_model_id}
            onChange={(e) => set("observer_model_id", e.target.value)}
          >
            <option value="">{t("modelAuto")}</option>
            {provModels(judgeProvider).map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
            {form.observer_model_id &&
              !provModels(judgeProvider).includes(form.observer_model_id) && (
                // 手改 settings / 供应商删了该模型：保留原值，不悄悄丢
                <option value={form.observer_model_id}>{form.observer_model_id}</option>
              )}
          </select>
        </div>

        <div className="mt-3 flex items-center gap-2">
          <button
            onClick={save}
            disabled={busy}
            className="flex items-center gap-1 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            {busy && <Loader2 className="h-3 w-3 animate-spin" />}
            {t("save")}
          </button>
          {msg && <span className="text-[11px] text-faint">{msg}</span>}
        </div>
      </div>
    </div>
  );
}