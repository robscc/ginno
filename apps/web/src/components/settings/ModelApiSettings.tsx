"use client";

import { useEffect, useRef, useState } from "react";
import { Zap, Server } from "lucide-react";
import { useGinno } from "@/lib/store";
import * as api from "@/lib/runtime";
import type { ProviderConfig, Providers } from "@/lib/types";
import { ProviderCard, type VerifyState } from "./ProviderCard";

const ORDER = ["anthropic", "openai", "custom"];
const META: Record<string, { name: string; subtitle: string; icon: React.ReactNode }> = {
  anthropic: {
    name: "Anthropic",
    subtitle: "Claude 3.5 / 3.7 Sonnet, Haiku, Opus",
    icon: (
      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-orange/15 text-orange">
        <Zap className="h-[18px] w-[18px] fill-orange" />
      </div>
    ),
  },
  openai: {
    name: "OpenAI",
    subtitle: "GPT-4o, GPT-4 Turbo, o1, o3-mini",
    icon: (
      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-green/15 text-green">
        <span className="h-3.5 w-3.5 rounded-full border-2 border-green" />
      </div>
    ),
  },
  custom: {
    name: "自定义端点 (OpenAI Compatible)",
    subtitle: "Ollama, DeepSeek, Qwen, Groq, LM Studio 等",
    icon: (
      <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-violet/15 text-violet">
        <Server className="h-[18px] w-[18px]" />
      </div>
    ),
  },
};

export function ModelApiSettings() {
  const g = useGinno();
  const [draft, setDraft] = useState<Providers>({});
  const [status, setStatus] = useState<Record<string, VerifyState>>({});
  const [saveMsg, setSaveMsg] = useState<{ ok: boolean; text: string } | null>(null);
  const [searchMsg, setSearchMsg] = useState<
    Record<string, { state: "idle" | "checking" | "ok" | "fail"; text?: string }>
  >({});
  const loaded = useRef(false);

  useEffect(() => {
    if (!loaded.current && Object.keys(g.providers).length) {
      setDraft(g.providers);
      setStatus(
        Object.fromEntries(Object.keys(g.providers).map((k) => [k, { state: "idle" }])),
      );
      loaded.current = true;
    }
  }, [g.providers]);

  const setField = (id: string, key: keyof ProviderConfig, value: unknown) =>
    setDraft((d) => ({ ...d, [id]: { ...d[id], [key]: value } }));

  // PUT replaces the whole providers block server-side, so always send every
  // provider. Refresh the global store afterwards — other pages (the Agents
  // provider dropdown, session creation) read providers/defaultProvider from it.
  const save = async (next: Providers, defaultProvider?: string): Promise<boolean> => {
    try {
      const r = await api.putProviders(next, defaultProvider);
      if (r?.ok !== false) {
        setSaveMsg({ ok: true, text: "已保存" });
        g.reloadProviders();
        return true;
      }
      setSaveMsg({ ok: false, text: "保存失败" });
    } catch {
      setSaveMsg({ ok: false, text: "保存失败：无法连接运行时" });
    }
    return false;
  };

  const onBlurSave = () => void save(draft);

  const onToggle = (id: string) => {
    const next = { ...draft, [id]: { ...draft[id], enabled: !draft[id].enabled } };
    setDraft(next);
    void save(next);
  };

  const onSetDefault = (id: string) => void save(draft, id);

  const onVerify = async (id: string) => {
    // verify runs against the *saved* config server-side, so persist first.
    const saved = await save(draft);
    if (!saved) {
      setStatus((s) => ({ ...s, [id]: { state: "fail", msg: "配置未保存，无法验证" } }));
      return;
    }
    setStatus((s) => ({ ...s, [id]: { state: "checking" } }));
    try {
      const r = await api.verifyProvider(id);
      setStatus((s) => ({
        ...s,
        [id]: r.ok ? { state: "ok", latency: r.latency_ms } : { state: "fail", msg: r.error },
      }));
    } catch {
      setStatus((s) => ({ ...s, [id]: { state: "fail", msg: "无法连接运行时" } }));
    }
  };

  const onToggleSearch = (id: string) => {
    const next = { ...draft, [id]: { ...draft[id], enable_search: !draft[id].enable_search } };
    setDraft(next);
    void save(next);
  };

  const onTestSearch = async (id: string) => {
    const saved = await save(draft);
    if (!saved) {
      setSearchMsg((s) => ({ ...s, [id]: { state: "fail", text: "配置未保存，无法测试" } }));
      return;
    }
    setSearchMsg((s) => ({ ...s, [id]: { state: "checking" } }));
    try {
      const r = await api.searchProbeProvider(id);
      setSearchMsg((s) => ({
        ...s,
        [id]: r.ok
          ? { state: "ok", text: r.text }
          : { state: "fail", text: r.error || "未知错误" },
      }));
    } catch {
      setSearchMsg((s) => ({ ...s, [id]: { state: "fail", text: "无法连接运行时" } }));
    }
  };

  return (
    <div className="mx-auto max-w-3xl px-8 py-7">
      <div className="flex items-baseline justify-between">
        <h2 className="text-lg font-semibold text-txt">模型 API 配置</h2>
        {saveMsg && (
          <span className={`text-xs ${saveMsg.ok ? "text-faint" : "text-red"}`}>{saveMsg.text}</span>
        )}
      </div>
      <p className="mt-1 text-sm text-muted">
        配置 Anthropic、OpenAI 等模型提供商的 API Key，用于驱动 Agent 推理能力。
      </p>

      <div className="mt-4 flex items-center gap-4 text-[11px] text-muted">
        <span className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-orange" /> Anthropic 协议
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-green" /> OpenAI 协议
        </span>
        <span className="flex items-center gap-1.5">
          <span className="h-1.5 w-1.5 rounded-full bg-violet" /> OpenAI Compatible
        </span>
      </div>

      <div className="mt-5 space-y-4">
        {ORDER.filter((id) => draft[id]).map((id) => (
          <ProviderCard
            key={id}
            cfg={draft[id]}
            icon={META[id]?.icon}
            name={META[id]?.name || id}
            subtitle={META[id]?.subtitle || ""}
            status={status[id] || { state: "idle" }}
            isDefault={g.defaultProvider === id}
            setField={(k, v) => setField(id, k, v)}
            onBlurSave={onBlurSave}
            onToggle={() => onToggle(id)}
            onVerify={() => onVerify(id)}
            onSetDefault={() => onSetDefault(id)}
            onToggleSearch={() => onToggleSearch(id)}
            onTestSearch={() => onTestSearch(id)}
            searchStatus={searchMsg[id] || { state: "idle" }}
          />
        ))}
      </div>
    </div>
  );
}
