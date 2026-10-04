"use client";

import { useEffect, useState } from "react";
import { useTranslations } from "next-intl";
import * as api from "@/lib/runtime";
import { BookOpen, Search, Download, Save } from "lucide-react";

interface KBForm {
  enabled: boolean;
  vault_path: string;
  raw_dir: string;
  wiki_dir: string;
  auto_inject: boolean;
  inject_top_k: number;
  inject_min_score: number;
  rescan_interval_s: number;
  use_semantic: boolean;
  embedding_model: string;
  semantic_weight: number;
  // memory refinery (dead config revived: capture/auto-distill/budget)
  capture: boolean;
  auto_summarize: boolean;
  pool_flush_threshold: number;
  memory_budget_chars: number;
  summarize_model: string;
}

const DEFAULTS: KBForm = {
  enabled: false,
  vault_path: "",
  raw_dir: "Ginno/Raw",
  wiki_dir: "Ginno/Wiki",
  auto_inject: true,
  inject_top_k: 5,
  inject_min_score: 0.3,
  rescan_interval_s: 60,
  use_semantic: false,
  embedding_model: "",
  semantic_weight: 0.5,
  capture: true,
  auto_summarize: true,
  pool_flush_threshold: 30,
  memory_budget_chars: 3000,
  summarize_model: "",
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <label className="field-label">{label}</label>
      {children}
    </div>
  );
}

export function KnowledgeSettings() {
  const t = useTranslations("settings.knowledge");
  const [form, setForm] = useState<KBForm>(DEFAULTS);
  const [probe, setProbe] = useState<string>("");
  const [msg, setMsg] = useState<string>("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .getSettings()
      .then((s) => {
        const k = (s as Record<string, any>).knowledge || {};
        setForm({ ...DEFAULTS, ...k });
      })
      .catch(() => {});
  }, []);

  const set = <K extends keyof KBForm>(key: K, value: KBForm[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  async function detect() {
    setProbe("");
    if (!form.vault_path.trim()) {
      setProbe(t("enterPath"));
      return;
    }
    const r = await api.kbWikiProbe(form.vault_path.trim());
    if (!r.ok) {
      setProbe(r.error || t("detectFailed"));
      return;
    }
    const d = r.detected;
    if (d?.wiki_dir) set("wiki_dir", d.wiki_dir);
    if (d?.raw_dir) set("raw_dir", d.raw_dir);
    setProbe(
      d?.namespace
        ? t("detected", {
            ns: d.namespace,
            wiki: r.wiki_pages ?? 0,
            raw: r.raw_pages ?? 0,
          }) + (r.has_index ? t("withIndex") : "")
        : t("noWikiDir", { count: r.total_md ?? 0 }),
    );
  }

  async function save(andIndex: boolean) {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.kbWikiPutConfig(form);
      if (!r.ok) {
        setMsg(t("saveFailed"));
        return;
      }
      if (andIndex) {
        const ix = await api.kbWikiReindex();
        setMsg(ix.ok ? t("savedIndexed", { count: ix.indexed }) : t("savedIndexFailed"));
      } else {
        setMsg(t("saved"));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <h2 className="flex items-center gap-2 text-lg font-semibold text-txt">
        <BookOpen className="h-5 w-5 text-violet" /> {t("title")}
      </h2>
      <p className="mt-1 text-sm text-muted">
        {t.rich("description", {
          code: (chunks) => <code className="text-txt">{chunks}</code>,
        })}
      </p>

      <div className="mt-5 space-y-4">
        <Field label={t("vaultPath")}>
          <div className="flex gap-2">
            <input
              className="field flex-1"
              placeholder="/Users/…/Documents/Obsidian Vault"
              value={form.vault_path}
              onChange={(e) => set("vault_path", e.target.value)}
            />
            <button
              onClick={detect}
              className="flex items-center gap-1.5 rounded-lg border border-line2 px-3 text-xs text-muted hover:text-txt"
            >
              <Search className="h-3.5 w-3.5" /> {t("detect")}
            </button>
          </div>
          {probe && <div className="mt-1 text-xs text-violet">{probe}</div>}
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label={t("wikiDir")}>
            <input className="field" value={form.wiki_dir} onChange={(e) => set("wiki_dir", e.target.value)} />
          </Field>
          <Field label={t("rawDir")}>
            <input className="field" value={form.raw_dir} onChange={(e) => set("raw_dir", e.target.value)} />
          </Field>
        </div>

        <label className="flex items-center gap-2 text-sm text-txt">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          {t("enableLabel")}
        </label>
        <label className="flex items-center gap-2 text-sm text-txt">
          <input
            type="checkbox"
            checked={form.auto_inject}
            onChange={(e) => set("auto_inject", e.target.checked)}
          />
          {t("autoInject")}
        </label>

        <div className="grid grid-cols-3 gap-3">
          <Field label={t("injectTopK")}>
            <input
              type="number"
              className="field"
              value={form.inject_top_k}
              onChange={(e) => set("inject_top_k", Number(e.target.value) || 0)}
            />
          </Field>
          <Field label={t("minScore")}>
            <input
              type="number"
              step="0.05"
              className="field"
              value={form.inject_min_score}
              onChange={(e) => set("inject_min_score", Number(e.target.value) || 0)}
            />
          </Field>
          <Field label={t("rescanInterval")}>
            <input
              type="number"
              className="field"
              value={form.rescan_interval_s}
              onChange={(e) => set("rescan_interval_s", Number(e.target.value) || 0)}
            />
          </Field>
        </div>

        <label className="flex items-center gap-2 text-sm text-txt">
          <input
            type="checkbox"
            checked={form.use_semantic}
            onChange={(e) => set("use_semantic", e.target.checked)}
          />
          {t.rich("semanticLabel", {
            code: (chunks) => <code className="font-mono text-xs">{chunks}</code>,
          })}
        </label>
        {form.use_semantic && (
          <div className="grid grid-cols-2 gap-3 rounded-lg border border-line bg-base/30 p-3">
            <Field label={t("embeddingModel")}>
              <input
                className="field font-mono text-xs"
                placeholder="…paraphrase-multilingual-MiniLM-L12-v2"
                value={form.embedding_model}
                onChange={(e) => set("embedding_model", e.target.value)}
              />
            </Field>
            <Field label={t("semanticWeight")}>
              <input
                type="number"
                step="0.1"
                className="field"
                value={form.semantic_weight}
                onChange={(e) => set("semantic_weight", Number(e.target.value) || 0)}
              />
            </Field>
            <p className="col-span-2 text-xs text-faint">{t("semanticNote")}</p>
          </div>
        )}

        <div className="rounded-lg border border-line bg-base/30 p-3">
          <div className="mb-2 text-sm text-txt">{t("refineryTitle")}</div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={form.capture}
              onChange={(e) => set("capture", e.target.checked)}
            />
            {t("captureLabel")}
          </label>
          <label className="mt-2 flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={form.auto_summarize}
              onChange={(e) => set("auto_summarize", e.target.checked)}
            />
            {t("autoSummarizeLabel")}
          </label>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Field label={t("flushThreshold")}>
              <input
                type="number"
                className="field"
                value={form.pool_flush_threshold}
                onChange={(e) => set("pool_flush_threshold", Number(e.target.value) || 0)}
              />
            </Field>
            <Field label={t("memoryBudget")}>
              <input
                type="number"
                className="field"
                value={form.memory_budget_chars}
                onChange={(e) => set("memory_budget_chars", Number(e.target.value) || 0)}
              />
            </Field>
          </div>
          <div className="mt-3">
            <Field label={t("distillModel")}>
              <input
                className="field font-mono text-xs"
                placeholder={t("distillPlaceholder")}
                value={form.summarize_model}
                onChange={(e) => set("summarize_model", e.target.value)}
              />
            </Field>
          </div>
          <p className="mt-2 text-xs text-faint">{t("distillNote")}</p>
        </div>

        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={() => save(true)}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" /> {t("saveIndex")}
          </button>
          <button
            onClick={() => save(false)}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" /> {t("saveOnly")}
          </button>
          {msg && <span className="text-xs text-violet">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
