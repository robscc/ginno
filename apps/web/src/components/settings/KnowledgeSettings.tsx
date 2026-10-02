"use client";

import { useEffect, useState } from "react";
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
      setProbe("Enter a vault path first");
      return;
    }
    const r = await api.kbWikiProbe(form.vault_path.trim());
    if (!r.ok) {
      setProbe(r.error || "Detection failed");
      return;
    }
    const d = r.detected;
    if (d?.wiki_dir) set("wiki_dir", d.wiki_dir);
    if (d?.raw_dir) set("raw_dir", d.raw_dir);
    setProbe(
      d?.namespace
        ? `Detected namespace "${d.namespace}": Wiki ${r.wiki_pages} pages${r.has_index ? " (with INDEX)" : ""} / Raw ${r.raw_pages} notes`
        : `No */Wiki directory detected (the whole vault will be indexed as the knowledge base, ${r.total_md} notes in total)`,
    );
  }

  async function save(andIndex: boolean) {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.kbWikiPutConfig(form);
      if (!r.ok) {
        setMsg("Failed to save");
        return;
      }
      if (andIndex) {
        const ix = await api.kbWikiReindex();
        setMsg(ix.ok ? `Saved and indexed ${ix.indexed} pages` : "Saved, but indexing failed");
      } else {
        setMsg("Saved");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <h2 className="flex items-center gap-2 text-lg font-semibold text-txt">
        <BookOpen className="h-5 w-5 text-violet" /> Knowledge Base
      </h2>
      <p className="mt-1 text-sm text-muted">
        Point to an Obsidian vault. An existing compiled LLM Wiki (e.g.{" "}
        <code className="text-txt">Molly/Wiki</code>) is indexed directly, no recompilation needed.
      </p>

      <div className="mt-5 space-y-4">
        <Field label="Vault path (absolute)">
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
              <Search className="h-3.5 w-3.5" /> Detect
            </button>
          </div>
          {probe && <div className="mt-1 text-xs text-violet">{probe}</div>}
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Wiki directory (searchable knowledge; relative to vault)">
            <input className="field" value={form.wiki_dir} onChange={(e) => set("wiki_dir", e.target.value)} />
          </Field>
          <Field label="Raw directory (compilation source; relative to vault)">
            <input className="field" value={form.raw_dir} onChange={(e) => set("raw_dir", e.target.value)} />
          </Field>
        </div>

        <label className="flex items-center gap-2 text-sm text-txt">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          Enable knowledge base
        </label>
        <label className="flex items-center gap-2 text-sm text-txt">
          <input
            type="checkbox"
            checked={form.auto_inject}
            onChange={(e) => set("auto_inject", e.target.checked)}
          />
          Auto-inject relevant content into each turn
        </label>

        <div className="grid grid-cols-3 gap-3">
          <Field label="Inject top-K">
            <input
              type="number"
              className="field"
              value={form.inject_top_k}
              onChange={(e) => set("inject_top_k", Number(e.target.value) || 0)}
            />
          </Field>
          <Field label="Minimum relevance score">
            <input
              type="number"
              step="0.05"
              className="field"
              value={form.inject_min_score}
              onChange={(e) => set("inject_min_score", Number(e.target.value) || 0)}
            />
          </Field>
          <Field label="Index refresh interval (seconds)">
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
          Semantic retrieval (local embeddings, requires{" "}
          <code className="font-mono text-xs">uv sync --extra rag</code>)
        </label>
        {form.use_semantic && (
          <div className="grid grid-cols-2 gap-3 rounded-lg border border-line bg-base/30 p-3">
            <Field label="Embedding model (sentence-transformers; empty = multilingual default)">
              <input
                className="field font-mono text-xs"
                placeholder="…paraphrase-multilingual-MiniLM-L12-v2"
                value={form.embedding_model}
                onChange={(e) => set("embedding_model", e.target.value)}
              />
            </Field>
            <Field label="Semantic weight (added to the lexical score)">
              <input
                type="number"
                step="0.1"
                className="field"
                value={form.semantic_weight}
                onChange={(e) => set("semantic_weight", Number(e.target.value) || 0)}
              />
            </Field>
            <p className="col-span-2 text-xs text-faint">
              When enabled, &quot;Save &amp; Index&quot; / Build wiki embeds the Wiki pages (the
              model is downloaded on first use). If the rag dependencies are missing, or the model
              download or encoding fails, it silently falls back to pure lexical retrieval.
            </p>
          </div>
        )}

        <div className="rounded-lg border border-line bg-base/30 p-3">
          <div className="mb-2 text-sm text-txt">Memory Refinery</div>
          <label className="flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={form.capture}
              onChange={(e) => set("capture", e.target.checked)}
            />
            Capture assistant replies into the memory pool after each turn
          </label>
          <label className="mt-2 flex items-center gap-2 text-sm text-txt">
            <input
              type="checkbox"
              checked={form.auto_summarize}
              onChange={(e) => set("auto_summarize", e.target.checked)}
            />
            Draft automatically when the threshold is reached (drafts still require manual review;
            memory is never rewritten silently)
          </label>
          <div className="mt-3 grid grid-cols-2 gap-3">
            <Field label="Auto-draft threshold (turns in pool)">
              <input
                type="number"
                className="field"
                value={form.pool_flush_threshold}
                onChange={(e) => set("pool_flush_threshold", Number(e.target.value) || 0)}
              />
            </Field>
            <Field label="Memory budget (chars)">
              <input
                type="number"
                className="field"
                value={form.memory_budget_chars}
                onChange={(e) => set("memory_budget_chars", Number(e.target.value) || 0)}
              />
            </Field>
          </div>
          <div className="mt-3">
            <Field label="Distillation model (provider; empty = follow default)">
              <input
                className="field font-mono text-xs"
                placeholder="Leave empty to use the default provider"
                value={form.summarize_model}
                onChange={(e) => set("summarize_model", e.target.value)}
              />
            </Field>
          </div>
          <p className="mt-2 text-xs text-faint">
            Distillation always produces drafts: review and edit the diff in the right-column Memory
            panel; only adopted drafts are written to MEMORY.md.
          </p>
        </div>

        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={() => save(true)}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" /> Save &amp; Index
          </button>
          <button
            onClick={() => save(false)}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" /> Save only
          </button>
          {msg && <span className="text-xs text-violet">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
