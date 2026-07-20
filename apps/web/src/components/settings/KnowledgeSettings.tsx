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
      setProbe("请先填写 vault 路径");
      return;
    }
    const r = await api.kbWikiProbe(form.vault_path.trim());
    if (!r.ok) {
      setProbe(r.error || "检测失败");
      return;
    }
    const d = r.detected;
    if (d?.wiki_dir) set("wiki_dir", d.wiki_dir);
    if (d?.raw_dir) set("raw_dir", d.raw_dir);
    setProbe(
      d?.namespace
        ? `检测到命名空间「${d.namespace}」：Wiki ${r.wiki_pages} 页${r.has_index ? "（含 INDEX）" : ""} / Raw ${r.raw_pages} 篇`
        : `未检测到 */Wiki 目录（将把整个 vault 作为知识库索引，共 ${r.total_md} 篇）`,
    );
  }

  async function save(andIndex: boolean) {
    setBusy(true);
    setMsg("");
    try {
      const r = await api.kbWikiPutConfig(form);
      if (!r.ok) {
        setMsg("保存失败");
        return;
      }
      if (andIndex) {
        const ix = await api.kbWikiReindex();
        setMsg(ix.ok ? `已保存并索引 ${ix.indexed} 页` : "已保存，但索引失败");
      } else {
        setMsg("已保存");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <h2 className="flex items-center gap-2 text-lg font-semibold text-txt">
        <BookOpen className="h-5 w-5 text-violet" /> 知识库
      </h2>
      <p className="mt-1 text-sm text-muted">
        指向一个 Obsidian vault。已有编译好的 LLM Wiki（如 <code className="text-txt">Molly/Wiki</code>）会被直接索引，无需重新编译。
      </p>

      <div className="mt-5 space-y-4">
        <Field label="Vault 路径（绝对路径）">
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
              <Search className="h-3.5 w-3.5" /> 检测
            </button>
          </div>
          {probe && <div className="mt-1 text-xs text-violet">{probe}</div>}
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field label="Wiki 目录（可检索知识；相对 vault）">
            <input className="field" value={form.wiki_dir} onChange={(e) => set("wiki_dir", e.target.value)} />
          </Field>
          <Field label="Raw 目录（编译源；相对 vault）">
            <input className="field" value={form.raw_dir} onChange={(e) => set("raw_dir", e.target.value)} />
          </Field>
        </div>

        <label className="flex items-center gap-2 text-sm text-txt">
          <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} />
          启用知识库
        </label>
        <label className="flex items-center gap-2 text-sm text-txt">
          <input
            type="checkbox"
            checked={form.auto_inject}
            onChange={(e) => set("auto_inject", e.target.checked)}
          />
          每轮对话按相关性自动注入
        </label>

        <div className="grid grid-cols-3 gap-3">
          <Field label="注入 top-K">
            <input
              type="number"
              className="field"
              value={form.inject_top_k}
              onChange={(e) => set("inject_top_k", Number(e.target.value) || 0)}
            />
          </Field>
          <Field label="最小相关度">
            <input
              type="number"
              step="0.05"
              className="field"
              value={form.inject_min_score}
              onChange={(e) => set("inject_min_score", Number(e.target.value) || 0)}
            />
          </Field>
          <Field label="索引刷新间隔(秒)">
            <input
              type="number"
              className="field"
              value={form.rescan_interval_s}
              onChange={(e) => set("rescan_interval_s", Number(e.target.value) || 0)}
            />
          </Field>
        </div>

        <div className="flex items-center gap-2 pt-1">
          <button
            onClick={() => save(true)}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-lg bg-violet px-3 py-1.5 text-xs font-medium text-white hover:opacity-90 disabled:opacity-50"
          >
            <Download className="h-3.5 w-3.5" /> 保存并索引
          </button>
          <button
            onClick={() => save(false)}
            disabled={busy}
            className="flex items-center gap-1.5 rounded-lg border border-line bg-card px-3 py-1.5 text-xs text-muted hover:text-txt disabled:opacity-50"
          >
            <Save className="h-3.5 w-3.5" /> 仅保存
          </button>
          {msg && <span className="text-xs text-violet">{msg}</span>}
        </div>
      </div>
    </div>
  );
}
