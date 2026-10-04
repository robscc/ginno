"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { Globe, Save, FlaskConical } from "lucide-react";

// Web search & fetch (docs/citations-design.md §4.3 / §5.8): engine config +
// telemetry (per-engine cite rate = "results actually used").

interface WebForm {
  enabled: boolean;
  default_engine: string;
  max_results: number;
  timeout_s: number;
  searxng_base_url: string;
  tavily_api_key: string;
}

const DEFAULTS: WebForm = {
  enabled: true,
  default_engine: "duckduckgo",
  max_results: 5,
  timeout_s: 15,
  searxng_base_url: "",
  tavily_api_key: "",
};

// label 存 catalog key（settings.web.engines.*），渲染时经 t() 翻译；
// as const 保证 label 为字面量联合（next-intl key 类型检查要求）
const ENGINES = [
  { id: "duckduckgo", label: "ddg" },
  { id: "searxng", label: "searxng" },
  { id: "tavily", label: "tavily" },
] as const;

function Field({ label, children, hint }: { label: string; children: React.ReactNode; hint?: string }) {
  return (
    <div>
      <label className="field-label">{label}</label>
      {children}
      {hint && <p className="mt-1 text-xs text-faint">{hint}</p>}
    </div>
  );
}

export function WebSearchSettings() {
  const t = useTranslations("settings.web");
  const [form, setForm] = useState<WebForm>(DEFAULTS);
  const [msg, setMsg] = useState("");
  const [busy, setBusy] = useState(false);
  const [testing, setTesting] = useState(false);
  const [usage, setUsage] = useState<Awaited<ReturnType<typeof api.getWebUsage>> | null>(null);

  const reloadUsage = () => api.getWebUsage().then(setUsage).catch(() => {});

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        const w = ((s as Record<string, any>).web || {}) as Record<string, any>;
        const engines = (w.engines || {}) as Record<string, any>;
        setForm({
          ...DEFAULTS,
          enabled: w.enabled ?? DEFAULTS.enabled,
          default_engine: w.default_engine || DEFAULTS.default_engine,
          max_results: w.max_results ?? DEFAULTS.max_results,
          timeout_s: w.timeout_s ?? DEFAULTS.timeout_s,
          searxng_base_url: engines?.searxng?.base_url || "",
          tavily_api_key: engines?.tavily?.api_key || "",
        });
      })
      .catch(() => {});
    reloadUsage();
  }, []);

  const set = <K extends keyof WebForm>(key: K, value: WebForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  const save = async () => {
    setBusy(true);
    setMsg("");
    try {
      // PUT /settings is a FULL-FILE replace: read the current settings first
      // and ABORT if the read fails — falling back to {} here would wipe every
      // other settings block (providers/permissions/knowledge) on save.
      const cur = (await api.getSettings()) as Record<string, any>;
      // ensure_layout always seeds a non-empty settings.json, so an empty read
      // means a failed/corrupt GET — refusing is safer than wiping config.
      if (!cur || typeof cur !== "object" || Object.keys(cur).length === 0) {
        setMsg(t("readAborted"));
        return;
      }
      const engines: Record<string, Record<string, string>> = {};
      if (form.searxng_base_url.trim()) engines.searxng = { base_url: form.searxng_base_url.trim() };
      if (form.tavily_api_key.trim()) engines.tavily = { api_key: form.tavily_api_key.trim() };
      await api.putSettings({
        ...cur,
        web: {
          enabled: form.enabled,
          default_engine: form.default_engine,
          max_results: form.max_results,
          timeout_s: form.timeout_s,
          engines,
        },
      });
      setMsg(t("saved"));
    } catch (e) {
      setMsg(t("saveFailed", { error: String(e) }));
    } finally {
      setBusy(false);
    }
  };

  const testSearch = async () => {
    setTesting(true);
    setMsg("");
    try {
      const r = await api.testWebSearch(form.default_engine);
      setMsg(r.ok ? t("testOk", { engine: form.default_engine, count: r.results ?? 0 }) : t("testFail", { error: r.error || t("searchFailed") }));
      reloadUsage();
    } catch (e) {
      setMsg(t("testFailed", { error: String(e) }));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="mx-auto max-w-2xl px-8 py-8">
      <div className="mb-6 flex items-center gap-2">
        <Globe className="h-5 w-5 text-blue" />
        <h2 className="text-[1rem] font-semibold">{t("title")}</h2>
      </div>

      <div className="flex flex-col gap-5">
        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
          />
          {t("enableLabel")}
        </label>

        <Field label={t("defaultEngine")}>
          <select
            className="field"
            value={form.default_engine}
            onChange={(e) => set("default_engine", e.target.value)}
          >
            {ENGINES.map((e) => (
              <option key={e.id} value={e.id}>
                {t(`engines.${e.label}`)}
              </option>
            ))}
          </select>
        </Field>

        {form.default_engine === "searxng" && (
          <Field label={t("searxngUrl")} hint={t("searxngHint")}>
            <input
              className="field"
              value={form.searxng_base_url}
              onChange={(e) => set("searxng_base_url", e.target.value)}
              placeholder="http://127.0.0.1:8888"
            />
          </Field>
        )}
        {form.default_engine === "tavily" && (
          <Field label={t("tavilyKey")}>
            <input
              className="field"
              type="password"
              value={form.tavily_api_key}
              onChange={(e) => set("tavily_api_key", e.target.value)}
              placeholder="tvly-…"
            />
          </Field>
        )}

        <div className="grid grid-cols-2 gap-4">
          <Field label={t("maxResults")}>
            <input
              className="field"
              type="number"
              min={1}
              max={10}
              value={form.max_results}
              onChange={(e) => set("max_results", Math.max(1, Math.min(10, Number(e.target.value) || 5)))}
            />
          </Field>
          <Field label={t("timeout")}>
            <input
              className="field"
              type="number"
              min={3}
              max={60}
              value={form.timeout_s}
              onChange={(e) => set("timeout_s", Math.max(3, Math.min(60, Number(e.target.value) || 15)))}
            />
          </Field>
        </div>

        <div className="flex items-center gap-2">
          <button
            className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
            onClick={save}
            disabled={busy}
          >
            <Save className="h-4 w-4" /> {t("save")}
          </button>
          <button
            className="flex items-center gap-1.5 rounded-lg border border-line2 px-3 py-1.5 text-xs text-muted hover:text-txt disabled:opacity-50"
            onClick={testSearch}
            disabled={testing || !form.enabled}
          >
            <FlaskConical className="h-4 w-4" /> {testing ? t("testing") : t("testSearch")}
          </button>
        </div>
        {msg && <p className="text-xs text-muted">{msg}</p>}

        {usage && (usage.total_searches > 0 || usage.total_cited > 0) && (
          <div className="mt-4 rounded-lg border border-line/60 p-4 text-xs">
            <div className="mb-2 font-medium text-muted">
              {t("telemetry", { searches: usage.total_searches, cited: usage.total_cited })}
            </div>
            {usage.engines.length > 0 && (
              <table className="w-full text-left">
                <thead>
                  <tr className="text-faint">
                    <th className="py-1 font-normal">{t("thEngine")}</th>
                    <th className="py-1 font-normal">{t("thSearches")}</th>
                    <th className="py-1 font-normal">{t("thHits")}</th>
                    <th className="py-1 font-normal">{t("thRate")}</th>
                  </tr>
                </thead>
                <tbody>
                  {usage.engines.map((e) => (
                    <tr key={e.engine} className="border-t border-line/40">
                      <td className="py-1">{e.engine}</td>
                      <td className="py-1">{e.searches}</td>
                      <td className="py-1">{e.hits_cited}</td>
                      <td className="py-1">{Math.round(e.cite_rate * 100)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
            {usage.top_domains.length > 0 && (
              <div className="mt-3 text-faint">
                {t("topDomains")}
                {usage.top_domains.slice(0, 8).map((d) => (
                  <span key={d.domain} className="ml-1.5 rounded bg-panel px-1.5 py-0.5 text-muted">
                    {d.domain} ×{d.cited}
                  </span>
                ))}
              </div>
            )}
          </div>
        )}

        <p className="text-xs leading-relaxed text-faint">{t("citationsNote")}</p>
      </div>
    </div>
  );
}
